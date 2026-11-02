"""Timing/provenance regressions. No real models, audio devices, Bluetooth or network."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import types

from backend.brain.conversation import Conversation
from backend.brain.llm import ChatChunk, LLMError, OllamaClient
from backend.brain.providers import FallbackClient, GeminiClient
from backend.brain.response_mode import simple_request
from backend.config import GeminiConfig, LLMConfig, Settings
from backend.context import DeviceContext, RequestContext, request_scope
from backend.hud.bus import HudBus
from backend.latency import Profile, current, measure
from backend.memory.store import MemoryStore
from backend.performance import aggregate
from backend.tools.clock import clock_tools
from backend.tools.registry import ToolRegistry


def test_parallel_spans_use_elapsed_wall_time_not_sum(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("backend.latency.time.perf_counter", lambda: now[0])
    p = Profile(origin=0, kind="voice")
    a = p.begin("model_total")
    now[0] = 1
    b = p.begin("tts_generation")
    now[0] = 2
    p.end(a)
    p.mark("first_content_audio")
    now[0] = 3
    p.end(b)
    p.finish()
    row = p.snapshot()
    assert row["total_s"] == 3 and row["first_content_s"] == 2
    assert sum(x["seconds"] for x in row["stages"]) == 4
    assert row["stages"][0]["start_s"] == 0 and row["stages"][1]["start_s"] == 1
    p.mark("late_callback")
    assert "late_callback" not in p.marks


def test_cancelled_interval_is_not_success():
    p = Profile()
    token = current.set(p)
    try:
        with pytest.raises(asyncio.CancelledError):
            with measure("tts_generation"):
                raise asyncio.CancelledError()
        p.finish("cancelled")
        assert p.snapshot()["status"] == "cancelled"
        assert p.stages[0]["outcome"] == "cancelled"
        assert p.snapshot()["first_content_s"] is None
    finally:
        current.reset(token)


def test_summary_excludes_failures_and_missing_audio_instead_of_zero():
    rows = [{"status": "completed", "first_content_s": n} for n in [1.0, 2.0, 6.0, 11.0, 16.0]]
    rows += [
        {"status": "failed", "first_content_s": 0.1},
        {"status": "cancelled", "first_content_s": None},
        {"status": "completed", "first_content_s": None},
    ]
    result = aggregate(rows)
    assert result["count"] == 8 and result["measured"] == 5
    assert result["median_s"] == 6 and result["p95_s"] == 16
    assert result["over"] == {"5": 60.0, "10": 40.0, "15": 20.0}
    assert aggregate([])["mean_s"] is None


def test_trace_survives_restart_and_crash_stays_interrupted(tmp_path):
    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    p = Profile(kind="voice")
    store.performance.attach(p)
    p.mark("waiting_audio")
    assert p.snapshot()["first_content_s"] is None
    p.mark("first_content_audio")
    p.finish()
    pending = Profile()
    store.performance.attach(pending)
    store.close()
    reopened = MemoryStore(path)
    try:
        assert reopened.performance.detail(p.request_id)["status"] == "completed"
        assert reopened.performance.detail(pending.request_id)["status"] == "interrupted"
        assert reopened.performance.statistics(7, "Europe/Tallinn")["summary"]["count"] == 2
    finally:
        reopened.close()


def test_opening_second_store_does_not_mark_active_work_interrupted(tmp_path):
    path = tmp_path / "shared.db"
    first = MemoryStore(path)
    profile = Profile()
    first.performance.attach(profile)
    second = MemoryStore(path)
    try:
        assert first.performance.detail(profile.request_id)["status"] == "running"
        assert second.performance.detail(profile.request_id)["status"] == "unknown"
        assert first.db.execute("SELECT status FROM request_traces").fetchone()[0] == "running"
        profile.finish()
        assert second.performance.detail(profile.request_id)["status"] == "completed"
    finally:
        second.close()
        first.close()


def test_trace_contains_no_prompt_results_or_secret_error_text():
    p = Profile()
    p.update(
        prompt="private prompt",
        api_key="private key",
        model="qwen3:8b",
        provider="ollama",
        thinking=False,
    )
    p.fail("error contains private prompt and password")
    dumped = json.dumps(p.snapshot())
    assert "private" not in dumped and "password" not in dumped
    assert p.info == {"model": "qwen3:8b", "provider": "ollama", "thinking": False}


def test_text_history_and_trace_share_one_request_id():
    async def run():
        store = MemoryStore(":memory:")
        settings = Settings(language="ru-RU")
        conv = Conversation(
            settings, SimpleNamespace(), store, tools=ToolRegistry(clock_tools(settings))
        )
        try:
            assert "".join([s async for s in conv.reply("Сколько времени?")])
            messages = store.conversation_page(conv.id)
            assert len({m["request_id"] for m in messages}) == 1
            identity = messages[-1]["request_id"]
            detail = store.performance.detail(identity)
            assert detail["request_id"] == identity and detail["status"] == "completed"
            assert messages[-1]["has_trace"] == 1
            assert detail["metadata"]["conversation_id"] == conv.id
            assert any(s["stage"] == "tool_execution" for s in detail["stages"])
        finally:
            store.close()

    asyncio.run(run())


def test_closing_partial_text_does_not_persist_completed_trace():
    class Model:
        async def chat_stream(self, *args, **kwargs):
            yield ChatChunk(content="Содержательный ответ. ")
            await asyncio.Event().wait()

    async def run():
        store = MemoryStore(":memory:")
        conv = Conversation(Settings(language="ru-RU"), Model(), store)
        try:
            stream = conv.reply("Расскажи о радуге")
            assert await anext(stream)
            await stream.aclose()
            row = store.db.execute("SELECT status FROM request_traces").fetchone()
            assert row["status"] == "cancelled"
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "text",
    [
        "Привет",
        "Что такое радуга?",
        "Что такое радуга? Ответь одним предложением.",
        "Подскажи, как отдохнуть после учёбы",
    ],
)
def test_fast_mode_is_conservative_and_configurable(text):
    assert simple_request(text)
    client = OllamaClient(LLMConfig(think=True))
    messages = [{"role": "user", "content": text}]
    assert client._payload(messages, True, None)["think"] is False
    client.cfg.think_for_simple = True
    assert client._payload(messages, True, None)["think"] is True


@pytest.mark.parametrize(
    "text",
    [
        "Докажи теорему",
        "Напиши алгоритм сортировки",
        "Что такое радуга? Подробно объясни механизм и приведи источники.",
        "Сравни два способа отдохнуть при заданных условиях",
        "Включи Favourite Songs",
        "Рассчитай 7 * 19",
        "Запомни мой любимый цвет",
        "Объясни юридические последствия",
    ],
)
def test_complex_requests_keep_thinking_and_tools(text):
    assert not simple_request(text)
    client = OllamaClient(LLMConfig(think=True))
    assert client._payload([{"role": "user", "content": text}], True, None)["think"] is True


def test_fast_mode_uses_original_request_before_context_notes():
    client = OllamaClient(LLMConfig())
    context = RequestContext(DeviceContext.local_mac(), "Что такое радуга?")
    with request_scope(context):
        payload = client._payload(
            [{"role": "user", "content": "Very long private context " * 100}], True, None
        )
    assert payload["think"] is False


def response(text="", call=None, finish=None):
    parts = [types.Part(text=text)] if text else []
    if call:
        parts.append(types.Part(function_call=types.FunctionCall(id="call-1", name=call, args={})))
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(parts=parts), finish_reason=finish)],
        model_version="gemini-3.6-flash",
    )


@pytest.mark.parametrize("action", [False, True])
def test_gemini_with_tools_streams_text_but_holds_actions_until_complete(action):
    async def run():
        ready, release = asyncio.Event(), asyncio.Event()

        async def api(**kwargs):
            assert kwargs["config"].tools

            async def stream():
                ready.set()
                yield response("Первое предложение. ")
                await release.wait()
                yield response(call="current_time", finish=types.FinishReason.STOP)

            return stream()

        client = GeminiClient(
            GeminiConfig(free_tier_confirmed=True),
            "test",
            SimpleNamespace(models=SimpleNamespace(generate_content_stream=api)),
        )
        ctx = RequestContext(
            DeviceContext.local_mac(), "Включи музыку" if action else "Мне нужен совет"
        )
        tools = ToolRegistry(clock_tools(Settings())).schemas()
        with request_scope(ctx):
            stream = client.chat_stream([{"role": "user", "content": ctx.text}], tools)
            first = asyncio.create_task(anext(stream))
            await ready.wait()
            await asyncio.sleep(0)
            if action:
                assert not first.done()
            else:
                chunk = await first
                assert chunk.content and not chunk.tool_calls
            release.set()
            if action:
                chunk = await first
                assert chunk.done and chunk.tool_calls
            else:
                rest = [c async for c in stream]
                assert rest[-1].done and rest[-1].tool_calls
                assert rest[-1].native_parts
            await stream.aclose()

    asyncio.run(run())


def test_incomplete_gemini_tool_call_is_never_released():
    async def run():
        async def api(**kwargs):
            async def stream():
                yield response(call="current_time")

            return stream()

        client = GeminiClient(
            GeminiConfig(free_tier_confirmed=True),
            "test",
            SimpleNamespace(models=SimpleNamespace(generate_content_stream=api)),
        )
        with pytest.raises(LLMError, match="действия отклонены"):
            _ = [
                c
                async for c in client.chat_stream(
                    [], ToolRegistry(clock_tools(Settings())).schemas()
                )
            ]

    asyncio.run(run())


def test_live_updates_are_real_intervals_and_end_after_completion():
    store = MemoryStore(":memory:")
    bus = HudBus()
    store.performance.bind(bus, "Europe/Tallinn")
    profile = Profile(kind="voice")
    try:
        store.performance.attach(profile)
        span = profile.begin("tool_execution", tool="current_time")
        event = next(e for e in bus.snapshot() if e["type"] == "performance_live")
        assert event["trace"]["status"] == "running"
        assert event["trace"]["stages"][-1]["end_s"] is None
        profile.end(span)
        profile.finish()
        bus.request("performance_detail", profile.request_id)
        assert store.performance.detail(profile.request_id)["status"] == "completed"
    finally:
        store.close()


def test_text_with_tts_finishes_after_playback_not_generation():
    from backend.voice import reply_aloud

    async def run():
        done_generating, allow_audio_end = asyncio.Event(), asyncio.Event()

        class Model:
            async def chat_stream(self, *args, **kwargs):
                yield ChatChunk(content="Полезный ответ. ", done=True)
                done_generating.set()

        class Speaker:
            on_sentence = None
            interrupted = False

            async def speak(self, sentences, on_start=None):
                async for _ in sentences:
                    if on_start:
                        on_start()
                        on_start = None
                await allow_audio_end.wait()
                return True

        store = MemoryStore(":memory:")
        conv = Conversation(Settings(language="ru-RU"), Model(), store)
        try:
            task = asyncio.create_task(reply_aloud(conv, Speaker(), "Что такое радуга?", 6))
            await done_generating.wait()
            await asyncio.sleep(0)
            assert not task.done()
            assert store.db.execute("SELECT status FROM request_traces").fetchone()[0] == "running"
            allow_audio_end.set()
            assert await task
            assert (
                store.db.execute("SELECT status FROM request_traces").fetchone()[0] == "completed"
            )
        finally:
            store.close()

    asyncio.run(run())


def test_waiting_phrase_does_not_count_as_content_audio():
    from backend.audio.waiting import is_waiting_only

    assert is_waiting_only("Секунду.")
    assert is_waiting_only("Ща")
    assert not is_waiting_only("Радуга возникает при преломлении света.")
    assert not is_waiting_only("Да.")  # a short actual answer is still an answer


@pytest.mark.parametrize("fails", [False, True])
def test_audio_is_reported_only_after_a_successful_device_write(monkeypatch, fails):
    import queue
    from pathlib import Path

    import numpy as np
    import sounddevice

    from backend.audio.tts import PiperSpeaker

    events = []

    class Output:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def write(self, block):
            if fails:
                raise RuntimeError("device disconnected")
            events.append("write")

        def abort(self):
            pass

    monkeypatch.setattr(sounddevice, "OutputStream", Output)
    speaker = PiperSpeaker(Path("/not-loaded"))
    speaker._voice = SimpleNamespace(config=SimpleNamespace(sample_rate=24000))
    audio = queue.Queue()
    audio.put(("Полезный ответ.", np.zeros(1200, dtype=np.int16)))
    audio.put(None)
    try:
        if fails:
            with pytest.raises(RuntimeError):
                speaker._play(
                    audio,
                    lambda: events.append("start"),
                    None,
                    lambda *_: events.append("sentence"),
                )
            assert events == []
        else:
            assert speaker._play(
                audio, lambda: events.append("start"), None, lambda *_: events.append("sentence")
            )
            assert events == ["write", "sentence", "start"]
    finally:
        speaker._synth_pool.shutdown(wait=True)


@pytest.mark.parametrize(
    "text",
    [
        "Как отдохнуть после операции на сердце?",
        "Помоги расслабиться при панической атаке, учти схему лечения",
        "Помоги выбрать способ отдохнуть с бюджетом 50000 и ограничениями на поездку",
    ],
)
def test_fast_rest_mode_does_not_capture_health_or_constrained_planning(text):
    assert not simple_request(text)


@pytest.mark.parametrize(
    "args",
    [
        ["latency-check", "--pipeline"],
        ["latency-check", "--pipeline", "--live", "--play-audio", "--stt-file", "private.wav"],
    ],
)
def test_pipeline_requires_explicit_audio_and_never_silently_ignores_input_file(monkeypatch, args):
    import backend.performance_check
    from backend.__main__ import main

    run = AsyncMock(side_effect=AssertionError("must not start equipment"))
    monkeypatch.setattr(backend.performance_check, "run", run)
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
    run.assert_not_called()


def test_stt_timeout_is_not_reported_as_successful_recognition():
    from tests.test_voice_loop import Harness

    async def run():
        h = Harness(text="unused", wake=False)
        store = MemoryStore(":memory:")
        h.loop.conv = Conversation(h.loop.s, SimpleNamespace(), store)
        h.loop.stt.transcribe = AsyncMock(side_effect=TimeoutError())
        try:
            await h.frames(20, prob=0.9)
            await h.frames(35)
            await h.loop.task
            row = store.db.execute("SELECT data FROM request_traces").fetchone()
            trace = json.loads(row[0])
            assert trace["status"] == "failed"
            assert "stt_failed" in trace["marks"] and "stt_completed" not in trace["marks"]
            assert next(s for s in trace["stages"] if s["stage"] == "stt")["outcome"] == "error"
        finally:
            store.close()

    asyncio.run(run())


def test_active_gemini_tool_stream_survives_original_total_budget():
    async def run():
        async def api(**kwargs):
            async def stream():
                for _ in range(6):
                    yield response("Смысловой фрагмент. ")
                    await asyncio.sleep(0.05)
                yield response(finish=types.FinishReason.STOP)

            return stream()

        config = GeminiConfig(free_tier_confirmed=True).model_copy(
            # Total stream still exceeds the original budget, with enough scheduling
            # margin that unrelated macOS UI work cannot turn this into a flaky test.
            update={"timeout_s": 0.15, "attempt_timeout_s": 0.08}
        )
        primary = GeminiClient(
            config, "test", SimpleNamespace(models=SimpleNamespace(generate_content_stream=api))
        )
        local = SimpleNamespace(chat_stream=AsyncMock(side_effect=AssertionError("no fallback")))
        context = RequestContext(DeviceContext.local_mac(), "Нужен подробный ответ")
        with request_scope(context):
            chunks = [
                c
                async for c in FallbackClient(primary, local).chat_stream(
                    [{"role": "user", "content": context.text}],
                    ToolRegistry(clock_tools(Settings())).schemas(),
                )
            ]
        assert sum(bool(c.content) for c in chunks) == 6
        assert chunks[-1].done
        local.chat_stream.assert_not_called()

    asyncio.run(run())


def test_stats_reconnect_returns_fresh_active_snapshot_not_cached_progress(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("backend.latency.time.perf_counter", lambda: now[0])
    store = MemoryStore(":memory:")
    profile = Profile(origin=0)
    try:
        store.performance.attach(profile)
        stage = profile.begin("model_total")
        now[0] = 5.0
        event = store.performance.statistics(1, "Europe/Tallinn")
        assert event["active"]["elapsed_s"] == 5
        assert event["active"]["stages"][0]["end_s"] is None
        assert profile.totals()["model_total"] == 5
        profile.end(stage)
        profile.finish()
        assert store.performance.statistics(1, "Europe/Tallinn")["active"] is None
    finally:
        store.close()


@pytest.mark.parametrize(
    "text, text_only", [("Что такое радуга?", True), ("Поставь таймер", False)]
)
def test_background_warm_matches_turn_schemas_history_and_memory_prefix(
    monkeypatch, text, text_only
):
    async def run():
        store = MemoryStore(":memory:")
        model = SimpleNamespace(cloud_context=True, needs_local_warmup=True, prime=AsyncMock())
        settings = Settings(language="ru-RU")
        settings.gemini.context_messages = 2
        settings.llm.context_messages = 8
        conv = Conversation(
            settings,
            model,
            store,
            tools=ToolRegistry(clock_tools(settings)),
            warm_after_turn=True,
        )
        for n in range(4):
            conv.add_exchange(f"Вопрос {n}", f"Ответ {n}")
        monkeypatch.setattr(
            Conversation,
            "system_prompt",
            property(lambda self: "request-specific memory" if self._request else "other memory"),
        )
        conv._request = RequestContext(conv.device, text)
        try:
            conv.schedule_warm()
            conv._request = None  # the streaming generator cleans up before the task runs
            await conv.settle()
            prompt, tools, history = model.prime.await_args.args
            assert prompt == "request-specific memory"
            assert tools == ([] if text_only else conv.tool_schemas)
            assert len(history) == 3  # same two-message budget as the real cloud/fallback turn
            assert history[0]["content"] == "Вопрос 3"
            assert history[1]["content"] == "Ответ 3"
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "text",
    [
        "Какая погода?",
        "Какая погода в Таллине завтра?",
        "Какая погода в Таллине вечером?",
        "Какая погода в этом городе?",
        "Какая погода в Таллине и почему?",
        "«Какая погода в Таллине?» — это пример команды",
    ],
)
def test_weather_fast_route_does_not_guess_forecast_or_context(text):
    from backend.local_commands import route

    assert route(text) is None


@pytest.mark.parametrize("failed", [False, True])
def test_live_weather_request_uses_existing_guarded_tool_without_model(failed):
    from backend.tools.registry import ToolError
    from backend.tools.weather import weather_tools

    async def run():
        store = MemoryStore(":memory:")
        settings = Settings(language="ru-RU")
        forecast = AsyncMock(
            return_value={
                "place": "Таллин, Эстония",
                "now": {"temperature": 6, "conditions": "дождь"},
                "days": [],
            },
            side_effect=ToolError("Погодный сервис недоступен") if failed else None,
        )
        model = SimpleNamespace(chat_stream=AsyncMock(side_effect=AssertionError("no LLM")))
        conv = Conversation(
            settings,
            model,
            store,
            tools=ToolRegistry(weather_tools(settings, SimpleNamespace(forecast=forecast))),
        )
        try:
            for _ in range(2):
                answer = "".join(
                    [part async for part in conv.reply("Чушпан, какая сегодня погода в Таллине?")]
                )
                if failed:
                    assert "недоступен" in answer and "6" not in answer
                else:
                    assert "Таллин, Эстония" in answer and "дождь" in answer and "6" in answer
            assert forecast.await_count == 2
            forecast.assert_awaited_with(city="Tallinn", days=1)
            model.chat_stream.assert_not_called()
            trace = json.loads(
                store.db.execute("SELECT data FROM request_traces LIMIT 1").fetchone()[0]
            )
            tool = next(s for s in trace["stages"] if s["stage"] == "tool_execution")
            assert tool["metadata"]["tool"] == "get_weather"
            assert tool["outcome"] == ("error" if failed else "ok")
            assert trace["status"] == ("failed" if failed else "completed")
        finally:
            store.close()

    asyncio.run(run())


def test_weather_timeout_cancels_service_and_records_failed_trace():
    from backend.tools.weather import weather_tools

    async def run():
        cancelled = asyncio.Event()

        async def forecast(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        settings = Settings(language="ru-RU")
        settings.tools = settings.tools.model_copy(update={"quick_read_timeout_s": 0.01})
        store = MemoryStore(":memory:")
        conv = Conversation(
            settings,
            SimpleNamespace(),
            store,
            tools=ToolRegistry(weather_tools(settings, SimpleNamespace(forecast=forecast))),
        )
        try:
            answer = "".join([part async for part in conv.reply("Какая погода в Таллине?")])
            assert "проверить не удалось" in answer and cancelled.is_set()
            trace = json.loads(store.db.execute("SELECT data FROM request_traces").fetchone()[0])
            assert trace["status"] == "failed" and "tool_timeout" in trace["errors"]
            assert (
                next(s for s in trace["stages"] if s["stage"] == "tool_execution")["outcome"]
                == "cancelled"
            )
            result = next(m for m in store.recent(conv.id, 10) if m["role"] == "tool")
            assert "проверить не удалось" in json.loads(result["content"])["error"]
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("text", ["Ум-м.", "У-у-у.", "Хммммм", "Субтитры сделал DimaTorzok"])
def test_live_noise_transcripts_do_not_start_expensive_model_round(text):
    from backend.audio.stt import is_hallucination

    assert is_hallucination(text)


@pytest.mark.parametrize(
    "text", ["Ум-м, что такое радуга?", "Да", "Угу", "М-м, включай", "Я устала"]
)
def test_noise_filter_preserves_actual_requests_and_agreements(text):
    from backend.audio.stt import is_hallucination

    assert not is_hallucination(text)


def test_repeated_live_definition_does_not_retry_a_correct_identical_answer():
    async def run():
        store = MemoryStore(":memory:")
        answer = "Радуга — это оптическое явление при преломлении солнечного света в каплях воды."
        calls = []

        async def stream(messages, tools=None):
            calls.append(messages)
            yield ChatChunk(content=answer[:20])
            yield ChatChunk(content=answer[20:], done=True)

        conv = Conversation(Settings(language="ru-RU"), SimpleNamespace(chat_stream=stream), store)
        conv.add_exchange("что такое радуга?", answer)
        try:
            result = "".join([p async for p in conv.reply("Чушпан, что такое радуга?")])
            assert result == answer and len(calls) == 1
            trace = json.loads(store.db.execute("SELECT data FROM request_traces").fetchone()[0])
            assert not trace["metadata"].get("retry")
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("query", ["Какая погода в Таллине?", "Включи Favourite Songs"])
def test_repeated_data_or_action_still_cannot_copy_a_past_success(query):
    answer = "Предыдущий результат инструмента нельзя выдавать за новый подтверждённый результат."
    history = [{"role": "user", "content": query}, {"role": "assistant", "content": answer}]
    assert answer.lower() in Conversation._earlier_replies(query, history)


@pytest.mark.parametrize(
    "text, fast, tools, selected",
    [
        ("Что такое радуга?", True, [], "secondary"),
        ("Что такое радуга?", False, [], "primary"),
        ("Что такое радуга?", True, ToolRegistry(clock_tools(Settings())).schemas(), "primary"),
        ("Подробно выведи формулы преломления света", True, [], "primary"),
    ],
)
def test_plain_cloud_route_uses_only_configured_secondary_and_preserves_complex_chain(
    text, fast, tools, selected
):
    async def run():
        calls = []

        def api(label):
            async def generate(**kwargs):
                calls.append(label)

                async def stream():
                    yield response("Содержательный ответ.")
                    yield response(finish=types.FinishReason.STOP)

                return stream()

            return SimpleNamespace(models=SimpleNamespace(generate_content_stream=generate))

        cfg = GeminiConfig(free_tier_confirmed=True, simple_use_fallback_model=fast)
        primary = GeminiClient(cfg, "test", api("primary"))
        secondary = GeminiClient(
            cfg.model_copy(update={"model": "gemini-3.5-flash"}), "test", api("secondary")
        )
        local = SimpleNamespace(chat_stream=AsyncMock(side_effect=AssertionError("no local")))
        with request_scope(RequestContext(DeviceContext.local_mac(), text)):
            chunks = [
                c
                async for c in FallbackClient(primary, local, secondary=secondary).chat_stream(
                    [{"role": "user", "content": text}], tools
                )
            ]
        assert calls == [selected] and chunks[-1].done
        local.chat_stream.assert_not_called()

    asyncio.run(run())
