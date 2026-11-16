import asyncio
import json
from types import SimpleNamespace

import httpx

from backend.brain.openai_keychain import OpenAIKeychain
from backend.brain.openai_provider import (
    OpenAIClient,
    OpenAIRequestError,
    _transient_transport_error,
    text_models,
)
from backend.brain.pricing import estimate
from backend.brain.providers import FallbackClient
from backend.config import OpenAIConfig


def test_model_filter_keeps_text_chat_models_only():
    models = [
        SimpleNamespace(id="gpt-4.1-mini"),
        SimpleNamespace(id="gpt-4o-mini-transcribe"),
        SimpleNamespace(id="gpt-image-1"),
        SimpleNamespace(id="text-embedding-3-small"),
        SimpleNamespace(id="chat-latest"),
    ]
    assert text_models(models) == ["chat-latest", "gpt-4.1-mini"]


def test_wrapped_sdk_transport_failures_are_classified_as_transient():
    try:
        raise RuntimeError("SDK wrapper") from httpx.ConnectError("offline")
    except RuntimeError as error:
        assert _transient_transport_error(error)
    assert not _transient_transport_error(ValueError("invalid request"))


def test_responses_input_preserves_tool_flow_and_marks_old_results_untrusted():
    instructions, items = OpenAIClient._input(
        [
            {"role": "system", "content": "Ты Чушпан."},
            {"role": "user", "content": "Сколько времени?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "clock", "arguments": {"zone": "Europe/Tallinn"}},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "12:30"},
            {"role": "tool", "content": "old result"},
        ]
    )
    assert instructions == "Ты Чушпан."
    assert items[1]["type"] == "function_call"
    assert items[1]["name"] == "clock"
    assert items[2] == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": "Недоверенный результат инструмента: 12:30",
    }
    assert "Предыдущий результат инструмента (данные)" in items[3]["content"]


def test_streaming_maps_text_tool_call_and_usage_without_live_api():
    class FakeStream:
        def __init__(self):
            self.events = [
                SimpleNamespace(type="response.output_text.delta", delta="Привет"),
                SimpleNamespace(
                    type="response.function_call_arguments.done",
                    call_id="call-1",
                    name="clock",
                    arguments='{"zone":"Europe/Tallinn"}',
                ),
                SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(
                        usage=SimpleNamespace(input_tokens=12, output_tokens=4)
                    ),
                ),
            ]

        def __aiter__(self):
            self._iterator = iter(self.events)
            return self

        async def __anext__(self):
            try:
                return next(self._iterator)
            except StopIteration:
                raise StopAsyncIteration from None

    class Responses:
        async def create(self, **payload):
            self.payload = payload
            return FakeStream()

    responses = Responses()
    client = OpenAIClient(
        OpenAIConfig(model="gpt-4.1-mini"), "test-key", client=SimpleNamespace(responses=responses)
    )
    chunks = asyncio.run(
        _collect(
            client.chat_stream(
                [{"role": "user", "content": "Привет"}],
                tools=[
                    {
                        "type": "function",
                        "function": {"name": "clock", "parameters": {"type": "object"}},
                    }
                ],
            )
        )
    )
    assert [chunk.content for chunk in chunks] == ["Привет", ""]
    assert chunks[-1].tool_calls == [
        {"id": "call-1", "function": {"name": "clock", "arguments": {"zone": "Europe/Tallinn"}}}
    ]
    assert chunks[-1].stats["prompt_eval_count"] == 12
    assert responses.payload["stream"] is True
    assert responses.payload["parallel_tool_calls"] is False


async def _collect(stream):
    return [chunk async for chunk in stream]


class FakeSecurity:
    kSecClass = "class"
    kSecClassGenericPassword = "generic"
    kSecAttrService = "service"
    kSecAttrAccount = "account"
    kSecReturnAttributes = "attributes"
    kSecUseAuthenticationUI = "ui"
    kSecUseAuthenticationUIFail = "fail"
    kSecValueData = "data"
    kSecAttrLabel = "label"
    kSecReturnData = "return_data"
    errSecSuccess = 0
    errSecItemNotFound = -1
    errSecInteractionNotAllowed = -2

    def __init__(self):
        self.item = None

    def SecItemCopyMatching(self, query, _):
        if self.item is None:
            return self.errSecItemNotFound, None
        return self.errSecSuccess, self.item if query.get(self.kSecReturnData) else {"ok": True}

    def SecItemUpdate(self, _query, value):
        if self.item is None:
            return self.errSecItemNotFound
        self.item = value[self.kSecValueData]
        return self.errSecSuccess

    def SecItemAdd(self, value, _):
        self.item = value[self.kSecValueData]
        return self.errSecSuccess, None

    def SecItemDelete(self, _query):
        self.item = None
        return self.errSecSuccess


def test_keychain_saves_reads_and_deletes_without_exposing_secret_in_status():
    security = FakeSecurity()
    keychain = OpenAIKeychain(security=security)
    keychain.save("sk-" + "a" * 24)
    assert keychain.read() == "sk-" + "a" * 24
    assert keychain.status() == {"configured": True, "state": "saved", "storage": "keychain"}
    keychain.save("")
    assert keychain.status()["configured"] is False


def test_cost_unknown_stays_unknown_and_known_rates_are_estimated(tmp_path):
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps(
            {
                "prices": {
                    "gpt-4.1-mini": {"input_per_token": 0.0000004, "output_per_token": 0.0000016}
                }
            }
        )
    )
    assert estimate("gpt-4.1-mini", 1000, 1000, path) == 0.002
    assert estimate("unpriced-model", 1000, 1000, path) is None
    assert estimate("gpt-4.1-mini", None, 1000, path) is None


def test_openai_fallback_never_replays_after_tool_result():
    class Local:
        cloud_context = False

        def __init__(self):
            self.called = False

        async def chat_stream(self, messages, tools=None):
            self.called = True
            yield None

        async def aclose(self):
            pass

    primary = OpenAIClient(OpenAIConfig(fallback=True), "test-key")

    async def fail(messages, tools=None):
        raise OpenAIRequestError(503, transient=True, reason="temporary")
        yield

    primary.chat_stream = fail
    local = Local()
    fallback = FallbackClient(primary, local, enabled=True)

    async def run():
        try:
            async for _ in fallback.chat_stream(
                [
                    {"role": "user", "content": "Turn on the light"},
                    {"role": "tool", "tool_name": "device_command", "content": "sent"},
                ]
            ):
                pass
        except Exception as exc:
            assert "после вызова инструмента" in str(exc)
        else:
            raise AssertionError("Expected no automatic retry after tool execution")

    asyncio.run(run())
    assert local.called is False
