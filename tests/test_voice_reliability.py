"""No microphone, models, output devices or system commands are used here."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from backend.audio.capture import Microphone
from backend.audio.evidence import evidence
from backend.audio.stt import WhisperSTT
from backend.audio.tts import SileroSpeaker
from backend.audio.waiting import PHRASES, WaitingPhrases
from backend.hud import HudBus
from backend.state import State, StateMachine, request_context
from backend.voice import _once, reply_aloud
from tests.test_voice_loop import FRAME, FakeConversation, FakeSpeaker, Harness

SPEECH = np.tile(FRAME, 20)


@pytest.mark.parametrize("kind", ["silence", "click", "rustle"])
def test_non_speech_never_reaches_stt_or_response(kind):
    async def scenario():
        audio = np.zeros(16000, dtype=np.int16)
        if kind == "click":
            audio[2000:2003] = 30000
        if kind == "rustle":
            audio = np.random.default_rng(3).normal(0, 500, 16000).astype(np.int16)
        h = Harness(text="Спасибо", wake=False)
        h.loop.speaker = FakeSpeaker()
        await h.loop.handle(audio, 0)
        assert h.stt.calls == 0
        assert h.loop.speaker.said == []
        assert not evidence(audio).usable

    asyncio.run(scenario())


def test_real_thanks_is_not_blacklisted():
    async def scenario():
        h = Harness(text="Спасибо", wake=False)
        h.s.language = "ru-RU"
        h.loop.speaker, h.loop.conv = FakeSpeaker(), FakeConversation()
        await h.loop.handle(SPEECH, 0)
        assert h.stt.calls == 1
        assert h.loop.speaker.said
        assert h.loop.conv.notes[0][0] == "Спасибо"

    asyncio.run(scenario())


@pytest.mark.parametrize("gain", [0.01, 0.1, 1, 5])
def test_soft_and_loud_speech_are_not_rejected_by_absolute_loudness(gain):
    assert evidence((SPEECH * gain).astype(np.int16)).usable


def test_noise_with_a_speech_envelope_is_not_blindly_rejected():
    samples = np.arange(16000)
    envelope = 0.1 + np.sin(samples / 16000 * np.pi * 5) ** 2
    audio = (np.random.default_rng(1).normal(0, 2000, 16000) * envelope).astype(np.int16)
    assert evidence(audio).usable  # VAD and decoder must still decide, not loudness alone.


@pytest.mark.parametrize("source", ["tv_volume", "music", "own_tts", "addressed_user", "name_only"])
def test_barge_requires_addressed_non_echo_speech(source):
    async def scenario():
        h = Harness(wake=False)
        h.s.assistant_name = "Чушпан"
        h.loop.speaker, h.loop.conv = FakeSpeaker(), FakeConversation("Готово.")
        h.stt.text = "Чушпан, остановись" if source in {"own_tts", "addressed_user"} else "Спасибо"
        if source == "name_only":
            h.stt.text = "Чушпан"
        h.loop.speaker.recent_spoken = [h.stt.text] if source == "own_tts" else []
        h.loop.state.to(State.THINKING)
        previous = asyncio.create_task(asyncio.Event().wait())
        h.loop.task = previous
        try:
            await h.loop._validate_barge(SPEECH, previous)
            if source == "name_only":
                assert previous.cancelled()
                assert h.state is State.LISTENING
                assert h.loop.speaker.said == []
            elif source == "addressed_user":
                assert previous.cancelled()
                assert h.loop.task is not previous
                await h.loop.task
                await asyncio.sleep(0)
                assert h.state is State.LISTENING
                assert h.loop.speaker.said == ["Готово."]
            else:
                assert not previous.done()
                assert h.loop.speaker.said == []
        finally:
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)

    asyncio.run(scenario())


def test_vad_onset_alone_does_not_cancel_active_reply():
    async def scenario():
        h = Harness(wake=False)
        h.s.audio.barge_in = True
        h.loop.speaker = FakeSpeaker()
        h.loop.state.to(State.THINKING)
        h.loop.task = asyncio.create_task(asyncio.Event().wait())
        await h.frames(4, prob=0.99)
        assert not h.loop.task.done()
        h.loop.task.cancel()
        await asyncio.gather(h.loop.task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["Доброе утро", "Спокойной ночи", "Привет", "Спасибо", "Пока"])
def test_social_replies_never_start_waiting_even_if_provider_is_slow(text):
    async def scenario():
        conv, speaker = FakeConversation(), FakeSpeaker()
        conv.s.language = "ru-RU"
        conv.s.tools.one_moment_after_s = 0.001
        speaker.waiting_phrases = WaitingPhrases()
        speaker.wait_durations = {phrase: 1 for phrase in PHRASES}

        async def reply(_):
            await asyncio.sleep(0.01)
            yield "Готово."

        conv.reply = reply
        assert await reply_aloud(conv, speaker, text, 1)
        assert speaker.said == ["Готово."]

    asyncio.run(scenario())


def test_old_microphone_callbacks_are_discarded_after_flush():
    mic = Microphone()
    mic._put(FRAME, 0)
    mic.flush()
    mic._put(FRAME, 0)
    assert mic._queue.empty()
    mic._put(FRAME, 1)
    assert mic._queue.qsize() == 1


def test_frame_delivered_to_pending_get_before_flush_is_also_discarded(monkeypatch):
    async def scenario():
        mic = Microphone()
        fresh = FRAME * 2
        original = asyncio.wait_for
        first = True

        async def wait_for(awaitable, timeout):
            nonlocal first
            item = await original(awaitable, timeout)
            if first:
                first = False
                mic.flush()  # the getter already owns the old item; clearing queue is insufficient
                mic._put(fresh)
            return item

        monkeypatch.setattr(asyncio, "wait_for", wait_for)
        mic._put(FRAME)
        stream = mic.frames()
        try:
            assert np.array_equal(await anext(stream), fresh)
        finally:
            await stream.aclose()

    asyncio.run(scenario())


def test_stale_events_do_not_change_next_request_or_transcript():
    state, bus = StateMachine(State.LISTENING), HudBus()
    old = state.begin_request()
    new = state.begin_request()
    assert state.set_phase("speaking", new)
    assert not state.set_phase("waiting", old)
    bus.request_id = new
    token = request_context.set(old)
    try:
        bus.publish({"type": "reply", "delta": "stale"})
        bus.publish({"type": "activity", "phase": "waiting"})
        assert bus.snapshot() == [{"type": "transcript", "lines": []}]
    finally:
        request_context.reset(token)


def test_decoder_rejects_noise_thanks_without_rewriting_words(monkeypatch):
    stt = WhisperSTT("mock", "ru", "")
    stt._model_path = lambda: "local"
    result = {
        "text": "Спасибо",
        "segments": [
            {
                "text": "Спасибо",
                "no_speech_prob": 0.95,
                "avg_logprob": -1.5,
            }
        ],
    }
    monkeypatch.setitem(
        sys.modules, "mlx_whisper", SimpleNamespace(transcribe=lambda *a, **k: result)
    )
    try:
        assert stt._transcribe_sync(SPEECH.astype(np.float32)) == ""
        result["segments"][0]["no_speech_prob"] = 0.1
        assert stt._transcribe_sync(SPEECH.astype(np.float32)) == "Спасибо"
    finally:
        stt._executor.shutdown()


def test_cancelled_speaker_does_not_stop_successor_playback():
    async def scenario():
        speaker = SileroSpeaker(Path("unused"))
        speaker._load = lambda: None
        speaker._synth = lambda _: np.ones(480, dtype=np.int16)
        owner_stops, played = [], []

        def play(queue, *args):
            stop = queue.stop
            owner_stops.append(stop)
            while (item := queue.get()) is not None:
                if item[0] == "waiting":
                    stop.wait(1)
                elif not stop.is_set():
                    played.append(item[0])
            return not stop.is_set()

        speaker._play = play
        try:
            first = asyncio.create_task(speaker.speak(_once("waiting")))
            while not owner_stops:
                await asyncio.sleep(0.001)
            successor = asyncio.create_task(speaker.speak(_once("main")))
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            assert await asyncio.wait_for(successor, 1)
            assert played == ["main"]
            assert owner_stops[0].is_set() and not owner_stops[1].is_set()
        finally:
            speaker._synth_pool.shutdown()

    asyncio.run(scenario())


def test_completion_flushes_microphone_but_preserves_barge_utterance():
    async def scenario():
        h = Harness(wake=False)
        calls = []
        h.loop.flush_audio = lambda: calls.append(True)
        h.loop.task = asyncio.create_task(asyncio.sleep(0))
        await h.loop.task
        h.loop._after_turn(h.loop.task)
        assert calls == [True]
        h.loop._barged_in = True
        h.loop._after_turn(h.loop.task)
        assert calls == [True]

    asyncio.run(scenario())


def test_fast_successor_cannot_inherit_waiting_callback_or_cancelled_stream():
    async def scenario():
        speaker = FakeSpeaker()
        speaker.waiting_phrases = WaitingPhrases()
        speaker.wait_durations = {PHRASES[0]: 1}
        started = asyncio.Event()
        original = speaker.speak
        displayed = []
        speaker.on_sentence = lambda text, _: displayed.append(text)

        async def speak(sentences, on_start=None):
            async for sentence in sentences:
                if sentence in PHRASES:
                    started.set()
                    await asyncio.Event().wait()
                else:
                    await original(_once(sentence), on_start)
            return True

        speaker.speak = speak
        old, new = FakeConversation(), FakeConversation("Новый ответ.")
        old.s.language = new.s.language = "ru-RU"
        old.s.tools.one_moment_after_s = 0.001

        async def reply(_):
            await asyncio.Event().wait()
            yield "Устаревший ответ."

        old.reply = reply
        first = asyncio.create_task(reply_aloud(old, speaker, "Запрос", 1))
        await asyncio.wait_for(started.wait(), 1)
        successor = asyncio.create_task(reply_aloud(new, speaker, "Другой запрос", 1))
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert await asyncio.wait_for(successor, 1)
        assert speaker.said == displayed == ["Новый ответ."]

    asyncio.run(scenario())


def test_waiting_does_not_restart_between_main_answer_pieces():
    async def scenario():
        conv, speaker = FakeConversation(), FakeSpeaker()
        conv.s.language = "ru-RU"
        conv.s.tools.one_moment_after_s = 0.001
        speaker.waiting_phrases = WaitingPhrases()
        speaker.wait_durations = {phrase: 1 for phrase in PHRASES}

        async def reply(_):
            yield "Доброе утро. "
            await asyncio.sleep(0.01)
            yield "Новости готовы."

        conv.reply = reply
        assert await reply_aloud(conv, speaker, "Расскажи новости", 1)
        assert speaker.said == ["Доброе утро.", "Новости готовы."]

    asyncio.run(scenario())
