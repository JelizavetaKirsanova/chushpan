"""Activation/continuation safety over PCM frames, plus native decoder and UI settings."""

import asyncio
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from backend.audio.activity import VoiceControls, controls, decoder_confident
from backend.audio.aec import resample
from backend.audio.vad import FRAME_SAMPLES, SileroVAD, UtteranceSegmenter
from backend.config import Settings
from backend.latency import Profile, current
from backend.memory.store import MemoryStore
from backend.state import State
from backend.voice import speak_tokens
from tests.test_dialogue import finish
from tests.test_voice_loop import FakeSpeaker, custom_harness
from tests.test_voice_reliability import SPEECH


def test_native_confidence_uses_provided_metrics_without_probability_invention():
    cfg = Settings().wakeword
    assert not decoder_confident(
        SimpleNamespace(last_confidence={}, last_speech_confident=True), cfg
    )
    for logprob, silence, compression, expected in [
        (-0.3, 0.02, 1.1, True),
        (-0.9, 0.02, 1.1, False),
        (-0.3, 0.7, 1.1, False),
        (-0.3, 0.02, 3, False),
    ]:
        source = SimpleNamespace(
            last_confidence={
                "min_avg_logprob": logprob,
                "max_no_speech_prob": silence,
                "max_compression_ratio": compression,
            }
        )
        assert decoder_confident(source, cfg) is expected


@pytest.mark.parametrize("text", ["Эй, Чушпан", "Эй, Чушпан, что такое радуга?"])
def test_low_confidence_name_never_activates_or_calls_model(text):
    async def run():
        h = custom_harness(text)
        h.stt.last_speech_confident = False
        h.loop.conv.reply = AsyncMock(side_effect=AssertionError("no model"))
        await h.loop._check_wake_phrase(SPEECH, time.perf_counter())
        assert h.state is State.SLEEPING and h.loop.task is None
        assert not h.loop.session_active

    asyncio.run(run())


def test_activation_rate_limit_and_native_threshold_actually_gate_decoder():
    async def run():
        h = custom_harness("Эй, Чушпан")
        await h.loop._check_wake_phrase(SPEECH, time.perf_counter())
        assert h.state is State.LISTENING
        h.loop._go_to_sleep()
        count = h.stt.calls
        await h.loop._check_wake_phrase(SPEECH, time.perf_counter())
        assert h.stt.calls == count and h.state is State.SLEEPING
        h.now += h.s.wakeword.cooldown_s + 1
        await h.loop._check_wake_phrase(SPEECH, time.perf_counter())
        assert h.state is State.LISTENING
        h.loop._close_session()

    asyncio.run(run())


@pytest.mark.parametrize("text", ["Эй, Чушпан, ум", "Эй, Чушпан, хммм", "Эй, Чушпан, ..."])
def test_name_then_noise_does_not_start_answer(text):
    async def run():
        h = custom_harness(text)
        h.loop.conv.reply = Mock(side_effect=AssertionError("filler is not an answer request"))
        await h.loop._check_wake_phrase(SPEECH, time.perf_counter())
        assert h.loop.task is None and not h.loop.speaker.said
        h.loop._close_session()

    asyncio.run(run())


def test_stale_echo_and_post_tts_activation_guard():
    async def run():
        h = custom_harness("Эй, Чушпан, расскажи о воде")
        h.loop.speaker.recent_spoken = [h.stt.text]
        h.loop.speaker.last_audio_at = time.perf_counter()
        await h.loop._check_wake_phrase(SPEECH, time.perf_counter())
        assert h.state is State.SLEEPING and h.loop.task is None
        h.now += 3
        h.loop.speaker.last_audio_at = time.perf_counter() - 5
        await h.loop._check_wake_phrase(SPEECH, time.perf_counter())
        await finish(h)
        assert h.loop.speaker.said
        h.loop._close_session()

    asyncio.run(run())


def test_natural_followup_named_farewell_and_hard_session_limit():
    async def run():
        h = custom_harness("Чушпан, что такое радуга?")
        h.loop._wake_up(None)
        heard = []

        async def reply(text):
            heard.append(text)
            yield "Короткий ответ."

        h.loop.conv.reply = reply
        for text in ["Чушпан, что такое радуга?", "А почему небо голубое?", "Пока, Чушпан"]:
            h.stt.text = text
            h.loop.state.to(State.THINKING)
            h.loop.task = asyncio.create_task(
                h.loop._bounded_turn(
                    lambda: h.loop.handle(SPEECH, time.perf_counter(), session_input=True)
                )
            )
            h.loop.task.add_done_callback(h.loop._after_turn)
            await finish(h)
        assert len(heard) == 2 and h.state is State.SLEEPING
        h.loop._wake_up(None)
        start = h.loop._session_started_at
        h.now = start + h.s.dialogue.max_session_s - 1
        h.loop._open_session()
        assert h.loop.session_deadline == start + h.s.dialogue.max_session_s
        h.now += 2
        h.loop._session_timeout()
        assert h.state is State.SLEEPING

    asyncio.run(run())


def test_manual_stop_works_even_when_legacy_always_listening_is_enabled():
    async def run():
        h = custom_harness("")
        h.loop.use_wake = False
        h.loop.state.to(State.LISTENING)
        h.loop.state.to(State.THINKING)
        h.loop.task = asyncio.create_task(asyncio.Event().wait())
        h.loop.speaker.interrupt = Mock()
        h.loop.command("stop")
        await asyncio.gather(h.loop.task, return_exceptions=True)
        assert h.loop.task.cancelled()
        h.loop.speaker.interrupt.assert_called_once()

    asyncio.run(run())


def test_named_stop_without_general_barge_in_stays_silent():
    async def run():
        h = custom_harness("Чушпан, стоп")
        h.s.audio.barge_in = False
        h.loop._wake_up(None)
        h.loop.state.to(State.SPEAKING)
        previous = asyncio.create_task(asyncio.Event().wait())
        h.loop.task = previous
        await h.loop._validate_barge(SPEECH, previous, stop_only=True)
        assert previous.cancelled() and h.loop.speaker.said == []
        h.loop._close_session()

    asyncio.run(run())


@pytest.mark.parametrize("filename", ["4-157296-A-24.wav", "1-118206-A-31.wav"])
def test_real_cough_and_click_do_not_make_a_vad_utterance(filename):
    path = Path(__file__).parent / "fixtures/audio" / filename
    with wave.open(str(path)) as f:
        raw = np.frombuffer(f.readframes(f.getnframes()), dtype="<i2")
        rate = f.getframerate()
    audio = np.pad(resample(raw, rate), (0, 24000))
    vad = SileroVAD()
    segmenter = UtteranceSegmenter(Settings().vad)
    utterances = []
    for i in range(0, len(audio) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
        frame = audio[i : i + FRAME_SAMPLES]
        event = segmenter.process(frame, vad(frame))
        if event.utterance is not None:
            utterances.append(event.utterance)
    assert not utterances


def test_voice_controls_apply_to_actual_gates_processor_and_sqlite():
    async def run():
        h = custom_harness("")
        store = MemoryStore(":memory:")
        h.loop.conv.store = store
        processor = SimpleNamespace(noise_suppression=False, configure_noise=Mock())
        h.loop.input_processor = processor
        value = VoiceControls(
            sensitivity="conservative", continuation_s=20, allow_barge_in=False
        ).model_dump()
        h.loop.configure_voice(value)
        assert store.preference("voice", "controls") == value
        assert h.s.wakeword.threshold == 0.8 and h.s.wakeword.min_avg_logprob == -0.5
        assert h.s.dialogue.duration_s == 20 and h.loop.use_wake
        processor.configure_noise.assert_called_once_with(True)
        saved = h.s.model_copy(deep=True)
        h.loop.configure_voice({**value, "mode": "always_listening"})
        assert h.s.wakeword == saved.wakeword
        # The legacy advanced form supports 300 s; the new panel must report the
        # effective value faithfully, with the independent absolute cap still visible.
        h.s.dialogue.duration_s = 300
        assert controls(h.s).continuation_s == 300
        h.loop._close_session()
        store.close()

    asyncio.run(run())


def test_waiting_marks_never_occupy_meaningful_sentence_marks(monkeypatch):
    import sounddevice

    from backend.audio.tts import PiperSpeaker

    class Output:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def write(self, block):
            pass

        def abort(self):
            pass

    monkeypatch.setattr(sounddevice, "OutputStream", Output)

    async def run():
        speaker = PiperSpeaker(Path("/unused"))
        speaker._voice = SimpleNamespace(config=SimpleNamespace(sample_rate=24000))
        speaker._synth = lambda _: np.ones(2400, dtype=np.int16)
        profile = Profile(kind="voice")
        token = current.set(profile)
        try:
            await speaker.speak(_phrases(["Секунду."]))
            assert "waiting_sentence_ready" in profile.marks
            assert "first_sentence_ready" not in profile.marks
            await speaker.speak(_phrases(["Радуга образуется при преломлении света."]))
            assert profile.marks["first_sentence_ready"] > profile.marks["waiting_sentence_ready"]
        finally:
            current.reset(token)

    asyncio.run(run())


async def _phrases(values):
    for value in values:
        yield value


def test_intro_is_combined_with_meaningful_sentence_without_losing_words():
    async def run():
        speaker = FakeSpeaker()
        paused = asyncio.Event()
        release = asyncio.Event()

        async def tokens():
            yield "Конечно. "
            paused.set()
            await release.wait()
            yield "Радуга — это свет в каплях воды."

        task = asyncio.create_task(speak_tokens(speaker, tokens(), 20, "ru"))
        await paused.wait()
        await asyncio.sleep(0)
        assert speaker.said == []
        release.set()
        await task
        assert speaker.said == ["Конечно. Радуга — это свет в каплях воды."]

    asyncio.run(run())


def test_compact_simple_prompt_keeps_personality_and_does_not_change_complex_context():
    from backend.brain.prompts import build_system_prompt
    from backend.brain.response_mode import simple_request

    s = Settings(language="ru-RU", persona={"style": "Уникальная неизменяемая манера персонажа"})
    full = build_system_prompt(s, ["таймеры", "музыка"], [(1, "Пользователь любит чай")])
    short = build_system_prompt(
        s, ["таймеры", "музыка"], [(1, "Пользователь любит чай")], text_only=True
    )
    assert len(short) < len(full) and s.persona_style in short
    assert "Пользователь любит чай" in short and "не утверждай успех" in short
    for question in ["Что такое радуга, ответь одним предложением", "А почему небо голубое?"]:
        assert simple_request(question)
    for question in [
        "Докажи теорему",
        "Напиши код сортировки",
        "Что такое радуга? Сравни две математические модели",
        "Какая сегодня погода?",
    ]:
        assert not simple_request(question)


def test_activation_checks_are_persisted_separately_from_response_latency():
    store = MemoryStore(":memory:")
    try:
        p = Profile(kind="activation")
        store.performance.attach(p)
        p.update(reason="not_addressed", max_no_speech_prob=0.1, min_avg_logprob=-0.2)
        p.finish("rejected")
        stats = store.performance.statistics(1, "Europe/Tallinn")
        assert stats["summary"]["count"] == 0
        assert len(stats["activations"]) == 1 and stats["activations"][0]["status"] == "rejected"
    finally:
        store.close()


def test_failed_settings_save_restores_native_noise_and_runtime_settings():
    import sqlite3

    h = custom_harness("")
    h.loop.conv.store = SimpleNamespace(
        set_preference=Mock(side_effect=sqlite3.OperationalError("private storage detail"))
    )
    processor = SimpleNamespace(noise_suppression=False, configure_noise=Mock())
    h.loop.input_processor = processor
    original = h.s.model_copy(deep=True)
    events = h.loop.bus.subscribe()
    h.loop.configure_voice(VoiceControls(sensitivity="conservative").model_dump())
    assert h.s.audio == original.audio and h.s.wakeword == original.wakeword
    assert [call.args for call in processor.configure_noise.call_args_list] == [(True,), (False,)]
    received = []
    while not events.empty():
        received.append(events.get_nowait())
    result = next(event for event in received if event["type"] == "voice_settings_result")
    assert result["type"] == "voice_settings_result" and not result["ok"]
    assert "private" not in result["message"]


def test_onnx_activation_honors_post_tts_echo_guard():
    from tests.test_voice_loop import Harness

    async def run():
        h = Harness()
        h.loop.mute_until = 3
        h.wake_model.wake_soon()
        await h.frames(3)
        assert h.state is State.SLEEPING
        h.now = 4
        await h.frames(3)
        assert h.state is State.LISTENING
        h.loop._close_session()

    asyncio.run(run())


def test_voice_diagnostic_default_never_opens_audio_devices(monkeypatch, capsys):
    from backend import voice_check
    from backend.audio.capture import Microphone

    monkeypatch.setattr(Microphone, "start", Mock(side_effect=AssertionError("no microphone")))
    monkeypatch.setattr(voice_check, "make_speaker", Mock(side_effect=AssertionError("no output")))
    assert asyncio.run(voice_check.run(Settings())) == 0
    assert '"results": []' in capsys.readouterr().out


def test_gemini_idle_after_content_cancels_without_replaying_or_executing_tools():
    from backend.brain.llm import ChatChunk
    from backend.brain.providers import FallbackClient, GeminiClient, GeminiRequestError
    from backend.config import GeminiConfig
    from tests.test_providers import Local, secondary_model

    class Stalled(GeminiClient):
        async def chat_stream(self, messages, tools=None):
            yield ChatChunk(content="Радуга образуется в каплях воды.")
            await asyncio.Event().wait()

    async def run():
        primary = Stalled(
            GeminiConfig(free_tier_confirmed=True).model_copy(
                update={"stream_idle_timeout_s": 0.02}
            ),
            "test-only",
        )
        secondary, method = secondary_model()
        local = Local()
        client = FallbackClient(primary, local, secondary=secondary)
        stream = client.chat_stream([{"role": "user", "content": "Объясни радугу"}])
        try:
            assert (await anext(stream)).content.startswith("Радуга")
            with pytest.raises(GeminiRequestError):
                await asyncio.wait_for(anext(stream), 0.5)
            assert not local.calls and method.await_count == 0
        finally:
            await stream.aclose()
            await client.aclose()

    asyncio.run(run())


def test_long_silence_after_waiting_is_visible_separately_from_sentence_gaps():
    profile = Profile(kind="voice", origin=100)
    profile.interval("waiting_playback", 102, 104)
    profile.mark("first_content_audio", 113)
    assert profile.snapshot()["waiting_to_content_gap_s"] == 9
    assert "playback_gap" not in profile.totals()


def test_vad_natural_pause_does_not_cut_a_sentence():
    cfg = Settings().vad.model_copy(update={"silence_ms": 650})
    segmenter = UtteranceSegmenter(cfg)
    frame = np.ones(FRAME_SAMPLES, np.int16)
    probabilities = [0.9] * 18 + [0.0] * 12 + [0.9] * 18 + [0.0] * 22
    complete = [segmenter.process(frame, probability) for probability in probabilities]
    utterances = [event for event in complete if event.utterance is not None]
    assert len(utterances) == 1
    assert utterances[0].speech_ms > 1000


def test_simple_response_does_not_insert_waiting_audio_before_cold_fallback():
    from backend.audio.waiting import allow_waiting

    assert not allow_waiting("Чушпан, что такое радуга?")
    assert allow_waiting("Чушпан, создай подробный план на неделю")
