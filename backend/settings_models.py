"""Validated local UI preferences; no credentials or arbitrary executable settings."""

from typing import Literal

from pydantic import Field, model_validator

from backend.brain.privacy import contains_secret
from backend.personality import Strict


class Studio(Strict):
    voice: str = Field(default="eugene", max_length=80)
    rate: Literal["x-slow", "slow", "medium", "fast", "x-fast"] = "medium"
    pitch: Literal["x-low", "low", "medium", "high", "x-high"] = "medium"
    volume: int = Field(default=100, ge=0, le=100)
    pause_ms: int = Field(default=0, ge=0, le=1500)
    pronunciations: dict[str, str] = Field(default_factory=dict, max_length=200)
    normalize_numbers: bool = True
    normalize_english: bool = True
    wake_phrase: str = Field(default="эй чушпан", min_length=2, max_length=80)
    silence_ms: int = Field(default=650, ge=350, le=2000)
    adaptive_endpoint: bool = True
    text_only: bool = False

    @model_validator(mode="after")
    def safe_dictionary(self):
        if any(
            not k.strip() or not v.strip() or len(k) > 80 or len(v) > 180
            for k, v in self.pronunciations.items()
        ) or contains_secret(self.model_dump_json()):
            raise ValueError("Проверь словарь: до 200 слов, без секретов и пустых значений.")
        return self


class MemoryPolicy(Strict):
    automatic: Literal["auto", "ask", "off"] = "auto"
    excluded: list[
        Literal["profile", "preference", "habit", "project", "episode", "correction"]
    ] = Field(default_factory=list)
    temporary_days: int = Field(default=30, ge=1, le=3650)


class DialogueExtras(Strict):
    nameless_interruptions: bool = False
    addressee_strictness: Literal["contextual", "addressed"] = "contextual"


class Models(Strict):
    provider: Literal["ollama", "gemini"] = "gemini"
    local_model: str = Field(default="qwen3:8b", pattern=r"^[a-zA-Z0-9_.:/-]{1,120}$")
    cloud_model: Literal[
        "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"
    ] = "gemini-3.8-flash"
    fallback: bool = True
    cloud_fallback: Literal["", "gemini-3.5-flash"] = ""
    mode: Literal["fast", "balanced", "quality"] = "balanced"


class Access(Strict):
    disabled_tools: list[str] = Field(default_factory=list, max_length=100)
    blocked_apps: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def safe_names(self):
        if any(not s.strip() or len(s) > 120 for s in self.disabled_tools + self.blocked_apps):
            raise ValueError("Неверное имя действия или приложения.")
        return self


class QuietHours(Strict):
    enabled: bool = False
    start: str = Field(default="23:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(default="07:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekdays: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1, max_length=7)
    mute: bool = True
    pause_initiative: bool = True

    @model_validator(mode="after")
    def valid_days(self):
        if len(set(self.weekdays)) != len(self.weekdays) or any(
            type(d) is not int or not 0 <= d <= 6 for d in self.weekdays
        ):
            raise ValueError("Проверь дни недели.")
        return self


class Notifications(Strict):
    reminders: bool = True
    scenarios: bool = False


class AboutMe(Strict):
    name: str = Field(default="", max_length=80)
    address: str = Field(default="", max_length=80)
    preferences: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def no_secrets(self):
        if contains_secret(self.model_dump_json()):
            raise ValueError("Не сохраняй секреты в профиле пользователя.")
        return self


CATEGORIES = {
    "studio": Studio,
    "memory": MemoryPolicy,
    "models": Models,
    "access": Access,
    "quiet": QuietHours,
    "notifications": Notifications,
    "about": AboutMe,
}


def quiet_now(store, settings, now=None):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    now = now or datetime.now(ZoneInfo(settings.location.tz_name))
    q = QuietHours.model_validate(store.preference("settings", "quiet") or {})
    clock = now.strftime("%H:%M")
    overnight = q.start >= q.end
    owner_day = (
        (now - timedelta(days=1)).weekday() if overnight and clock < q.end else now.weekday()
    )
    within = (clock >= q.start or clock < q.end) if overnight else q.start <= clock < q.end
    return q if q.enabled and owner_day in q.weekdays and within else None


def silent_reply(store, settings):
    studio = store.preference("settings", "studio") or {}
    quiet = quiet_now(store, settings)
    return (
        studio.get("text_only") is True or studio.get("volume") == 0 or bool(quiet and quiet.mute)
    )


def apply_persisted(settings, store):
    """Applied before model/voice initialization, without rewriting config files."""
    settings._calendar_disabled = store.preference("dashboard", "calendar_enabled") is False
    if not hasattr(settings, "_ui_performance_base"):
        settings._ui_performance_base = {
            "think": settings.llm.think,
            "think_for_simple": settings.llm.think_for_simple,
            "simple_timeout_s": settings.gemini.simple_timeout_s,
            "first_result_budget_s": settings.gemini.first_result_budget_s,
        }
    if value := store.preference("settings", "models"):
        model = Models.model_validate(value)
        settings.llm.provider, settings.llm.model, settings.llm.fallback = (
            model.provider,
            model.local_model,
            model.fallback,
        )
        settings.gemini.model = model.cloud_model
        settings.gemini.fallback_model = model.cloud_fallback or None
        base = settings._ui_performance_base
        settings.llm.think, settings.llm.think_for_simple = base["think"], base["think_for_simple"]
        settings.gemini.simple_timeout_s = base["simple_timeout_s"]
        settings.gemini.first_result_budget_s = base["first_result_budget_s"]
        if model.mode == "fast":
            settings.llm.think_for_simple = False
            settings.gemini.simple_timeout_s = min(settings.gemini.simple_timeout_s, 2)
            settings.gemini.first_result_budget_s = min(settings.gemini.first_result_budget_s, 5)
        elif model.mode == "quality":
            settings.llm.think = True
            settings.llm.think_for_simple = True
            settings.gemini.first_result_budget_s = settings.gemini.timeout_s
    if value := store.preference("settings", "studio"):
        studio = Studio.model_validate(value)
        if settings.tts.engine == "silero":
            settings.tts.silero_speaker = studio.voice
        elif settings.tts.engine == "piper":
            settings.tts.length_scale = {
                "x-slow": 1.4,
                "slow": 1.2,
                "medium": 1,
                "fast": 0.85,
                "x-fast": 0.7,
            }[studio.rate]
        settings.tts.pronunciations = dict(studio.pronunciations)
        settings.wakeword.phrase = studio.wake_phrase
        settings.vad.silence_ms, settings.vad.adaptive_endpoint = (
            studio.silence_ms,
            studio.adaptive_endpoint,
        )
    if value := store.preference("settings", "about"):
        about = AboutMe.model_validate(value)
        settings.user.name = about.name or None
        settings.user.form_of_address = about.address or None
    if value := store.preference("voice", "dialogue"):
        extras = DialogueExtras.model_validate(value)
        settings.dialogue = settings.dialogue.model_copy(update=extras.model_dump())
