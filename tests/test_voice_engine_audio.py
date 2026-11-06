"""Engine 2 audio regressions; synthetic frames and fake streams only."""

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from backend.audio.capture import Microphone
from backend.audio.sentences import SentenceSplitter
from backend.audio.tts import speech_text
from backend.audio.vad import FRAME_MS, SegmentEvent, UtteranceSegmenter
from backend.config import VADConfig
from backend.latency import Profile
from tests.test_voice_loop import FRAME, FakeConversation, FakeSpeaker, Harness


def test_adaptive_pause_works_above_default_floor_and_preserves_long_command():
    seg = UtteranceSegmenter(VADConfig())
    for _ in range(120):
        seg.process(FRAME, 0.9)
    assert 1400 <= seg.endpoint_frames * FRAME_MS <= 1850
    for _ in range(40):  # 1280 ms: longer than old endpoint, within new endpoint
        assert seg.process(FRAME, 0).utterance is None
    for _ in range(20):
        assert seg.process(FRAME, 0.9).utterance is None
    events = [seg.process(FRAME, 0) for _ in range(60)]
    assert sum(e.utterance is not None for e in events) == 1


def test_short_command_keeps_configured_endpoint():
    cfg = VADConfig(silence_ms=1600)
    seg = UtteranceSegmenter(cfg)
    for _ in range(15):
        seg.process(FRAME, 0.9)
    assert seg.endpoint_frames * FRAME_MS == 1600


def test_microphone_closes_after_stop_error_and_ignores_old_stream(monkeypatch):
    async def run():
        callbacks, streams = [], []

        def make_stream(**kwargs):
            callbacks.append(kwargs["callback"])
            stream = SimpleNamespace(start=Mock(), stop=Mock(side_effect=OSError), close=Mock())
            streams.append(stream)
            return stream

        monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=make_stream))
        mic = Microphone()
        mic.start()
        mic.set_device("new fake input")
        streams[0].close.assert_called_once()
        callbacks[0](FRAME[:, None], 512, None, None)
        callbacks[1](FRAME[:, None], 512, None, None)
        await asyncio.sleep(0)
        assert mic._queue.qsize() == 1
        # A flush keeps the same live stream usable.
        mic.flush()
        callbacks[1](FRAME[:, None], 512, None, None)
        await asyncio.sleep(0)
        assert mic._queue.qsize() == 1
        mic.stop()
        assert mic._stream is None
        streams[1].close.assert_called_once()

    asyncio.run(run())


def test_callbacks_already_queued_before_restart_cannot_enter_new_stream(monkeypatch):
    async def run():
        callbacks = []

        def create(**kwargs):
            callbacks.append(kwargs["callback"])
            return SimpleNamespace(start=Mock(), stop=Mock(), close=Mock())

        monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=create))
        mic = Microphone()
        mic.start()
        callbacks[0](FRAME[:, None], 512, None, None)
        mic.stop()
        mic.start()
        await asyncio.sleep(0)
        assert mic._queue.empty()
        mic.stop()

    asyncio.run(run())


def test_unpunctuated_stream_starts_before_end_and_preserves_every_word():
    words = ("Длинная фраза без знаков препинания " * 50).split()
    splitter = SentenceSplitter(protect_literals=True)
    chunks, first = [], None
    for index, word in enumerate(words):
        emitted = splitter.feed(word + " ")
        if emitted and first is None:
            first = index
        chunks.extend(emitted)
    chunks.append(splitter.flush())
    assert first < len(words) // 4
    assert " ".join(chunks).split() == words


def test_stream_does_not_split_long_url_or_decimal():
    splitter = SentenceSplitter(protect_literals=True, max_chars=80)
    url = "https://example.test/" + "a" * 100
    pieces = splitter.feed("Проверь ссылку " + url + " и температуру 12.5 градусов " * 5)
    pieces.append(splitter.flush())
    assert any(url in p for p in pieces)
    assert not any(p.endswith("12.") for p in pieces)


def test_name_stress_is_only_in_speech_copy_and_can_be_overridden():
    original = "Чушпан открыл Safari и поставил громкость 25%."
    prepared = speech_text(original, {}, "ru")
    assert "Чу́шпан" in prepared and "Сафари" in prepared and "двадцать пять" in prepared
    assert original == "Чушпан открыл Safari и поставил громкость 25%."
    assert "Чушпа́н" in speech_text(original, {"Чушпан": "Чушпа́н"}, "ru")


def test_latency_metrics_keep_absent_stages_unmeasured_and_correct_speech_offset():
    p = Profile(origin=10)
    p.ended = 20
    p.marks["user_speech_end"] = 2
    assert p.snapshot()["metrics"]["speech_end_to_completed_s"] == 8
    assert p.summary()["wake_s"] is None
    assert p.summary()["routing_s"] is None
    assert "private" not in json.dumps(p.snapshot())


def test_truncated_utterance_does_not_reach_model_or_tools():
    async def run():
        h = Harness(text="Выключи ленту и", wake=False)
        h.loop.conv, h.loop.speaker = FakeConversation("Нельзя выполнить"), FakeSpeaker()
        h.loop._last_segment = SegmentEvent(end_reason="max_duration")
        await h.loop.handle(FRAME, 0)
        assert h.stt.calls == 0
        assert h.loop.speaker.said == []

    asyncio.run(run())


@pytest.mark.parametrize("prob", [0, 0.2, 0.49])
def test_noise_never_begins_an_utterance(prob):
    seg = UtteranceSegmenter(VADConfig())
    for _ in range(300):
        event = seg.process(np.zeros_like(FRAME), prob)
        assert not event.started and event.utterance is None
