"""Voice controls and native decoder evidence; never invent an acoustic confidence score."""

import re
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SENSITIVITY = {
    "conservative": (0.8, -0.5, 0.3),
    "balanced": (0.5, -0.7, 0.45),
    "sensitive": (0.35, -0.9, 0.55),
}


class VoiceControls(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sensitivity: Literal["conservative", "balanced", "sensitive"] = "balanced"
    mode: Literal["name_only", "dialogue"] = "dialogue"
    continuation_s: int = Field(default=25, ge=10, le=300)
    allow_barge_in: bool = False
    noise_suppression: bool = True


def controls(settings):
    sensitivity = next(
        (
            name
            for name, (score, logprob, silence) in SENSITIVITY.items()
            if (
                settings.wakeword.threshold,
                settings.wakeword.min_avg_logprob,
                settings.wakeword.max_no_speech_prob,
            )
            == (score, logprob, silence)
        ),
        "balanced",
    )
    return VoiceControls(
        sensitivity=sensitivity,
        mode="dialogue" if settings.dialogue.enabled else "name_only",
        continuation_s=settings.dialogue.duration_s,
        allow_barge_in=settings.audio.barge_in,
        noise_suppression=settings.audio.noise_suppression,
    )


def apply_controls(settings, value):
    config = VoiceControls.model_validate(value)
    threshold, logprob, silence = SENSITIVITY[config.sensitivity]
    settings.wakeword = settings.wakeword.model_copy(
        update={
            "enabled": True,
            "threshold": threshold,
            "min_avg_logprob": logprob,
            "max_no_speech_prob": silence,
        }
    )
    settings.audio = settings.audio.model_copy(
        update={"barge_in": config.allow_barge_in, "noise_suppression": config.noise_suppression}
    )
    settings.dialogue = settings.dialogue.model_copy(
        update={
            "enabled": config.mode == "dialogue",
            "duration_s": config.continuation_s,
        }
    )
    return config


def decoder_confident(stt, cfg=None):
    evidence = getattr(stt, "last_confidence", None)
    if evidence is None:
        # A decoder adapter without native numerical metadata can provide its own
        # boolean verdict; the UI never turns that verdict into a probability.
        return bool(getattr(stt, "last_speech_confident", False))
    if not evidence:
        return False
    return bool(
        evidence["max_no_speech_prob"] <= (cfg.max_no_speech_prob if cfg else 0.6)
        and evidence["min_avg_logprob"] >= (cfg.min_avg_logprob if cfg else -1.0)
        and evidence["max_compression_ratio"] <= 2.4
    )


def echoes_output(stt_text, speaker, *, now, tail_s=2):
    if now - getattr(speaker, "last_audio_at", now) > tail_s:
        return False

    def clean(value):
        return " ".join(re.findall(r"\w+", value.casefold()))

    text = clean(stt_text)
    return bool(text) and any(
        text in clean(spoken) or SequenceMatcher(None, text, clean(spoken)).ratio() >= 0.85
        for spoken in getattr(speaker, "recent_spoken", [])
    )


def stop_requested(text):
    return bool(
        re.fullmatch(
            r"(?:стоп|остановись|останови(?: ответ| озвучку)?|хватит|замолчи|"
            r"перестань говорить)[.!? ]*",
            text.casefold(),
        )
    )
