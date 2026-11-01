import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from backend import stt_check
from backend.audio.capture import Microphone
from backend.audio.vad import FRAME_SAMPLES, UtteranceSegmenter, frames_for
from backend.config import Settings, VADConfig
from tests.test_vad import run


def test_default_diagnostic_never_opens_microphone(monkeypatch, capsys):
    import sounddevice

    monkeypatch.setattr(
        sounddevice,
        "query_devices",
        lambda *args: {
            "name": "Test microphone",
            "default_samplerate": 48000,
        },
    )
    monkeypatch.setattr(sounddevice, "check_input_settings", lambda **kwargs: None)
    start = Mock(side_effect=AssertionError("recording forbidden"))
    monkeypatch.setattr(stt_check.Microphone, "start", start)
    assert asyncio.run(stt_check.run(Settings())) == 0
    start.assert_not_called()
    assert "Запись выключена" in capsys.readouterr().out


def test_short_command_and_pause_keep_audio_edges():
    cfg = VADConfig(start_ms=64, min_speech_ms=160, silence_ms=1000, preroll_ms=400)
    events = run(UtteranceSegmenter(cfg), [0.0] * 20 + [0.9] * 6 + [0.0] * 35)
    final = next(event for _, event in events if event.utterance is not None)
    assert final.end_reason == "silence"
    assert final.utterance[0] < 20
    assert final.utterance[-1] == 25 + frames_for(cfg.tail_ms)
    events = run(UtteranceSegmenter(cfg), [0.9] * 6 + [0.0] * 20 + [0.9] * 10 + [0.0] * 35)
    assert sum(event.utterance is not None for _, event in events) == 1


def test_max_duration_reported():
    cfg = VADConfig(max_utterance_s=1)
    events = run(UtteranceSegmenter(cfg), [0.9] * 40)
    final = next(event for _, event in events if event.utterance is not None)
    assert final.end_reason == "max_duration"


def test_mixed_command_is_not_rewritten_and_audio_is_normalized():
    from backend.audio.stt import WhisperSTT

    stt = WhisperSTT("test", "ru", "Чушпан, YouTube, Safari, Gemini")
    seen = []

    def transcribe(audio):
        seen.append(audio)
        return "Чушпан, открой YouTube в Safari, расскажи про Gemini."

    stt._transcribe_sync = transcribe
    try:
        source = np.tile(np.array([-32768, 0, 16384, 32767], dtype=np.int16), 2048)
        text = asyncio.run(stt.transcribe(source))
        assert text == "Чушпан, открой YouTube в Safari, расскажи про Gemini."
        assert seen[0].dtype == np.float32
        np.testing.assert_allclose(seen[0], np.tile([-1, 0, 0.5, 32767 / 32768], 2048))
        np.testing.assert_array_equal(source, np.tile([-32768, 0, 16384, 32767], 2048))
    finally:
        stt._executor.shutdown(wait=True)


def test_audio_metrics_and_lost_frames():
    audio = np.array([32767, -32768, 0], dtype=np.int16)
    report = stt_check.audio_metrics(audio)
    assert report["clipped_samples"] == 2
    assert report["duration_s"] == 3 / 16000
    mic = Microphone(max_frames=1)
    mic._put(np.zeros(FRAME_SAMPLES, dtype=np.int16))
    mic._put(np.ones(FRAME_SAMPLES, dtype=np.int16))
    assert mic.dropped_frames == 1


def test_no_speech_timeout_stops_mic_without_transcribing(monkeypatch):
    import sounddevice

    monkeypatch.setattr(
        sounddevice,
        "query_devices",
        lambda *args: {
            "name": "test",
            "default_samplerate": 48000,
        },
    )
    monkeypatch.setattr(sounddevice, "check_input_settings", lambda **kwargs: None)

    async def frames():
        raise TimeoutError
        yield

    mic = SimpleNamespace(start=Mock(), stop=Mock(), frames=frames)
    monkeypatch.setattr(stt_check, "Microphone", lambda *args: mic)
    monkeypatch.setattr(stt_check, "SileroVAD", Mock())
    model = Mock(side_effect=AssertionError("no transcription"))
    monkeypatch.setattr(stt_check, "WhisperSTT", model)
    assert asyncio.run(stt_check.run(Settings(), record=True)) == 1
    mic.stop.assert_called_once()
    model.assert_not_called()
