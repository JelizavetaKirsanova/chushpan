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
    mode: Literal["name_only", "dialogue", "continuous"] = "dialogue"
    continuation_s: int = Field(default=20, ge=10, le=300)
    allow_barge_in: bool = False
    noise_suppression: bool = True
    vad_threshold: float | None = Field(default=None, ge=0.1, le=0.95)
    min_speech_ms: int | None = Field(default=None, ge=80, le=1000)
    background_filter: Literal["balanced", "strict"] = "balanced"


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
        mode="continuous"
        if not settings.wakeword.enabled
        else "dialogue"
        if settings.dialogue.enabled
        else "name_only",
        continuation_s=settings.dialogue.duration_s,
        allow_barge_in=settings.audio.barge_in,
        noise_suppression=settings.audio.noise_suppression,
        vad_threshold=settings.vad.threshold,
        min_speech_ms=settings.vad.min_speech_ms,
        background_filter=settings.dialogue.background_filter,
    )


def apply_controls(settings, value):
    config = VoiceControls.model_validate(value)
    threshold, logprob, silence = SENSITIVITY[config.sensitivity]
    settings.wakeword = settings.wakeword.model_copy(
        update={
            "enabled": config.mode != "continuous",
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
            "background_filter": config.background_filter,
        }
    )
    settings.vad = settings.vad.model_copy(
        update={
            **({"threshold": config.vad_threshold} if config.vad_threshold is not None else {}),
            **({"min_speech_ms": config.min_speech_ms} if config.min_speech_ms is not None else {}),
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


def migrate_interaction_defaults(store, settings=None):
    """Upgrade previous built-in timings once; retain custom timings and all other values."""
    if settings is not None:
        if settings.vad.silence_ms in {650, 700}:
            settings.vad.silence_ms = 1200
        if settings.dialogue.duration_s in {25, 45}:
            settings.dialogue.duration_s = 20
    if store.preference("voice", "interaction_defaults"):
        return
    studio = store.preference("settings", "studio")
    if studio:
        updated = dict(studio)
        if studio.get("silence_ms") in {650, 700}:
            updated["silence_ms"] = 1200
        if "input_device" not in studio and settings is not None:
            updated["input_device"] = settings.audio.input_device
        if updated != studio:
            store.set_preference("settings", "studio", updated)
    saved = store.preference("voice", "controls")
    if saved and saved.get("continuation_s") in {25, 45}:
        store.set_preference("voice", "controls", {**saved, "continuation_s": 20})
    store.set_preference("voice", "interaction_defaults", 1)
