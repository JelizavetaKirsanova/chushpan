"""Data-only source configuration; never accept executable selectors or scripts."""

import ipaddress
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def web_url(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if (
        len(value) > 2048
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or any(ord(c) < 32 for c in value)
        or any(c in value for c in ('"', "\\", "<", ">"))
    ):
        raise ValueError("Нужен обычный HTTP(S) URL без логина и пароля.")
    # The backend does not fetch these URLs. Loopback is useful for local HTML5 tests.
    return value


def origin(value: str) -> str:
    parsed = urlsplit(web_url(value))
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def local_video(value: str) -> str:
    path = Path(value)
    if (
        not path.is_absolute()
        or path.suffix.lower()
        not in {
            ".mp4",
            ".m4v",
            ".mov",
            ".mkv",
            ".avi",
            ".webm",
        }
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("Укажи абсолютный путь к видеофайлу.")
    return str(path)


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default="", pattern=r"^[a-z0-9-]{0,64}$")
    name: str = Field(min_length=1, max_length=80)
    base_url: str = ""
    search_url: str = ""
    adapter: Literal["manual", "html5", "local"] = "manual"
    category: Literal["site", "streaming", "local"] = "site"
    enabled: bool = True

    @model_validator(mode="after")
    def urls(self):
        if self.adapter == "local":
            self.category = "local"
            if self.base_url or self.search_url:
                raise ValueError("Локальному источнику не нужен адрес сайта.")
            return self
        if self.category == "local":
            raise ValueError("Для файлов выбери локальный адаптер.")
        self.base_url = web_url(self.base_url)
        if self.search_url:
            if self.search_url.count("{query}") != 1:
                raise ValueError("Адрес поиска должен содержать один {query}.")
            if origin(self.search_url) != origin(self.base_url):
                raise ValueError("Поиск должен оставаться на сайте источника.")
        return self


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["movie", "series"] = "series"
    year: int = Field(default=0, ge=0, le=2200)
    poster_url: str = ""
    description: str = Field(default="", max_length=3000)
    catalogue_key: str = Field(default="", max_length=160)
    source_id: str = ""
    url: str = ""
    season: int = Field(default=1, ge=0, le=999)
    episode: int = Field(default=1, ge=0, le=9999)
    voice: str = Field(default="", max_length=100)

    @field_validator("url")
    @classmethod
    def page_url(cls, value):
        return local_video(value) if value.startswith("/") else web_url(value) if value else ""

    @field_validator("poster_url")
    @classmethod
    def poster(cls, value):
        if not value:
            return value
        # Remote artwork is only loaded after the user supplies it. No local URLs.
        value = web_url(value)
        parsed = urlsplit(value)
        host = parsed.hostname.lower()
        if parsed.scheme != "https" or host == "localhost" or "." not in host:
            raise ValueError("Для постера нужен публичный HTTPS URL.")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("Для постера нужен публичный адрес.")
        return value

    @model_validator(mode="after")
    def clean(self):
        self.title = " ".join(self.title.split())
        if not re.search(r"\w", self.title):
            raise ValueError("Укажи название.")
        if bool(self.source_id) != bool(self.url):
            raise ValueError("Укажи источник и страницу вместе.")
        if self.kind == "movie":
            self.season = self.episode = 0
        elif self.season < 1 or self.episode < 1:
            raise ValueError("Номера сезона и серии начинаются с 1.")
        return self


class Metadata(BaseModel):
    model_config = ConfigDict(extra="ignore")
    genres: list[str] = Field(default_factory=list, max_length=30)
    runtime: int = Field(default=0, ge=0, le=100000)
    rating: float = Field(default=0, ge=0, le=10, allow_inf_nan=False)
    seasons: list[dict] = Field(default_factory=list, max_length=1000)
    episode_count: int = Field(default=0, ge=0)


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    watched_threshold: float = Field(default=0.95, ge=0.5, le=1)
    auto_next: bool = False
    startup: Literal["continue", "library"] = "continue"
    poll_seconds: int = Field(default=5, ge=2, le=60)
    devices: dict[str, Literal["mac", "airplay"]] = Field(default_factory=dict)
    watch_region: str = Field(default="EE", pattern=r"^[A-Z]{2}$")
    save_progress: bool = True
