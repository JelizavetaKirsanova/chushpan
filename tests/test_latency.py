import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import types

from backend.brain.conversation import Conversation
from backend.brain.llm import LLMError
from backend.brain.providers import FallbackClient, GeminiClient, GeminiRequestError
from backend.config import GeminiConfig, Settings
from backend.latency import Profile, current, measure
from backend.local_commands import route
from backend.memory.store import MemoryStore
from backend.tools.clock import clock_tools
from backend.tools.registry import Tool, ToolRegistry


@pytest.mark.parametrize(
    "text,name,args",
    [
        ("Чушпан. Сколько времени?", "current_time", {}),
        ("Какая громкость?", "get_system_status", {"topic": "volume"}),
        ("Сделай громкость Mac 30%", "set_volume", {"level": 30}),
        ("Установи яркость экрана 25%", "set_brightness", {"level": 25}),
    ],
)
def test_local_routes_remain_guarded_without_model(text, name, args):
    assert route(text) == (name, args)


@pytest.mark.parametrize(
    "text",
    [
        "Не сделай громкость 30%",
        "Он сказал: сколько времени",
        "Покажи яркость и выключи Mac",
        "Сделай громкость 120%",
    ],
)
def test_quoted_negative_and_compound_commands_are_not_local_actions(text):
    assert route(text) is None


def test_local_time_never_contacts_model_and_records_no_content():
    async def scenario():
        settings = Settings(language="ru-RU")
        store = MemoryStore(":memory:")
        model = SimpleNamespace(
            chat_stream=AsyncMock(side_effect=AssertionError("network forbidden"))
        )
        profile = Profile()
        token = current.set(profile)
        try:
            conv = Conversation(settings, model, store, tools=ToolRegistry(clock_tools(settings)))
            answer = "".join([text async for text in conv.reply("Сколько времени?")])
            assert answer.startswith("Сейчас ")
            model.chat_stream.assert_not_called()
            assert "local_execution" in profile.totals()
            assert answer not in json.dumps(profile.stages)
        finally:
            current.reset(token)
            store.close()

    asyncio.run(scenario())


def test_local_route_does_not_bypass_tool_intent_guard():
    async def scenario():
        settings = Settings(language="ru-RU")
        handler = AsyncMock()
        tool = Tool(
            "set_volume",
            "test",
            handler,
            {"level": {"type": "integer"}},
            intent=lambda args, text: False,
        )
        store = MemoryStore(":memory:")
        try:
            conv = Conversation(settings, SimpleNamespace(), store, tools=ToolRegistry([tool]))
            answer = "".join([text async for text in conv.reply("Сделай громкость 30%")])
            assert "явная просьба" in answer
            handler.assert_not_called()
        finally:
            store.close()

    asyncio.run(scenario())


def test_stream_yields_before_completion_and_never_retries_after_partial_output():
    async def scenario():
        async def stream(**kwargs):
            async def chunks():
                yield types.GenerateContentResponse(
                    candidates=[
                        types.Candidate(content=types.Content(parts=[types.Part(text="Привет.")]))
                    ]
                )
                raise GeminiRequestError(503, transient=True, reason="test")

            return chunks()

        sdk = SimpleNamespace(models=SimpleNamespace(generate_content_stream=stream))
        primary = GeminiClient(GeminiConfig(free_tier_confirmed=True), "secret-test-key", sdk)
        local = SimpleNamespace(
            chat_stream=AsyncMock(side_effect=AssertionError("fallback forbidden"))
        )
        client = FallbackClient(primary, local)
        iterator = client.chat_stream([{"role": "user", "content": "Привет"}], tools=[])
        assert (await anext(iterator)).content == "Привет."
        with pytest.raises(GeminiRequestError):
            await anext(iterator)
        local.chat_stream.assert_not_called()

    asyncio.run(scenario())


def test_shared_cloud_budget_skips_second_full_timeout(monkeypatch):
    from backend.brain import providers

    async def scenario():
        now = [0.0]
        monkeypatch.setattr(providers.time, "perf_counter", lambda: now[0])
        cfg = GeminiConfig(free_tier_confirmed=True, timeout_s=5)
        primary = GeminiClient(cfg, "key")
        secondary = GeminiClient(cfg.model_copy(update={"model": "gemini-3.5-flash"}), "key")

        async def failing(*args):
            now[0] = 5.1
            raise GeminiRequestError(None, transient=True, reason="test")
            yield

        primary.chat_stream = failing
        secondary.chat_stream = AsyncMock(side_effect=AssertionError("second wait forbidden"))
        from tests.test_providers import Local

        local = Local()
        result = [
            chunk
            async for chunk in FallbackClient(primary, local, secondary=secondary).chat_stream([])
        ]
        assert result[0].content
        secondary.chat_stream.assert_not_called()
        assert len(local.calls) == 1

    asyncio.run(scenario())


def test_profile_records_failed_stage_without_exception_text():
    profile = Profile()
    token = current.set(profile)
    try:
        with pytest.raises(LLMError):
            with measure("context_memory"):
                raise LLMError("private prompt")
        assert profile.stages[0]["outcome"] == "error"
        assert "private" not in json.dumps(profile.stages)
    finally:
        current.reset(token)


def test_recent_outage_does_not_repeat_cloud_wait_on_next_request():
    from tests.test_providers import Local, gemini

    async def scenario():
        primary, method = gemini(error=GeminiRequestError(503, transient=True, reason="test"))
        local = Local()
        client = FallbackClient(primary, local)
        for _ in range(2):
            assert [chunk async for chunk in client.chat_stream([])]
        assert method.await_count == 1
        assert len(local.calls) == 2
        client._cooldown_until[primary] = 0
        assert [chunk async for chunk in client.chat_stream([])]
        assert method.await_count == 2

    asyncio.run(scenario())


def test_voice_stats_include_vad_and_explicit_local_model_status():
    from tests.test_voice_loop import FakeConversation, FakeSpeaker, Harness

    async def scenario():
        h = Harness(text="Проверка", wake=False)
        h.loop.conv = FakeConversation("Готово.")
        h.loop.conv.s.language = "ru-RU"
        h.loop.speaker = FakeSpeaker()
        await h.frames(20, prob=0.9)
        await h.frames(35)
        await h.loop.task
        event = next(item for item in h.loop.bus.snapshot() if item["type"] == "stats")
        assert event["model_used"] is False
        assert event["first_token_s"] == 0
        assert event["response_s"] >= event["stages"]["vad_wait"]
        assert event["response_s"] >= event["stt_s"]
        assert "stt" in event["stages"]

    asyncio.run(scenario())


def test_slow_primary_has_bounded_attempt_and_secondary_gets_time_to_answer():
    from tests.test_providers import Local, gemini, response

    async def scenario():
        primary, method = gemini()
        primary.cfg = primary.cfg.model_copy(update={"attempt_timeout_s": 0.01})

        async def slow(**kwargs):
            await asyncio.sleep(1)
            return response("Слишком поздно")

        method.side_effect = slow
        secondary, _ = gemini([response("Быстрый ответ")])
        secondary.cfg = secondary.cfg.model_copy(update={"model": "gemini-3.5-flash"})
        local = Local()
        chunks = [
            chunk
            async for chunk in FallbackClient(primary, local, secondary=secondary).chat_stream([])
        ]
        assert chunks[0].content == "Быстрый ответ"
        assert not local.calls

    asyncio.run(scenario())


def test_responsive_stream_is_not_cut_by_first_chunk_deadline():
    from tests.test_providers import Local, response

    async def scenario():
        async def stream(**kwargs):
            async def chunks():
                yield response("Начало. ")
                await asyncio.sleep(0.03)
                final = response("Продолжение.")
                final.candidates[0].finish_reason = types.FinishReason.STOP
                yield final

            return chunks()

        sdk = SimpleNamespace(
            models=SimpleNamespace(generate_content_stream=stream), aclose=AsyncMock()
        )
        config = GeminiConfig(free_tier_confirmed=True).model_copy(
            update={"attempt_timeout_s": 0.01}
        )
        primary, local = GeminiClient(config, "test", sdk), Local()
        chunks = [chunk async for chunk in FallbackClient(primary, local).chat_stream([], tools=[])]
        assert "".join(chunk.content for chunk in chunks) == "Начало. Продолжение."
        assert not local.calls

    asyncio.run(scenario())
