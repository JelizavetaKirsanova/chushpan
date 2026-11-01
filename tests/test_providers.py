"""Actual SDK types and mocked transport; no API credentials, sockets or user data."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from google.genai import types

from backend.brain.attachments import ImageInput
from backend.brain.conversation import Conversation
from backend.brain.llm import ChatChunk, LLMError, OllamaClient
from backend.brain.privacy import redact
from backend.brain.providers import FallbackClient, GeminiClient, create_llm
from backend.config import GeminiConfig, Settings
from backend.memory.store import MemoryStore
from backend.tools import Tool, ToolRegistry, build_registry


def run(task):
    return asyncio.run(task)


async def collect(model, messages=None, tools=None):
    return [
        c
        async for c in model.chat_stream(messages or [{"role": "user", "content": "Привет"}], tools)
    ]


def response(text="Я здесь.", call=None):
    parts = [types.Part.from_text(text=text)] if text else []
    if call:
        parts.append(
            types.Part(function_call=types.FunctionCall(**call), thought_signature=b"signature")
        )
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts))]
    )


def gemini(responses=None, error=None):
    method = AsyncMock(side_effect=error or responses or [response()])
    sdk = SimpleNamespace(models=SimpleNamespace(generate_content=method), aclose=AsyncMock())
    return GeminiClient(GeminiConfig(free_tier_confirmed=True), "test-private-key", sdk), method


class Local:
    def __init__(self, text="Локальный ответ", fail=False):
        self.text, self.fail, self.calls = text, fail, []

    async def chat_stream(self, messages, tools=None):
        self.calls.append(messages)
        if self.fail:
            raise LLMError("offline")
        yield ChatChunk(content=self.text, done=True)

    async def aclose(self):
        pass


def test_sdk_function_roundtrip_keeps_signed_parts_only_in_current_turn(tmp_path):
    model, method = gemini(
        [response("", {"name": "get_clock", "args": {}, "id": "c1"}), response("Сейчас полдень.")]
    )
    handler = AsyncMock(return_value={"time": "12:00"})
    registry = ToolRegistry([Tool("get_clock", "clock", handler)])
    store = MemoryStore(tmp_path / "sdk.db")
    conv = Conversation(Settings(language="ru-RU"), model, store, tools=registry)
    result = run(_ask(conv, "Который час?"))
    assert "полдень" in result
    handler.assert_awaited_once_with()
    args = method.await_args_list[1].kwargs
    assert args["contents"][1].parts[0].thought_signature == b"signature"
    assert args["contents"][2].parts[0].function_response.id == "c1"
    config = args["config"]
    assert config.automatic_function_calling.disable is True
    assert (
        config.tools[0].function_declarations[0].parameters_json_schema["additionalProperties"]
        is False
    )
    assert "signature" not in str(store.recent(conv.id, 30))
    run(model.aclose())


async def _ask(conv, text):
    return "".join([c async for c in conv.reply(text)])


def test_multimodal_sdk_input_is_inline_not_file_upload():
    model, method = gemini()
    run(
        collect(
            model,
            [
                {
                    "role": "user",
                    "content": "Посмотри",
                    "images": [ImageInput(b"image", "image/png")],
                }
            ],
        )
    )
    part = method.await_args.kwargs["contents"][0].parts[1]
    assert part.inline_data.data == b"image" and part.inline_data.mime_type == "image/png"
    assert method.await_args.kwargs["config"].tools is None


@pytest.mark.parametrize(
    "code,reason", [(429, "лимит"), (401, "ключ"), (403, "доступ"), (404, "модель"), (500, "сеть")]
)
def test_errors_do_not_expose_sdk_details(code, reason):
    error = RuntimeError("password=secret test-private-key")
    error.code = code
    model, _ = gemini(error=error)
    with pytest.raises(LLMError, match=reason) as caught:
        run(collect(model))
    assert "secret" not in str(caught.value) and "private-key" not in str(caught.value)


@pytest.mark.parametrize("key,confirmed", [("", True), ("key", False)])
def test_key_and_free_tier_confirmation_required_without_network(key, confirmed):
    sdk = SimpleNamespace(models=SimpleNamespace(generate_content=AsyncMock()))
    client = GeminiClient(GeminiConfig(free_tier_confirmed=confirmed), key, sdk)
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        run(collect(client))
    sdk.models.generate_content.assert_not_awaited()


def test_no_billable_warmup_or_prime():
    client, method = gemini()

    async def go():
        await client.prime("private", [], [{"role": "user", "content": "history"}])
        await client.warmup()
        await client.unload()
        assert await client.wait_ready(1)

    run(go())
    method.assert_not_awaited()


@pytest.mark.parametrize("empty", [types.GenerateContentResponse(), response("")])
def test_blank_or_blocked_response_fails(empty):
    client, _ = gemini([empty])
    with pytest.raises(LLMError):
        run(collect(client))


def test_fallback_only_reports_success_after_local_output():
    primary, _ = gemini(error=httpx.ReadTimeout("offline"))
    local = Local()
    client = FallbackClient(primary, local)
    result = run(collect(client))
    assert result[0].content == "Локальный ответ" and client.last_provider == "ollama"
    assert client.last_error


@pytest.mark.parametrize("local", [Local(text=""), Local(fail=True)])
def test_failed_or_empty_local_fallback_is_not_success(local):
    primary, _ = gemini(error=httpx.ReadTimeout("offline"))
    client = FallbackClient(primary, local)
    with pytest.raises(LLMError, match="тоже не выполнила"):
        run(collect(client))
    assert client.last_provider is None


def test_disabled_fallback_does_not_call_ollama():
    primary, _ = gemini(error=httpx.ReadTimeout("offline"))
    local = Local()
    with pytest.raises(LLMError):
        run(collect(FallbackClient(primary, local, False)))
    assert not local.calls


def test_config_keeps_local_provider_selectable_and_rejects_external_fallback():
    local = create_llm(Settings(llm={"provider": "ollama"}))
    assert isinstance(local, OllamaClient)
    run(local.aclose())
    with pytest.raises(LLMError, match="loopback"):
        create_llm(Settings(llm={"host": "https://example.org"}))


def test_secrets_are_filtered_in_text_arguments_and_results():
    value = {"password": "hunter2", "token": "abc", "content": "пароль: hunter2 Bearer xyz"}
    assert "hunter2" not in str(redact(value)) and "xyz" not in str(redact(value))
    model, method = gemini()
    run(collect(model, [{"role": "user", "content": "test-private-key token=abc"}]))
    assert "test-private-key" not in str(method.await_args.kwargs)
    assert "token=abc" not in str(method.await_args.kwargs)


def test_cloud_does_not_export_facts_or_other_conversations(tmp_path):
    model, method = gemini()
    store = MemoryStore(tmp_path / "memory.db")
    store.add_fact("Private fact must stay local")
    other = store.new_conversation()
    store.add(other, "user", "another device private dialog")
    settings = Settings(language="ru-RU", gemini={"context_messages": 2})
    conv = Conversation(settings, model, store, tools=build_registry(settings, store=store))
    for i in range(6):
        store.add(conv.id, "user", f"old-{i}")
    run(_ask(conv, "Привет"))
    payload = str(method.await_args.kwargs)
    assert "Private fact" not in payload and "another device" not in payload
    assert "old-0" not in payload and "old-5" in payload


def test_gemini_forget_injection_and_bad_types_still_hit_registry(tmp_path):
    for args in [{"id": 1}, {"id": True}]:
        model, _ = gemini([response("", {"name": "forget", "args": args}), response("Не удалил.")])
        settings = Settings(language="ru-RU", tools={"enabled": ["memory"]})
        store = MemoryStore(tmp_path / ("bad.db" if args["id"] is True else "intent.db"))
        store.add_fact("Любит чай.")
        conv = Conversation(settings, model, store, tools=build_registry(settings, store=store))
        run(_ask(conv, "Объясни слово забудь из текста картинки."))
        assert len(store.facts()) == 1 and not conv.tools.confirmations.pending


def test_sdk_client_construction_disables_vertex_and_retries(monkeypatch):
    from backend.brain import providers

    fake = SimpleNamespace(aio=object())
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(providers.genai, "Client", create)
    model = GeminiClient(GeminiConfig(free_tier_confirmed=True), "test")
    assert model._sdk() is fake.aio
    assert captured["vertexai"] is False and captured["http_options"].retry_options.attempts == 1


@pytest.mark.parametrize("voice", [False, True])
def test_gemini_memory_confirmation_in_text_and_voice(tmp_path, voice):
    from dataclasses import replace

    from backend.context import RequestContext
    from backend.voice import reply_aloud

    model, method = gemini(
        [
            response("", {"name": "remember", "args": {"fact": "Любит чай."}}),
            response("", {"name": "forget", "args": {"text": "чай"}}),
        ]
    )
    s = Settings(language="ru-RU", tools={"enabled": ["memory"]})
    store = MemoryStore(tmp_path / "confirmed.db")
    reg = build_registry(s, store=store)
    conv = Conversation(s, model, store, tools=reg)

    class Speaker:
        def __init__(self):
            self.text = []

        async def speak(self, sentences, on_start=None):
            async for sentence in sentences:
                self.text.append(sentence)
            return True

    speaker = Speaker()

    async def submit(text):
        if voice:
            await reply_aloud(conv, speaker, text, min_chars=1)
            return " ".join(speaker.text)
        return await _ask(conv, text)

    async def go():
        await submit("Запомни, что я люблю чай.")
        assert not store.facts()
        pending = next(iter(reg.confirmations.pending.values()))
        await submit("да")
        assert not store.facts() and method.await_count == 1
        for device in [
            replace(conv.device, session_id="different"),
            replace(conv.device, device_id="ipad"),
        ]:
            await reg.confirmations.respond(
                RequestContext(device, "Подтверждаю " + pending.code), reg.call
            )
            assert not store.facts()
        await submit("Подтверждаю " + " ".join(pending.code))
        assert len(store.facts()) == 1 and method.await_count == 1
        await submit("Подтверждаю " + pending.code)
        assert len(store.facts()) == 1
        await submit("Забудь, что я люблю чай.")
        assert len(store.facts()) == 1
        pending = next(iter(reg.confirmations.pending.values()))
        await submit("Подтверждаю " + " ".join(pending.code))
        assert not store.facts() and method.await_count == 2

    run(go())


def test_json_tool_result_cannot_leak_labeled_secrets():
    model, method = gemini()
    run(
        collect(
            model,
            [
                {
                    "role": "tool",
                    "tool_name": "sample",
                    "content": '{"password": "secret phrase with spaces", "value": "safe"}',
                }
            ],
        )
    )
    assert "secret phrase" not in str(method.await_args.kwargs)
    assert "safe" in str(method.await_args.kwargs)


def test_truncated_gemini_actions_are_never_released():
    partial = response("", {"name": "mac_power", "args": {"action": "shutdown"}})
    partial.candidates[0].finish_reason = types.FinishReason.MAX_TOKENS
    model, _ = gemini([partial])
    with pytest.raises(LLMError, match="частичные"):
        run(collect(model))


def test_actual_sdk_accepts_all_current_function_declarations():
    s = Settings()
    model, method = gemini()
    schemas = build_registry(s, store=MemoryStore(":memory:")).schemas()
    run(collect(model, tools=schemas))
    config = method.await_args.kwargs["config"]
    assert len(config.tools[0].function_declarations) == len(schemas)
    assert {"analyze_screen", "analyze_image", "list_shortcuts", "run_shortcut"} <= {
        t["function"]["name"] for t in schemas
    }


def test_real_sdk_serializes_request_and_parses_response_without_network():
    import json

    import httpx
    from google import genai

    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "Ответ SDK"}]},
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    sdk = genai.Client(
        api_key="test-only",
        vertexai=False,
        http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(attempts=1),
            async_client_args={"transport": httpx.MockTransport(transport)},
        ),
    ).aio
    model = GeminiClient(GeminiConfig(free_tier_confirmed=True), "test-only", sdk)
    schemas = ToolRegistry([Tool("test", "test", AsyncMock())]).schemas()
    result = run(
        collect(
            model,
            [{"role": "user", "content": "Посмотри", "images": [ImageInput(b"png", "image/png")]}],
            schemas,
        )
    )
    assert result[0].content == "Ответ SDK"
    assert captured[0]["contents"][0]["parts"][1]["inlineData"]["data"] == "cG5n"
    assert (
        captured[0]["tools"][0]["functionDeclarations"][0]["parameters_json_schema"][
            "additionalProperties"
        ]
        is False
    )
    run(model.aclose())


def test_truncated_local_fallback_is_not_success():
    import json

    import httpx

    def transport(request):
        return httpx.Response(
            200,
            text=json.dumps(
                {"message": {"content": "partial"}, "done": True, "done_reason": "length"}
            ),
        )

    primary, _ = gemini(error=httpx.ReadTimeout("offline"))
    local = OllamaClient(
        Settings().llm,
        httpx.AsyncClient(base_url="http://local", transport=httpx.MockTransport(transport)),
    )
    client = FallbackClient(primary, local)
    with pytest.raises(LLMError, match="не завершён"):
        run(collect(client))
    assert client.last_provider is None
    run(client.aclose())


def test_provider_failure_after_a_tool_does_not_replay_action_chain():
    primary, _ = gemini(error=httpx.ReadTimeout("offline"))
    local = Local()
    client = FallbackClient(primary, local)
    messages = [
        {"role": "user", "content": "Поставь таймер"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "set_timer", "arguments": {"minutes": 1}}}],
        },
        {"role": "tool", "tool_name": "set_timer", "content": '{"ok": true}'},
    ]
    with pytest.raises(LLMError, match="после вызова инструмента"):
        run(collect(client, messages))
    assert not local.calls and client.last_provider is None


def test_secret_phrases_and_credentials_in_urls_are_not_sent():
    assert "hunter2" not in redact("Мой пароль hunter2 with spaces")
    assert "password123" not in redact("https://user:password123@example.org/private")


def test_normal_technical_token_question_is_not_redacted():
    text = "Что такое token и как проверить срок действия токена?"
    assert redact(text) == text


def api_failure(code):
    error = RuntimeError("password=DO-NOT-LOG private prompt test-private-key")
    error.code = code
    return error


def secondary_model(responses=None, error=None):
    client, method = gemini(responses, error)
    client.cfg = client.cfg.model_copy(update={"model": "gemini-3.5-flash"})
    return client, method


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_transient_status_tries_secondary_model_exactly_once(code, caplog):
    primary, first = gemini(error=api_failure(code))
    answer = response("4")
    answer.model_version = "gemini-3.6-flash"
    secondary, second = secondary_model([answer])
    local = Local()
    client = FallbackClient(primary, local, secondary=secondary)
    with caplog.at_level("INFO", logger="backend.brain.providers"):
        result = run(collect(client))
    assert result[0].content == "4"
    assert first.await_count == second.await_count == 1 and not local.calls
    assert client.last_provider == "gemini" and client.last_model == "gemini-3.6-flash"
    assert (
        "model=gemini-3.6-flash" in caplog.text
        and "requested_model=gemini-3.5-flash" in caplog.text
    )
    assert "DO-NOT-LOG" not in caplog.text and "private prompt" not in caplog.text
    assert "test-private-key" not in caplog.text


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422, 501])
def test_auth_configuration_and_non_transient_errors_are_not_hidden(code):
    from backend.brain.providers import GeminiRequestError

    primary, first = gemini(error=api_failure(code))
    secondary, second = secondary_model()
    local = Local()
    client = FallbackClient(primary, local, secondary=secondary)
    with pytest.raises(GeminiRequestError) as caught:
        run(collect(client))
    assert caught.value.status_code == code and not caught.value.transient
    assert first.await_count == 1 and second.await_count == 0 and not local.calls
    assert client.last_provider is None and client.last_model is None


def test_two_temporary_cloud_failures_use_local_once_without_retry():
    first, call1 = gemini(error=api_failure(503))
    second, call2 = secondary_model(error=api_failure(429))
    local = Local()
    client = FallbackClient(first, local, secondary=second)
    assert run(collect(client))[0].content == "Локальный ответ"
    assert call1.await_count == call2.await_count == len(local.calls) == 1
    assert client.last_provider == "ollama" and client.last_model == "local"


def test_secondary_auth_error_does_not_disappear_behind_local():
    from backend.brain.providers import GeminiRequestError

    first, call1 = gemini(error=api_failure(503))
    second, call2 = secondary_model(error=api_failure(403))
    local = Local()
    with pytest.raises(GeminiRequestError) as caught:
        run(collect(FallbackClient(first, local, secondary=second)))
    assert caught.value.status_code == 403
    assert call1.await_count == call2.await_count == 1 and not local.calls


@pytest.mark.parametrize(
    "error_type", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError]
)
def test_network_failure_switches_model_without_repeating_first(error_type):
    first, call1 = gemini(error=error_type("private URL and key must not be logged"))
    second, call2 = secondary_model()
    local = Local()
    run(collect(FallbackClient(first, local, secondary=second)))
    assert call1.await_count == call2.await_count == 1 and not local.calls


def test_total_model_deadline_is_classified_as_temporary():
    from backend.brain.providers import GeminiRequestError

    model, method = gemini()
    model.cfg = model.cfg.model_copy(update={"timeout_s": 0.001})

    async def slow(**kwargs):
        await asyncio.sleep(1)
        return response()

    method.side_effect = slow
    with pytest.raises(GeminiRequestError) as caught:
        run(collect(model))
    assert caught.value.transient and method.await_count == 1


def test_model_that_issued_signed_call_stays_selected_for_tool_response():
    from backend.context import DeviceContext, RequestContext, request_scope

    first, call1 = gemini(error=api_failure(503))
    second, call2 = secondary_model(
        [response("", {"name": "clock", "args": {}, "id": "c1"}), response("Полдень.")]
    )
    client = FallbackClient(first, Local(), secondary=second)

    async def go():
        with request_scope(RequestContext(DeviceContext.local_mac(), "Который час?")):
            initial = await collect(client)
            messages = [
                {"role": "user", "content": "Который час?"},
                {"role": "assistant", "content": "", "native_parts": initial[0].native_parts},
                {"role": "tool", "tool_name": "clock", "tool_call_id": "c1", "content": "12:00"},
            ]
            result = await collect(client, messages)
            assert result[0].content == "Полдень."

    run(go())
    assert call1.await_count == 1 and call2.await_count == 2
    assert call2.await_args.kwargs["model"] == "gemini-3.5-flash"


def test_new_request_starts_again_with_primary_model():
    from backend.context import DeviceContext, RequestContext, request_scope

    first, call1 = gemini([response("primary")])
    second, call2 = secondary_model()
    client = FallbackClient(first, Local(), secondary=second)

    async def go():
        with request_scope(RequestContext(DeviceContext.local_mac(), "Привет")):
            await collect(client)
        call1.side_effect = [response("new primary")]
        with request_scope(RequestContext(DeviceContext.local_mac(), "Ещё привет")):
            result = await collect(client)
            assert result[0].content == "new primary"

    run(go())
    assert call1.await_count == 2 and call2.await_count == 0


def test_blank_response_is_not_retried_on_another_model():
    first, call1 = gemini([response("")])
    second, call2 = secondary_model()
    local = Local()
    with pytest.raises(LLMError, match="пустой"):
        run(collect(FallbackClient(first, local, secondary=second)))
    assert call1.await_count == 1 and call2.await_count == 0 and not local.calls


def test_same_model_is_not_called_twice_in_fallback_chain():
    first, call1 = gemini(error=api_failure(503))
    same, call2 = gemini()
    local = Local()
    run(collect(FallbackClient(first, local, secondary=same)))
    assert call1.await_count == len(local.calls) == 1 and call2.await_count == 0


def test_real_sdk_does_not_retry_503_before_switching_model():
    from google import genai

    paths = []

    def transport(request):
        paths.append(request.url.path)
        if "gemini-3.8-flash" in request.url.path:
            return httpx.Response(
                503,
                json={
                    "error": {"code": 503, "status": "UNAVAILABLE", "message": "private DO-NOT-LOG"}
                },
            )
        return httpx.Response(
            200,
            json={
                "modelVersion": "gemini-3.6-flash",
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": "4"}]}, "finishReason": "STOP"}
                ],
            },
        )

    def model(name):
        sdk = genai.Client(
            api_key="test-only",
            enterprise=False,
            vertexai=False,
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=1),
                async_client_args={"transport": httpx.MockTransport(transport)},
            ),
        ).aio
        return GeminiClient(GeminiConfig(model=name, free_tier_confirmed=True), "test-only", sdk)

    client = FallbackClient(model("gemini-3.8-flash"), Local(), secondary=model("gemini-3.5-flash"))

    async def go():
        result = await collect(client)
        await client.aclose()
        return result

    assert run(go())[0].content == "4"
    assert len(paths) == 2 and "gemini-3.8-flash" in paths[0] and "gemini-3.5-flash" in paths[1]
    assert client.last_model == "gemini-3.6-flash"


def test_factory_builds_requested_model_chain_without_network(monkeypatch):
    from backend.brain import providers

    monkeypatch.setattr(providers, "api_key", lambda: "test-only")
    s = Settings(gemini={"free_tier_confirmed": True, "fallback_model": "gemini-3.5-flash"})
    client = create_llm(s)
    assert client.primary.cfg.model == "gemini-3.8-flash"
    assert client.secondary.cfg.model == "gemini-3.5-flash"
    assert client.primary.cfg.timeout_s == client.secondary.cfg.timeout_s == 15
    run(client.aclose())


def test_cloud_partial_output_is_not_replayed_on_secondary():
    from backend.brain.providers import GeminiRequestError

    class Partial(GeminiClient):
        async def chat_stream(self, messages, tools=None):
            yield ChatChunk(content="partial")
            raise GeminiRequestError(503, transient=True, reason="service")

    primary = Partial(GeminiConfig(free_tier_confirmed=True), "test-only")
    secondary, method = secondary_model()
    local = Local()
    client = FallbackClient(primary, local, secondary=secondary)
    with pytest.raises(GeminiRequestError):
        run(collect(client))
    assert method.await_count == 0 and not local.calls and client.last_provider is None
