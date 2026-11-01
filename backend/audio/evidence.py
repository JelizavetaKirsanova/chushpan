"""Conservative acoustic checks; never infer words or identify a speaker."""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class AudioEvidence:
    duration_s: float
    rms: float
    active_ms: float
    stationary_noise: bool = False

    @property
    def usable(self) -> bool:
        # Digital silence/DC and isolated clicks must not reach a generative decoder.
        # One PCM count is the quantisation floor, not a microphone sensitivity setting.
        return self.rms > 1 and self.active_ms >= 160 and not self.stationary_noise


def evidence(audio: np.ndarray) -> AudioEvidence:
    samples = audio.astype(np.float32)
    frames = samples[: len(samples) // 512 * 512].reshape(-1, 512)
    rms = float(np.std(samples)) if len(samples) else 0.0
    active = np.std(frames, axis=1) > 1 if len(frames) else np.array([])
    noise = False
    if np.sum(active) >= 5:
        voiced = frames[active]
        energy = np.std(voiced, axis=1)
        power = np.abs(np.fft.rfft(voiced * np.hanning(512), axis=1)) ** 2 + 1e-12
        flatness = np.exp(np.mean(np.log(power), axis=1)) / np.mean(power, axis=1)
        # Conservative joint check: sustained broadband energy with no speech envelope.
        noise = bool(np.median(flatness) > 0.45 and np.std(energy) / np.mean(energy) < 0.15)
    return AudioEvidence(len(samples) / 16000, rms, float(np.sum(active)) * 32, noise)
