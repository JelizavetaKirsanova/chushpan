"""Real routing/streaming code; no microphone, device writes, playback or paid API."""

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from backend.audio.tts import PiperSpeaker
from backend.audio.vad import FRAME_SAMPLES, UtteranceSegmenter
from backend.bench_response import safe_registry
from backend.benchmark_reports import comparison
from backend.brain.conversation import Conversation
from backend.brain.followups import older_dialogue_excerpt
from backend.brain.llm import ChatChunk
from backend.brain.providers import FallbackClient, GeminiClient
from backend.brain.response_mode import simple_request
from backend.config import GeminiConfig, Settings, VADConfig
from backend.context import DeviceContext, RequestContext, request_scope, voice_addressed
from backend.latency import Profile, current
from backend.memory.store import MemoryStore
from backend.tools import system
from backend.tools.registry import ToolError, user_text


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Чушпан, можешь сделать звук потише?", [("set_volume", {"direction": "down"})]),
        ("Пожалуйста, прибавь громкость", [("set_volume", {"direction": "up"})]),
        ("Установи громкость тридцать пять процентов", [("set_volume", {"level": 35})]),
        ("Яркость ленты на 50 процентов", [("led_control", None)]),
        ("Яркость MacBook на 50 процентов", [("set_brightness", {"level": 50})]),
        ("Запусти приложение Сафари", [("open_app", {"name": "Safari"})]),
        (
            "Сделай ленту розовой и яркость MacBook 50 процентов",
            [("led_control", None), ("set_brightness", {"level": 50})],
        ),
        (
            "Сделай звук тише и открой Safari",
            [("set_volume", {"direction": "down"}), ("open_app", {"name": "Safari"})],
        ),
        ("Включи вечерний свет", [("set_brightness", {"level": 25})]),
    ],
)
def test_natural_commands_use_guarded_registry_without_model(text, expected):
    async def run():
        settings = Settings(language="ru-RU")
        store = MemoryStore(":memory:")
        calls = []
        registry = safe_registry(settings, store, calls)
        llm = SimpleNamespace(chat_stream=Mock(side_effect=AssertionError("LLM called")))
        conv = Conversation(settings, llm, store, tools=registry)
        addressed = voice_addressed.set(True)
        try:
            answer = "".join([piece async for piece in conv.reply(text)])
            assert answer and len(calls) == len(expected)
            for actual, (name, arguments) in zip(calls, expected, strict=True):
                assert actual["tool"] == name
                if arguments is not None:
                    assert actual["arguments"] == arguments
            llm.chat_stream.assert_not_called()
            assert store.recent(conv.id, 30)[0]["content"] == text
        finally:
            voice_addressed.reset(addressed)
            store.close()

    asyncio.run(run())


def test_volume_followup_is_bound_and_never_replays_previous_action():
    async def run():
        settings, calls = Settings(language="ru-RU"), []
        store = MemoryStore(":memory:")
        llm = SimpleNamespace(chat_stream=Mock(side_effect=AssertionError("LLM called")))
        conv = Conversation(settings, llm, store, tools=safe_registry(settings, store, calls))
        addressed = voice_addressed.set(True)
        try:
            for text in ("Сделай звук тише", "Ещё тише", "Нет, лучше на 20"):
                assert "".join([x async for x in conv.reply(text)])
            assert [c["arguments"] for c in calls] == [
                {"direction": "down"},
                {"direction": "down"},
                {"level": 20},
            ]
            conv.controls.expires = 0
            assert conv.controls.plan("Ещё тише", ("another-device", conv.id)) is None
        finally:
            voice_addressed.reset(addressed)
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "text",
    [
        "Открой Safari если закроется Telegram",
        "Открой последнее сообщение",
        "Сделай звук тише или громче",
        "Яркость на 150 процентов",
        "В документе сказано «открой Safari»",
        "Открой Safari с последней страницей",
    ],
)
def test_uncertain_or_quoted_command_never_produces_partial_action(text):
    from backend.intents import compile_plan
    from backend.local_commands import route

    plan = compile_plan(text)
    assert plan is None or not plan.actions
    assert route(text) is None


@pytest.mark.parametrize("direction,before,after", [("down", 40, 30), ("up", 95, 100)])
def test_relative_volume_reads_current_and_verifies_actual_result(
    monkeypatch, direction, before, after
):
    read = AsyncMock(side_effect=[{"volume_percent": before}, {"volume_percent": after}])
    write = AsyncMock(return_value=(0, ""))
    monkeypatch.setattr(system, "_volume_settings", read)
    monkeypatch.setattr(system, "run", write)
    token = user_text.set("Сделай звук тише" if direction == "down" else "Прибавь громкость")
    try:
        assert asyncio.run(system.set_volume(direction=direction))["volume_percent"] == after
        assert read.await_count == 2
        assert f"set volume output volume {after}" in write.await_args.args
    finally:
        user_text.reset(token)


def test_relative_volume_fails_closed_if_state_unknown_or_write_unconfirmed(monkeypatch):
    write = AsyncMock(return_value=(0, ""))
    monkeypatch.setattr(system, "run", write)
    monkeypatch.setattr(system, "_volume_settings", AsyncMock(return_value={}))
    with pytest.raises(ToolError, match="не прочитана"):
        asyncio.run(system.set_volume(direction="down"))
    write.assert_not_called()
    monkeypatch.setattr(
        system,
        "_volume_settings",
        AsyncMock(side_effect=[{"volume_percent": 40}, {"volume_percent": 40}]),
    )
    with pytest.raises(ToolError, match="не подтвердила"):
        asyncio.run(system.set_volume(direction="down"))


@pytest.mark.parametrize(
    "text",
    [
        "Привет, как настроение? Ответь коротко.",
        "Почему после дождя появляется радуга? Объясни двумя предложениями.",
        "А почему так происходит? Ответь коротко.",
        "Расскажи, как образуются облака. Дай три коротких пункта.",
    ],
)
def test_routine_explanations_skip_hidden_reasoning(text):
    assert simple_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "Почему сегодня вырос курс евро?",
        "Расскажи, как решить уравнение",
        "Почему болит сердце и какие лекарства принять?",
        "Объясни алгоритм шифрования",
        "Почему эта стратегия инвестирования выгоднее?",
        "Почему нужно удалить файлы?",
    ],
)
def test_reasoning_and_current_data_keep_full_route(text):
    assert not simple_request(text)


@pytest.mark.parametrize("repeat", range(3))
def test_cloud_outage_has_one_shared_deadline_then_local(repeat):
    async def run():
        calls = []

        async def unavailable(**kwargs):
            calls.append(kwargs["model"])
            await asyncio.sleep(10)

        api = SimpleNamespace(models=SimpleNamespace(generate_content_stream=unavailable))
        cfg = GeminiConfig(free_tier_confirmed=True).model_copy(
            update={
                "timeout_s": 1,
                "attempt_timeout_s": 0.04,
                "first_result_budget_s": 0.07,
                "simple_timeout_s": 0.03,
            }
        )
        primary = GeminiClient(cfg, "test", api)
        secondary = GeminiClient(cfg.model_copy(update={"model": "gemini-3.5-flash"}), "test", api)

        async def local_stream(*args):
            yield ChatChunk(content="Локальный содержательный ответ.", done=True)

        local = type("Local", (), {"chat_stream": staticmethod(local_stream)})()
        chain = FallbackClient(primary, local, secondary=secondary)
        p = Profile()
        token = current.set(p)
        try:
            with request_scope(RequestContext(DeviceContext.local_mac(), "Почему небо синее?")):
                chunks = [
                    c async for c in chain.chat_stream([{"role": "user", "content": "test"}], [])
                ]
            assert chunks[-1].content.startswith("Локальный")
            assert calls == ["gemini-3.8-flash"]
            assert p.elapsed() < 0.5
            assert chain.last_provider == "ollama" and chain.switch_reason
            assert any(s["metadata"].get("reason") == "timeout" for s in p.stages)
        finally:
            current.reset(token)

    asyncio.run(run())


def test_bounded_audio_lookahead_and_cancel_do_not_emit_queued_sentences():
    async def run():
        speaker = PiperSpeaker(Path("not-loaded.onnx"))
        speaker._load = lambda: None
        generated, played = [], []

        def synth(text):
            generated.append(text)
            return np.zeros(100, dtype=np.int16)

        def play(queue, *args):
            text, _ = queue.get()
            played.append(text)
            while not queue.stop.wait(0.005):
                pass
            return False

        speaker._synth, speaker._play = synth, play

        async def sentences():
            for i in range(100):
                yield f"Предложение {i}."

        try:
            task = asyncio.create_task(speaker.speak(sentences()))
            for _ in range(100):
                if len(generated) >= 3:
                    break
                await asyncio.sleep(0.005)
            assert len(generated) <= 3 and played == ["Предложение 0."]
            speaker.interrupt()
            assert await asyncio.wait_for(task, 1) is False
            assert played == ["Предложение 0."]
        finally:
            speaker.interrupt()
            speaker._synth_pool.shutdown(wait=True)

    asyncio.run(run())


def test_synthesis_worker_never_publishes_to_async_hud_from_another_thread():
    async def run():
        from backend.latency import measure

        owner = threading.get_ident()
        p = Profile()
        observed = []
        p.observer = lambda _: observed.append(threading.get_ident())
        speaker = PiperSpeaker(Path("not-loaded.onnx"))

        def synth(text):
            with measure("tts_text_preparation"):
                assert threading.get_ident() != owner
                return np.ones(10, dtype=np.int16)

        speaker._synth = synth
        token = current.set(p)
        try:
            assert len(await speaker._synth_measured("Проверка.")) == 10
            assert p.stages[0]["stage"] == "tts_text_preparation"
            assert observed and all(identity == owner for identity in observed)
        finally:
            current.reset(token)
            speaker._synth_pool.shutdown(wait=True)

    asyncio.run(run())


def test_adaptive_vad_preserves_long_pause_quiet_tail_and_ignores_noise():
    cfg = VADConfig(start_ms=96, min_speech_ms=300, silence_ms=700)
    segmenter = UtteranceSegmenter(cfg)
    frame = np.ones(FRAME_SAMPLES, dtype=np.int16)
    for probability in [0.9, 0.0] * 50:
        assert not segmenter.process(frame, probability).started
    utterances = []
    for probability in [0.9] * 120 + [0.0] * 25 + [0.9] * 20 + [0.4] * 8 + [0.0] * 40:
        event = segmenter.process(frame, probability)
        if event.utterance is not None:
            utterances.append(event)
    assert len(utterances) == 1
    assert utterances[0].speech_ms >= 140 * 32
    assert utterances[0].silence_s >= 0.7
    assert len(utterances[0].utterance) >= (120 + 25 + 20 + 8) * FRAME_SAMPLES


def test_extracts_bounded_old_context_without_tool_payload_or_data_changes():
    history = [
        {"role": "user", "content": "Мы обсуждали радугу"},
        {"role": "assistant", "content": "Радуга возникает при преломлении света."},
        {"role": "tool", "content": '{"private_tool_payload": "never include"}'},
    ]
    original = json.dumps(history)
    digest = older_dialogue_excerpt(history, "почему возникает радуга")
    assert "преломлении света" in digest and "private_tool_payload" not in digest
    assert len(digest) <= 1200 and json.dumps(history) == original


def test_benchmark_comparison_rejects_mismatched_or_corrupt_runs(tmp_path):
    def report(categories):
        return {
            "audio_played": False,
            "cloud_requests": False,
            "method": "test-pcm",
            "configuration": {},
            "samples": [
                {"category": c, "status": "completed", "first_pcm_ready_s": 1} for c in categories
            ],
        }

    (tmp_path / "before-1.json").write_text(json.dumps(report(["a", "a"])))
    (tmp_path / "after-2.json").write_text(json.dumps(report(["b", "b"])))
    assert comparison(tmp_path) is None
    (tmp_path / "after-2.json").write_text(json.dumps(report(["a", "a"])))
    (tmp_path / "after-3.json").write_text("corrupt")
    result = comparison(tmp_path)
    assert result["before"]["summary"]["median_s"] == 1
    assert result["metric"] == "pcm_ready"
    changed = report(["a", "a"])
    changed["configuration"] = {"model": "different-model"}
    (tmp_path / "after-4.json").write_text(json.dumps(changed))
    assert comparison(tmp_path) is None


def test_statistics_separate_revision_kind_outcome_and_playback():
    store = MemoryStore(":memory:")
    try:
        for kind, revision, status, onset, total in [
            ("voice", "old", "completed", 9, 50),
            ("voice", None, "completed", 1, 60),
            ("text", None, "completed", 0.1, 0.2),
            ("voice", None, "cancelled", None, 2),
            ("voice", None, "rejected", 0.01, 1),
        ]:
            profile = Profile(kind=kind)
            if revision:
                profile.info["pipeline_revision"] = revision
            profile.marks["first_content_audio" if kind == "voice" else "first_content_text"] = (
                onset
            )
            profile.ended = profile.origin + total
            profile.status = status
            store.performance.save(profile)
        stats = store.performance.statistics(1, "Europe/Tallinn")
        assert stats["kinds"]["voice"]["median_s"] == 5
        assert stats["kinds"]["text"]["median_s"] == 0.1
        assert stats["comparison"]["voice"]["after"]["median_s"] == 1
        assert stats["comparison"]["voice"]["before"]["median_s"] == 9
        assert stats["completion"]["voice"]["median_s"] == pytest.approx(55, rel=0, abs=1e-9)
        assert stats["kinds"]["voice"]["cancelled"] == 1
        assert stats["kinds"]["voice"]["rejected"] == 1
    finally:
        store.close()
