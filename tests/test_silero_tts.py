"""Silero uses the existing player without network, model downloads or audio devices."""

from pathlib import Path

import numpy as np
import torch

from backend.audio.tts import PiperSpeaker, SileroSpeaker
from backend.config import Settings
from backend.doctor import check_voice
from backend.voice import make_speaker


class FakeSilero:
    def __init__(self):
        self.requests = []

    def apply_tts(self, **kwargs):
        self.requests.append(kwargs)
        return torch.tensor([-1.5, -0.5, 0, 0.5, 1.5])


def test_silero_uses_the_shared_player_and_audio_device(tmp_path):
    s = Settings(
        tts={"engine": "silero", "voices_dir": tmp_path, "silero_speaker": "aidar"},
        audio={"output_device": "test output"},
    )
    speaker = make_speaker(s)
    assert isinstance(speaker, SileroSpeaker)
    assert isinstance(speaker, PiperSpeaker)  # level meter, AEC and interrupt hooks stay active
    assert speaker.device == "test output"
    assert speaker.speaker == "aidar" and speaker.sample_rate == 24000
    assert speaker.voice_path == tmp_path / "silero_v5_6_ru.pt"
    assert s.voice_name == "aidar"
    speaker.interrupt()
    assert speaker.interrupted


def test_silero_stress_and_pcm_conversion():
    text = "Эй, Чушпан."
    model = FakeSilero()
    speaker = SileroSpeaker(Path("not-loaded.pt"), pronunciations={"Чушпан": "Чушпа́н"})
    speaker._voice = model
    pcm = speaker._synth(text)
    assert text == "Эй, Чушпан."  # the displayed response never gets stress markers
    assert model.requests[0]["text"] == "Эй, Чушп+ан."
    assert model.requests[0]["speaker"] == "eugene"
    assert model.requests[0]["sample_rate"] == speaker.sample_rate
    assert pcm.dtype == np.int16
    assert pcm.tolist() == [-32767, -16383, 0, 16383, 32767]


def test_silero_model_path_and_diagnostics_do_not_require_piper_json(tmp_path):
    path = tmp_path / "voice.pt"
    s = Settings(tts={"engine": "silero", "silero_model": path})
    assert s.voice_path == path
    assert not check_voice(s).ok
    path.write_bytes(b"test only")
    check = check_voice(s)
    assert check.ok and "Silero" in check.name
