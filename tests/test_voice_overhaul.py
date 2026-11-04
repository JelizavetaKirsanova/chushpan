"""Completion, backpressure and attribution contracts; no network or audio devices."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from google.genai import types

from backend.audio.tts import synthesis_chunks
from backend.brain.conversation import Conversation
from backend.brain.llm import ChatChunk, LLMError, OllamaClient
from backend.brain.providers import FallbackClient, GeminiClient
from backend.config import GeminiConfig, LLMConfig, Settings
from backend.latency import Profile, consumer_wait, current
from backend.memory.store import MemoryStore
from backend.settings_models import apply_persisted
from backend.tools import Tool, ToolRegistry


def cloud(stream, **cfg):
    async def generate(**kwargs):
        return stream()

    return GeminiClient(
        GeminiConfig(free_tier_confirmed=True).model_copy(update=cfg),
        "test",
        SimpleNamespace(models=SimpleNamespace(generate_content_stream=generate)),
    )


def response(text="", *, stop=False):
    return types.GenerateContentResponse(
        model_version="gemini-3.8-flash",
        candidates=[
            types.Candidate(
                content=types.Content(parts=[types.Part(text=text)]),
                finish_reason=types.FinishReason.STOP if stop else None,
            )
        ],
    )


class Lines(httpx.AsyncByteStream):
    def __init__(self, rows, delay=0):
        self.rows, self.delay = rows, delay

    async def __aiter__(self):
        for row in self.rows:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield (json.dumps(row) + "\n").encode()


@pytest.mark.parametrize("provider", ["gemini", "ollama"])
def test_truncated_text_stream_is_an_error_and_never_a_completed_answer(provider):
    async def run():
        async def partial():
            yield response("Начало ответа. ")

        client = (
            cloud(partial)
            if provider == "gemini"
            else OllamaClient(
                LLMConfig(),
                httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda _: httpx.Response(
                            200, stream=Lines([{"message": {"content": "Начало."}}])
                        )
                    ),
                    base_url="http://test",
                ),
            )
        )
        try:
            with pytest.raises(LLMError):
                async for _ in client.chat_stream([{"role": "user", "content": "Привет"}], []):
                    pass
        finally:
            if provider == "ollama":
                await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["gemini", "ollama"])
def test_slow_tts_consumer_does_not_spend_network_idle_timeout(provider):
    async def run():
        async def complete():
            yield response("Первое предложение. ")
            yield response("Последнее предложение.", stop=True)

        if provider == "gemini":
            client = cloud(complete, timeout_s=0.01, stream_idle_timeout_s=0.01)
        else:
            client = OllamaClient(
                LLMConfig().model_copy(
                    update={"first_result_timeout_s": 0.01, "stream_idle_timeout_s": 0.01}
                ),
                httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda _: httpx.Response(
                            200,
                            stream=Lines(
                                [
                                    {"message": {"content": "Первое предложение. "}},
                                    {
                                        "message": {"content": "Последнее предложение."},
                                        "done": True,
                                    },
                                ]
                            ),
                        )
                    ),
                    base_url="http://test",
                ),
            )
        try:
            chunks = []
            async for chunk in client.chat_stream([{"role": "user", "content": "Привет"}], []):
                chunks.append(chunk)
                await asyncio.sleep(0.025)
            assert chunks[-1].done
            assert "Последнее предложение." in "".join(c.content for c in chunks)
        finally:
            if provider == "ollama":
                await client.aclose()

    asyncio.run(run())


def test_thinking_without_usable_local_output_cannot_wait_forever():
    async def run():
        client = OllamaClient(
            LLMConfig().model_copy(update={"first_result_timeout_s": 0.015}),
            httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200,
                        stream=Lines([{"message": {"thinking": "reasoning"}}] * 100, delay=0.003),
                    )
                ),
                base_url="http://test",
            ),
        )
        try:
            with pytest.raises(LLMError, match="вовремя"):
                async for _ in client.chat_stream(
                    [{"role": "user", "content": "Докажи теорему"}], []
                ):
                    pass
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "text,level",
    [
        ("Что такое радуга?", "low"),
        ("Докажи теорему и приведи источники", None),
    ],
)
def test_gemini_reasoning_keeps_model_default_for_complex_requests(text, level):
    async def run():
        generate = AsyncMock(
            return_value=types.GenerateContentResponse(
                candidates=[
                    types.Candidate(content=types.Content(parts=[types.Part(text="Ответ.")]))
                ]
            )
        )
        client = GeminiClient(
            GeminiConfig(free_tier_confirmed=True),
            "test",
            SimpleNamespace(models=SimpleNamespace(generate_content=generate)),
        )
        async for _ in client.chat_stream([{"role": "user", "content": text}], []):
            pass
        cfg = generate.await_args.kwargs["config"]
        assert (
            cfg.thinking_config.thinking_level.value.lower() if cfg.thinking_config else None
        ) == level
        assert cfg.temperature is None

    asyncio.run(run())


def test_disabled_cloud_streaming_keeps_all_text_until_completion():
    async def run():
        async def complete():
            yield response("Первое. ")
            yield response("Последнее.", stop=True)

        chunks = [
            c
            async for c in cloud(complete, streaming=False).chat_stream(
                [{"role": "user", "content": "Привет"}], []
            )
        ]
        assert len(chunks) == 1 and chunks[0].done
        assert chunks[0].content == "Первое. Последнее."

    asyncio.run(run())


def test_network_timeout_pause_also_applies_to_fallback_wrapper():
    async def run():
        async def complete():
            yield response("Первое. ")
            yield response("Последнее.", stop=True)

        client = FallbackClient(
            cloud(complete, stream_idle_timeout_s=0.01), SimpleNamespace(), enabled=False
        )
        async for _ in client.chat_stream([{"role": "user", "content": "Привет"}], []):
            await asyncio.sleep(0.025)

    asyncio.run(run())


def test_generic_failed_tool_cannot_be_narrated_as_a_success():
    async def handler():
        return {"ok": False}

    registry = ToolRegistry([Tool("example", "example", handler)])
    assert "не выполнено" in registry.direct_reply("example", {"ok": False})
    assert registry.direct_reply("example", {"error": "Недоступно"}) is None


def test_failed_action_result_blocks_model_success_claim_and_preserves_tool_history():
    async def run():
        async def handler():
            return {"error": "Недоступно"}

        class Model:
            requests = 0

            async def chat_stream(self, *args, **kwargs):
                self.requests += 1
                if self.requests == 1:
                    yield ChatChunk(
                        tool_calls=[{"function": {"name": "launch_example", "arguments": {}}}],
                        done=True,
                    )
                else:
                    yield ChatChunk(content="Готово, приложение открыто.", done=True)

        store, model = MemoryStore(":memory:"), Model()
        conv = Conversation(
            Settings(language="ru-RU"),
            model,
            store,
            tools=ToolRegistry([Tool("launch_example", "example", handler)]),
        )
        try:
            answer = "".join([s async for s in conv.reply("Открой Spotify")])
            assert "Готово" not in answer and "не подтвердил" in answer
            assert model.requests == 2
            messages = store.recent(conv.id, 20)
            assert any(m.get("role") == "tool" for m in messages)
            assert not any("Готово" in m.get("content", "") for m in messages)
        finally:
            store.close()

    asyncio.run(run())


def test_provider_work_time_excludes_tts_backpressure(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("backend.latency.time.perf_counter", lambda: now[0])
    profile = Profile(origin=100)
    token = current.set(profile)
    try:
        span = profile.begin("model_total")
        now[0] = 101
        with consumer_wait(span):
            now[0] = 106
        now[0] = 108
        profile.end(span)
        assert profile.stages[0]["seconds"] == 8
        assert profile.stages[0]["consumer_wait_s"] == 5
        assert profile.summary()["llm_total_s"] == 3
    finally:
        current.reset(token)


def test_long_unbroken_word_is_never_cut_in_half_for_synthesis():
    word = "А" * 600
    text = word + " " + "слово " * 200
    chunks = synthesis_chunks(text)
    assert chunks[0] == word
    assert " ".join(chunks).split() == text.split()


def test_stage_relations_are_separate_from_model_latency(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("backend.latency.time.perf_counter", lambda: now[0])
    profile = Profile(kind="voice", origin=100)
    for name, offset in [
        ("user_speech_end", 0),
        ("stt_completed", 1),
        ("first_llm_token", 3),
        ("first_content_audio", 4),
    ]:
        profile.mark(name, 100 + offset)
    now[0] = 109
    profile.finish()
    assert profile.snapshot()["metrics"] == {
        "speech_end_to_stt_s": 1,
        "stt_to_first_token_s": 2,
        "first_token_to_audio_s": 1,
        "speech_end_to_audio_s": 4,
        "speech_end_to_completed_s": 9,
    }


def test_history_attributes_failed_primary_and_fallback_separately_and_filters_all_rows(
    monkeypatch,
):
    from backend.statistics import period_range

    anchor = datetime(2026, 11, 5, 10, tzinfo=UTC)
    monkeypatch.setattr(
        "backend.performance.period_range",
        lambda days, timezone, filters: period_range(days, timezone, filters, now=anchor),
    )
    store = MemoryStore(":memory:")
    try:
        for _ in range(105):
            profile = Profile(kind="voice")
            profile._wall_time = anchor - timedelta(minutes=5)
            profile.update(
                model="qwen3:8b",
                provider="ollama",
                input_mode="microphone",
                request_class="routine",
            )
            store.performance.attach(profile)
            first = profile.begin("llm_attempt", model="gemini-3.8-flash", provider="gemini")
            profile.end(first, "error")
            second = profile.begin(
                "llm_attempt", model="qwen3:8b", provider="ollama", fallback=True
            )
            profile.end_first(second)
            profile.end(second)
            profile.mark("first_content_audio")
            profile.finish()
        old = Profile(kind="text")
        old._wall_time = anchor - timedelta(hours=2)
        store.performance.attach(old)
        old.finish()
        result = store.performance.statistics(0, "Europe/Tallinn", {"model": "qwen3:8b"})
        assert result["summary"]["count"] == 105
        assert len(result["series"]) == 105 and len(result["requests"]) == 100
        models = {m["model"]: m for m in result["models"]}
        assert models["gemini-3.8-flash"]["first_token"]["failed"] == 105
        assert models["qwen3:8b"]["fallbacks"] == 105
        assert models["qwen3:8b"]["first_token"]["measured"] == 105
        assert (
            store.performance.statistics(0, "Europe/Tallinn", {"input_mode": "replay"})["summary"][
                "count"
            ]
            == 0
        )
        assert store.performance.statistics(1, "Europe/Tallinn")["summary"]["count"] == 106
    finally:
        store.close()


def test_persisted_advanced_settings_apply_without_changing_chosen_model():
    settings, store = Settings(), MemoryStore(":memory:")
    try:
        selected = {
            "provider": "gemini",
            "cloud_model": "gemini-3.8-flash",
            "streaming": False,
            "thinking": "on",
            "cloud_thinking": "high",
            "cloud_timeout_s": 25,
            "local_first_result_s": 40,
            "cloud_idle_s": 10,
        }
        store.set_preference("settings", "models", selected)
        apply_persisted(settings, store)
        assert settings.gemini.model == "gemini-3.8-flash"
        assert settings.gemini.thinking_level == "high" and settings.llm.think_for_simple
        assert settings.gemini.timeout_s == 25 and settings.llm.first_result_timeout_s == 40
        assert not settings.llm.streaming and not settings.gemini.streaming
    finally:
        store.close()
