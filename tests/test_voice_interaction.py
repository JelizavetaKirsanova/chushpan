"""End-to-end state/PCM regressions with fake models and a silent output sink."""

import asyncio
import time
from pathlib import Path

import numpy as np
import pytest

from backend.audio.activity import migrate_interaction_defaults
from backend.audio.evidence import evidence
from backend.audio.stt import strip_wake_phrase
from backend.audio.tts import SileroSpeaker, SynthesisError
from backend.audio.vad import FRAME_SAMPLES, UtteranceSegmenter
from backend.config import Settings, VADConfig
from backend.memory.store import MemoryStore
from backend.state import State
from tests.test_dialogue import finish
from tests.test_voice_loop import custom_harness
from tests.test_voice_reliability import SPEECH


@pytest.mark.parametrize(
    "phrase", ["Hey, Чушпан", "Эй, Чушпан", "Хей, Чушпан", "Hey, Chushpan", "Эй, Чушпэн"]
)
def test_wake_aliases_preserve_request_without_changing_confidence(phrase):
    async def run():
        h = custom_harness(phrase + ", что такое радуга?")
        await h.loop._check_wake_phrase(SPEECH, time.perf_counter())
        await finish(h)
        assert h.loop.speaker.said == ["Хорошо."]
        h.loop._close_session()

    asyncio.run(run())


@pytest.mark.parametrize(
    "text",
    ["Вчера говорили Hey Chushpan", "Эй, чужой человек", "Хей, чушь", "Что значит слово чушпан?"],
)
def test_mentions_and_unrelated_names_never_match(text):
    assert strip_wake_phrase(text, "эй чушпан") is None


def test_followup_countdown_starts_after_full_playback_and_pauses_during_speech():
    async def run():
        h = custom_harness("Эй, Чушпан, объясни радугу")
        h.s.dialogue.duration_s = 20
        h.loop._wake_up(None)
        started, release = asyncio.Event(), asyncio.Event()

        async def reply(text):
            h.now += 30  # slow model must not consume the follow-up window
            yield "Ответ."

        async def speak(parts, on_start=None):
            async for _ in parts:
                if on_start:
                    on_start()
            started.set()
            await release.wait()
            h.now += 10  # full playback, not first audio, anchors the deadline
            return True

        h.loop.conv.reply = reply
        h.loop.speaker.speak = speak
        h.loop.state.to(State.THINKING)
        h.loop.task = asyncio.create_task(h.loop._bounded_turn(lambda: h.loop.handle(SPEECH, 0)))
        h.loop.task.add_done_callback(h.loop._after_turn)
        await started.wait()
        assert h.loop.session_active and h.loop._session_paused_at is not None
        release.set()
        await finish(h)
        assert h.loop.session_deadline == h.now + 20
        h.now += 19.9
        await h.frames(10, prob=0.9)
        assert h.loop.segmenter.in_speech and h.loop.session_active
        h.now += 3
        h.loop._session_timeout()
        assert h.state is State.LISTENING
        h.loop._close_session()

    asyncio.run(run())


def test_neutral_followup_without_name_is_heard_after_an_accepted_turn():
    async def run():
        h = custom_harness("Эй, Чушпан, привет")
        for words in ["Эй, Чушпан, привет", "Как тебя зовут?", "Мне нужна помощь с учёбой"]:
            h.stt.text = words
            if h.state is State.SLEEPING:
                h.loop._wake_up(None)
            h.loop.state.to(State.THINKING)
            h.loop.task = asyncio.create_task(
                h.loop._bounded_turn(lambda: h.loop.handle(SPEECH, 0))
            )
            h.loop.task.add_done_callback(h.loop._after_turn)
            await finish(h)
        assert len(h.loop.speaker.said) == 3
        h.loop._close_session()

    asyncio.run(run())


def test_short_pause_and_long_phrase_remain_one_utterance_with_preroll_and_tail():
    seg = UtteranceSegmenter(VADConfig(silence_ms=1200, preroll_ms=400))
    events = []
    probs = [0.0] * 15 + [0.9] * 300 + [0.0] * 25 + [0.9] * 700 + [0.0] * 45
    for i, prob in enumerate(probs):
        event = seg.process(np.full(FRAME_SAMPLES, i, np.int16), prob)
        if event.utterance is not None:
            events.append(event)
    assert len(events) == 1 and events[0].end_reason == "silence"
    assert events[0].utterance[0] < 15
    assert events[0].utterance[-1] > 1039 * FRAME_SAMPLES / FRAME_SAMPLES


def test_silero_skips_punctuation_and_chunks_all_long_text_without_devices():
    from tests.test_silero_tts import FakeSilero

    speaker = SileroSpeaker(Path("unused"))
    model = FakeSilero()
    speaker._voice = model
    assert not len(speaker._synth("."))
    assert model.requests == []
    words = " ".join(["слово"] * 400)
    audio = speaker._synth(words)
    assert len(model.requests) > 1 and len(audio) == 5 * len(model.requests)
    assert " ".join(r["text"] for r in model.requests) == words
    speaker._synth_pool.shutdown()


def test_one_failed_fragment_does_not_cancel_valid_queued_audio_or_hide_failure():
    async def run():
        speaker = SileroSpeaker(Path("unused"))
        played = []
        speaker._load = lambda: None

        def synth(text):
            if text == "Ошибка":
                raise ValueError("model failure")
            return np.ones(128, np.int16)

        def player(queue, *_):
            while (item := queue.get()) is not None:
                assert not queue.stop.is_set()
                played.append(item[0])
            return True

        speaker._synth = synth
        speaker._play = player

        async def sentences():
            for part in ["Начало.", ".", "Ошибка", "Конец."]:
                yield part

        try:
            with pytest.raises(SynthesisError):
                await speaker.speak(sentences())
            assert played == ["Начало.", "Конец."]
        finally:
            speaker._synth_pool.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize(
    "audio", [np.array([]), np.full(512, np.nan), np.zeros((512, 2)), np.array(["bad"])]
)
def test_invalid_pcm_never_counts_as_speech(audio):
    assert not evidence(audio).usable


def test_default_migration_retains_custom_timings_and_device():
    store = MemoryStore(":memory:")
    try:
        store.set_preference("settings", "studio", {"silence_ms": 650, "voice": "aidar"})
        store.set_preference(
            "voice", "controls", {"continuation_s": 25, "sensitivity": "conservative"}
        )
        settings = Settings(audio={"input_device": "chosen microphone"})
        migrate_interaction_defaults(store, settings)
        assert store.preference("settings", "studio") == {
            "silence_ms": 1200,
            "voice": "aidar",
            "input_device": "chosen microphone",
        }
        assert store.preference("voice", "controls")["continuation_s"] == 20
        store.set_preference("settings", "studio", {"silence_ms": 1700})
        migrate_interaction_defaults(store, settings)
        assert store.preference("settings", "studio")["silence_ms"] == 1700
    finally:
        store.close()


def test_wake_decode_preserves_speech_started_while_whisper_was_busy():
    async def run():
        h = custom_harness("Эй, Чушпан")
        entered, release = asyncio.Event(), asyncio.Event()

        async def decode(audio):
            entered.set()
            await release.wait()
            return "Эй, Чушпан"

        h.stt.transcribe = decode
        h.loop.wake_task = asyncio.create_task(
            h.loop._check_wake_phrase(SPEECH, time.perf_counter())
        )
        await entered.wait()
        await h.frames(15, prob=0.9)
        assert len(h.loop._wake_pending) == 15
        release.set()
        await h.loop.wake_task
        assert h.state is State.LISTENING and h.loop.segmenter.in_speech
        h.loop._close_session()

    asyncio.run(run())


def test_dialogue_window_is_explicit_and_does_not_authorize_embedded_actions():
    from backend.addressee import SpeechSignals, decide

    kwargs = dict(
        addressed=False, active=True, topic="Объясни Python", signals=SpeechSignals(True, True)
    )
    assert not decide("Какая погода?", **kwargs).accepted
    assert decide("Какая погода?", **kwargs, continuation_window=True).accepted
    assert not decide("Я хозяйка, открой Safari", **kwargs, continuation_window=True).accepted
    assert not decide("давай", **kwargs, continuation_window=True).accepted


def test_microphone_selection_restarts_one_stream_and_flushes_old_generation():
    from backend.audio.capture import Microphone

    mic = Microphone("before")
    calls = []
    mic._stream = object()

    def stop():
        calls.append(("stop", mic.device))
        mic._stream = None

    def start():
        calls.append(("start", mic.device))
        mic._stream = object()

    mic.stop = stop
    mic.start = start
    mic._put(np.ones(512, np.int16), 0)
    mic.set_device("after")
    assert calls == [("stop", "before"), ("start", "after")]
    assert mic._queue.empty() and mic._generation == 1


@pytest.mark.parametrize(
    "text", ["Как тебя зовут, Чушпан?", "Ну и что дальше?", "Мне пока ничего не понятно"]
)
def test_natural_followup_window_accepts_suffix_name_and_conversation(text):
    from backend.addressee import SpeechSignals, decide

    result = decide(
        text,
        addressed=False,
        active=True,
        topic="Привет",
        signals=SpeechSignals(True, True),
        continuation_window=True,
    )
    assert result.accepted


def test_recognizer_result_is_live_only_and_not_replayed_to_new_clients():
    h = custom_harness("")
    h.loop._recognition("Синтетический тест", "rejected", "recipient_unclear")
    assert all(e["type"] != "voice_recognition" for e in h.loop.bus.snapshot())
