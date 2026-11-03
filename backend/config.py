"""Settings: config.yaml < config.local.yaml < .env < environment variables."""

from __future__ import annotations

import os
from datetime import tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from backend.i18n import Locale, get_locale, normalize_code
from backend.paths import DESKTOP, RESOURCE_ROOT, USER_ROOT

ROOT_DIR = RESOURCE_ROOT
CONFIG_FILE = ROOT_DIR / "config.yaml"
LOCAL_CONFIG_FILE = USER_ROOT / "config.local.yaml"
ENV_FILE = USER_ROOT / ".env"
WAKEWORD_VERSION = "v0.1"  # openWakeWord pre-trained model version

# Flat variables for personal data, mapped onto the nested settings.
SIMPLE_ENV: dict[str, tuple[str, str]] = {
    "JARVIS_OWNER_NAME": ("user", "name"),
    "JARVIS_OWNER_GENDER": ("user", "gender"),
    "JARVIS_OWNER_TITLE": ("user", "form_of_address"),
    "JARVIS_LANGUAGE": ("", "language"),
    "JARVIS_CITY": ("location", "query"),
    "JARVIS_LATITUDE": ("location", "latitude"),
    "JARVIS_LONGITUDE": ("location", "longitude"),
    "JARVIS_TIMEZONE": ("location", "timezone"),
    "YOUTUBE_API_KEY": ("youtube_video", "api_key"),
    "YOUTUBE_SLEEP_PLAYLIST_URL": ("youtube_video", "playlist_url"),
    "YOUTUBE_CACHE_TTL": ("youtube_video", "cache_ttl"),
}


class LocationConfig(BaseModel):
    query: str | None = None  # e.g. "London, England, United Kingdom"
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    timezone: str | None = None  # None = system timezone

    @property
    def resolved(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def display_name(self) -> str | None:
        return self.name or self.query

    @property
    def tz(self) -> tzinfo:
        if self.timezone:
            return ZoneInfo(self.timezone)
        from datetime import datetime

        return datetime.now().astimezone().tzinfo  # type: ignore[return-value]

    @property
    def tz_name(self) -> str:
        return self.timezone or str(self.tz)


class UserConfig(BaseModel):
    name: str | None = None
    gender: str = "neutral"  # female | male | neutral (Portuguese values also accepted)
    form_of_address: str | None = None  # None = language default (madam/sir, senhora/senhor)
    address_mode: Literal["ты", "вы"] = "ты"

    @property
    def kind(self) -> str:
        g = self.gender.strip().lower()
        if g.startswith(("f", "w", "ж")):
            return "f"
        if g.startswith(("m", "м")):
            return "m"
        return "n"


class PersonaConfig(BaseModel):
    file: Path | None = None  # Relative to the project root, or an absolute path
    style: str | None = None  # Optional additions to the file; otherwise locale default
    max_sentences: int = 3


class MacConfig(BaseModel):
    # Trusted configuration, never roots supplied by the LLM or a remote client.
    file_roots: dict[str, Path] = Field(
        default_factory=lambda: {
            "documents": Path.home() / "Documents",
            "desktop": Path.home() / "Desktop",
            "downloads": Path.home() / "Downloads",
        }
    )


class LLMConfig(BaseModel):
    provider: Literal["gemini", "ollama"] = "gemini"
    fallback: bool = True
    host: str = "http://localhost:11434"
    model: str = "qwen3.5:9b-q4_K_M"
    keep_alive: str = "30m"
    temperature: float = 0.6
    think: bool = True
    think_for_simple: bool = False  # only a conservative allowlist; complex requests keep think
    num_ctx: int = Field(default=16384, ge=1024)  # persona + tools + conversation + reply
    context_messages: int = 30  # a turn with a tool call takes 4: question, call, result, answer


class GeminiConfig(BaseModel):
    model: Literal[
        "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"
    ] = "gemini-3.8-flash"
    fallback_model: Literal["gemini-3.5-flash"] | None = None
    # The API key cannot tell us whether Cloud Billing is linked to its project.
    free_tier_confirmed: bool = False
    timeout_s: float = Field(default=15, ge=5, le=120)
    attempt_timeout_s: float = Field(default=6, ge=1, le=120)
    stream_idle_timeout_s: float = Field(default=4, ge=1, le=30)
    first_result_budget_s: float = Field(default=8, ge=1, le=120)
    simple_timeout_s: float = Field(default=3, ge=1, le=30)
    simple_use_fallback_model: bool = True  # plain requests use configured secondary, then local
    max_output_tokens: int = Field(default=2048, ge=128, le=8192)
    context_messages: int = Field(default=24, ge=0, le=30)
    context_chars: int = Field(default=24000, ge=4000, le=100000)


class ShortcutEntry(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}
    identifier: str = Field(pattern=r"^[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}$")
    description: str = Field(min_length=1, max_length=300)
    input_type: Literal["none", "text", "url"] = "none"
    # Explicit manual review only; existing entries retain mandatory confirmation.
    safe_without_confirmation: bool = False
    # Review the Shortcut again after editing it; its internals can change independently.


class ShortcutsConfig(BaseModel):
    allowed: dict[str, ShortcutEntry] = Field(default_factory=dict)
    timeout_s: float = Field(default=30, ge=1, le=120)


class NightConfig(BaseModel):
    # Manual review of Shortcuts' own grants; not a claim about TCC state.
    shortcut_permissions_reviewed: bool = False
    external_uuid: str | None = Field(default=None, pattern=r"^[A-Fa-f0-9-]{36}$")
    external_control: Literal["brightness", "softwareBrightness", "hardwareBrightness"] = (
        "brightness"
    )

    @field_validator("external_uuid")
    @classmethod
    def valid_uuid(cls, value: str | None) -> str | None:
        return str(UUID(value)) if value else None


class LEDConfig(BaseModel):
    enabled: bool = False
    name: str = "ELK-BLEDOB"
    firmware: str = "JSY-P10A27NM-V1.1.2"
    address: UUID = UUID("3A4F5301-2291-2E56-628D-83567E951876")
    service: UUID = UUID("0000fff0-0000-1000-8000-00805f9b34fb")
    write_characteristic: UUID = UUID("0000fff3-0000-1000-8000-00805f9b34fb")
    notify_characteristic: UUID = UUID("0000fff4-0000-1000-8000-00805f9b34fb")
    connect_timeout_s: float = Field(default=12, ge=1, le=30)
    write_timeout_s: float = Field(default=5, ge=0.1, le=15)
    settle_s: float = Field(default=1, ge=0.1, le=3)
    night_off: bool = True
    comfortable_brightness: int | None = Field(default=None, ge=0, le=100)


class STTConfig(BaseModel):
    model: str = "mlx-community/whisper-small-mlx"
    language: str | None = None  # None = assistant language


class TTSConfig(BaseModel):
    engine: str = "piper"
    voice: str | None = None  # None = language default
    voices_dir: Path = Path("models/voices")
    say_voice: str | None = None
    length_scale: float | None = None  # speaking speed: < 1 faster, > 1 slower
    sentence_min_chars: int = 20
    pronunciations: dict[str, str] = Field(default_factory=dict)
    silero_model: Path = Path("silero_v5_6_ru.pt")  # in voices_dir, or an absolute path
    silero_speaker: str = "eugene"  # eugene | aidar (male); baya | kseniya | xenia (female)


class AudioConfig(BaseModel):
    input_device: str | int | None = None  # None = system default
    output_device: str | int | None = None
    # Interrupt Jarvis by speaking. With echo cancellation (Piper voice), no headphones needed.
    barge_in: bool = False
    # With barge_in: remove Jarvis's own voice from the microphone (WebRTC AEC3).
    echo_cancellation: bool = True
    noise_suppression: bool = True


class VADConfig(BaseModel):
    threshold: float = 0.5
    start_ms: int = 96
    min_speech_ms: int = 300
    silence_ms: int = 1200  # pause that ends an utterance
    adaptive_endpoint: bool = True  # preserve the configured floor; allow natural longer pauses
    preroll_ms: int = 300
    tail_ms: int = Field(default=256, ge=0, le=2000)
    max_utterance_s: float = 60


class DialogueConfig(BaseModel):
    enabled: bool = True
    duration_s: int = Field(default=20, ge=10, le=300)
    nameless_interruptions: bool = False
    addressee_strictness: Literal["contextual", "addressed"] = "contextual"
    max_session_s: float = Field(default=180, ge=30, le=900)


class WakeWordConfig(BaseModel):
    enabled: bool = True
    phrase: str | None = None  # Custom phrase recognized locally by Whisper; None = ONNX model
    model: str = "hey_jarvis"  # openWakeWord pre-trained name, or a path to a custom .onnx
    models_dir: Path = Path("models/wakeword")
    threshold: float = Field(default=0.5, ge=0.05, le=0.99)
    min_avg_logprob: float = Field(default=-0.7, ge=-3, le=0)
    max_no_speech_prob: float = Field(default=0.45, ge=0, le=1)
    cooldown_s: float = Field(default=3, ge=0.5, le=30)
    check_interval_s: float = Field(default=1, ge=0, le=10)
    listen_timeout_s: float = 6  # after the wake word, how long to wait for a request
    follow_up_s: float = 8  # after a reply, keep listening without the wake word
    chime: bool = True


class ToolsConfig(BaseModel):
    control_context_s: int = Field(default=60, ge=5, le=180)
    # weather | news | system | timers | clock | memory | media. Remove one to hide it.
    enabled: list[str] = Field(
        default_factory=lambda: [
            "weather",
            "news",
            "system",
            "timers",
            "clock",
            "memory",
            "media",
        ]  # fmt: skip
    )
    max_rounds: int = 3  # tool calls the model may chain before it must answer
    # Say "one moment" when the model is silent this long (it is probably calling a tool).
    one_moment_after_s: float = 1.5
    quick_read_timeout_s: float = Field(default=3, ge=1, le=30)


class PlaylistConfig(BaseModel):
    id: str = ""
    title: str = ""


class YouTubeMusicConfig(BaseModel):
    enabled: bool = True
    transport: Literal["safari", "webapp"] = "safari"
    bundle_id: str | None = None  # discover Safari Web App by its manifest
    app_path: Path | None = None
    auth_file: Path | None = None  # optional; library otherwise read from the signed-in app
    timeout_s: float = Field(default=12, ge=3, le=60)
    main_playlist: PlaylistConfig = Field(default_factory=PlaylistConfig)
    scenarios: dict[str, PlaylistConfig] = Field(default_factory=dict)


class YouTubeVideoConfig(BaseModel):
    api_key: SecretStr = SecretStr("")
    playlist_url: str = ""
    cache_ttl: int = Field(default=21600, ge=60, le=2592000)
    timeout_s: float = Field(default=6, ge=1, le=15)
    max_requests: int = Field(default=100, ge=3, le=500)
    mode: Literal["random", "order", "longest", "shortest"] = "random"
    min_seconds: int = Field(default=0, ge=0, le=86400)
    max_seconds: int = Field(default=86400, ge=1, le=604800)
    exclude_recent_hours: int = Field(default=24, ge=0, le=8760)
    allow_repeat: bool = False
    preferred_screen: Literal["external", "current"] = "external"
    external_uuid: str | None = None  # Defaults to night.external_uuid, never a display index.
    fullscreen: bool = True
    night_autoplay: bool = False  # Explicit opt-in; existing night routine is preserved.

    @field_validator("external_uuid")
    @classmethod
    def valid_uuid(cls, value: str | None) -> str | None:
        return str(UUID(value)) if value else None

    @model_validator(mode="after")
    def duration_bounds(self) -> YouTubeVideoConfig:
        if self.min_seconds > self.max_seconds:
            raise ValueError("YouTube: min_seconds должен быть <= max_seconds")
        return self


class MediaConfig(BaseModel):
    provider: Literal["youtube_music", "spotify"] | None = "youtube_music"
    youtube_music: YouTubeMusicConfig = Field(default_factory=YouTubeMusicConfig)
    # Spotify's volume while Jarvis listens and speaks, as a fraction of its own; 0 = off.
    duck_to: float = 0.2
    search_wait_s: float = 6  # how long to wait for the search results to show
    play_volume: int | None = 75  # Spotify's volume for a song asked for; None = keep


class NewsConfig(BaseModel):
    cache_minutes: int = 30
    max_items: int = 5
    feeds: list[str] = Field(default_factory=list)


class BriefingConfig(BaseModel):
    enabled: bool = True  # weather and AI news on start (`voice --no-briefing` skips it)
    headlines: int = 6  # candidates the model picks the one or two most relevant from
    fetch_timeout_s: float = 5  # a source slower than this is left out
    network_wait_s: float = 20  # at login Wi-Fi may join late; then weather and news are skipped
    once_a_day: bool = True  # later starts on the same day open quietly (D-50)
    morning_volume: int = Field(default=20, ge=0, le=100)


class ResourcesConfig(BaseModel):
    """How much memory the voice assistant holds while nobody talks to it (D-37)."""

    # performance: everything always loaded · balanced: unload the model and Whisper after
    # `idle_minutes` asleep · economy: unload them as soon as Jarvis goes to sleep
    profile: Literal["performance", "balanced", "economy"] = "balanced"
    idle_minutes: float = 10
    mlx_cache_mb: int = 64  # MLX keeps freed GPU buffers to reuse; uncapped it held ~850 MB

    @property
    def unload_after_s(self) -> float | None:
        """Seconds asleep before unloading; None = never."""
        if self.profile == "performance":
            return None
        return 0.0 if self.profile == "economy" else self.idle_minutes * 60

    @property
    def keep_alive(self) -> str:
        """What Ollama is told: a safety net in case Jarvis ends without unloading it."""
        if self.profile == "performance":
            return "-1"
        return f"{round(self.idle_minutes if self.profile == 'balanced' else 0) + 5}m"


class ServerConfig(BaseModel):
    # Existing HUD has no network authentication. Remote API belongs to a later stage.
    host: Literal["127.0.0.1", "localhost"] = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)


class HudConfig(BaseModel):
    enabled: bool = True  # `voice --no-hud` turns it off for one run
    window: bool = True  # a native window; False = a browser tab
    open_on_start: bool = True  # unless a HUD page left open reconnects first
    # The name of `open_on_start` until phase 7, when the HUD got its own window. Declared
    # so pydantic-settings keeps JARVIS_HUD__OPEN_BROWSER (it drops unknown nested keys).
    open_browser: bool | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _renamed(self) -> HudConfig:
        if self.open_browser is not None:
            self.open_on_start = self.open_browser
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JARVIS_",
        env_nested_delimiter="__",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        yaml_file=(CONFIG_FILE, LOCAL_CONFIG_FILE),
        yaml_file_encoding="utf-8",
        extra="ignore",
    )

    assistant_name: str = "Чушпан"
    language: str = "ru-RU"
    log_level: str = "INFO"
    data_dir: Path = Path("data")

    user: UserConfig = Field(default_factory=UserConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    mac: MacConfig = Field(default_factory=MacConfig)
    location: LocationConfig = Field(default_factory=LocationConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    shortcuts: ShortcutsConfig = Field(default_factory=ShortcutsConfig)
    night: NightConfig = Field(default_factory=NightConfig)
    led: LEDConfig = Field(default_factory=LEDConfig)
    youtube_video: YouTubeVideoConfig = Field(default_factory=YouTubeVideoConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    vad: VADConfig = Field(default_factory=VADConfig)
    wakeword: WakeWordConfig = Field(default_factory=WakeWordConfig)
    dialogue: DialogueConfig = Field(default_factory=DialogueConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    briefing: BriefingConfig = Field(default_factory=BriefingConfig)
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    hud: HudConfig = Field(default_factory=HudConfig)

    @property
    def data_path(self) -> Path:
        path = (
            USER_ROOT / "data"
            if DESKTOP
            else self.data_dir
            if self.data_dir.is_absolute()
            else USER_ROOT / self.data_dir
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    @field_validator("language")
    @classmethod
    def _normalize_language(cls, v: str) -> str:
        return normalize_code(v)

    # Language-dependent values
    @property
    def locale(self) -> Locale:
        return get_locale(self.language)

    @property
    def persona_style(self) -> str:
        parts = []
        if self.persona.file is not None:
            path = self.persona.file
            if not path.is_absolute():
                path = USER_ROOT / path if (USER_ROOT / path).exists() else ROOT_DIR / path
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                raise ValueError(f"Файл личности пуст: {path}")
            parts.append(text)
        if self.persona.style:
            parts.append(self.persona.style)
        return "\n\n".join(parts) or self.locale.persona

    @property
    def owner_address(self) -> str | None:
        """How the assistant addresses the owner: a title (madam, senhora...) or their name."""
        u = self.user
        return u.form_of_address or self.locale.titles.get(u.kind) or u.name

    @property
    def owner_full_address(self) -> str | None:
        return self.locale.full_address(self.owner_address, self.user.name)

    @property
    def voice_name(self) -> str:
        if self.tts.engine == "silero":
            return self.tts.silero_speaker
        return self.tts.voice or self.locale.voice

    @property
    def voice_path(self) -> Path:
        d = self.tts.voices_dir
        base = d if d.is_absolute() else USER_ROOT / d
        if self.tts.engine == "silero":
            model = self.tts.silero_model
            return model if model.is_absolute() else base / model
        return base / f"{self.voice_name}.onnx"

    @property
    def stt_language(self) -> str:
        return self.stt.language or self.locale.lang

    @property
    def stt_hint(self) -> str:
        hint = self.locale.stt_hint.format(name=self.assistant_name)
        if self.stt_language == "ru":
            hint += " YouTube, Safari, Gemini, Ollama, GitHub, BetterDisplay."
        return hint

    @property
    def wakeword_dir(self) -> Path:
        d = self.wakeword.models_dir
        return d if d.is_absolute() else USER_ROOT / d

    @property
    def wakeword_path(self) -> Path:
        model = self.wakeword.model
        if model.endswith(".onnx"):
            path = Path(model)
            return path if path.is_absolute() else USER_ROOT / path
        return self.wakeword_dir / f"{model}_{WAKEWORD_VERSION}.onnx"

    @property
    def db_path(self) -> Path:
        return self.data_path / "jarvis.db"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Priority: init args > flat vars > env > .env > config.yaml
        env_file = getattr(dotenv_settings, "env_file", ENV_FILE)
        return (
            init_settings,
            SimpleEnvSource(settings_cls, env_file),
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
        )


class SimpleEnvSource(PydanticBaseSettingsSource):
    """Reads JARVIS_OWNER_NAME, JARVIS_CITY, etc. from the environment and .env."""

    def __init__(self, settings_cls: type[BaseSettings], env_file: Any) -> None:
        super().__init__(settings_cls)
        self.env_file = Path(env_file) if env_file else None

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if self.env_file and self.env_file.exists():
            values.update(dotenv_values(self.env_file))
        values.update(os.environ)
        out: dict[str, dict[str, Any]] = {}
        for var, (section, key) in SIMPLE_ENV.items():
            value = values.get(var)
            if value in (None, ""):
                continue
            if section:
                out.setdefault(section, {})[key] = value.strip()
            else:
                out[key] = value.strip()
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
