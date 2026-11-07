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
    adapter: Literal["manual", "html5", "local", "direct"] = "manual"
    category: Literal["site", "streaming", "local"] = "site"
    enabled: bool = True
    priority: int = Field(default=0, ge=0, le=10000)
    preferred: bool = False
    search_mode: Literal["manual", "archive", "sitemap"] = "manual"
    language: str = Field(default="", max_length=40)
    access_type: Literal["unknown", "free", "subscription", "rent", "buy"] = "unknown"
    access_status: Literal["FREE", "PAID", "MIXED", "UNKNOWN"] = "UNKNOWN"
    embed_allowed: bool = False

    @model_validator(mode="before")
    @classmethod
    def legacy_cost(cls, values):
        if isinstance(values, dict) and "access_status" not in values:
            legacy = values.get("access_type", "unknown")
            values = {
                **values,
                "access_status": "UNKNOWN"
                if legacy == "unknown"
                else "FREE"
                if legacy == "free"
                else "PAID",
            }
        return values

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
        if self.search_mode == "archive" and urlsplit(self.base_url).hostname != "archive.org":
            raise ValueError("Официальный API Internet Archive доступен только на archive.org.")
        if self.adapter == "direct" and not self.embed_allowed:
            raise ValueError("Для встроенного видео нужно разрешение источника на встраивание.")
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
    original_title: str = Field(default="", max_length=500)
    original_language: str = ""
    countries: list[str] = Field(default_factory=list)
    release_date: str = ""
    popularity: float = Field(default=0, ge=0, allow_inf_nan=False)
    keywords: list[str] = Field(default_factory=list, max_length=100)
    directors: list[str] = Field(default_factory=list, max_length=40)
    cast: list[str] = Field(default_factory=list, max_length=40)
    vote_count: int = Field(default=0, ge=0)
    enriched: bool = False
    genre_ids: list[int] = Field(default_factory=list, max_length=30)
    backdrop_url: str = ""
    country_names: list[str] = Field(default_factory=list, max_length=100)
    alternative_titles: list[str] = Field(default_factory=list, max_length=100)
    imdb_id: str = Field(default="", max_length=40)
    search_enriched: bool = False
    homepage: str = ""

    @field_validator("backdrop_url")
    @classmethod
    def backdrop(cls, value):
        return Entry.poster(value)


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    watched_threshold: float = Field(default=0.95, ge=0.5, le=1)
    auto_next: bool = False
    startup: Literal["continue", "library"] = "continue"
    poll_seconds: int = Field(default=5, ge=2, le=60)
    devices: dict[str, Literal["mac", "airplay"]] = Field(default_factory=dict)
    watch_region: str = Field(default="EE", pattern=r"^[A-Z]{2}$")
    save_progress: bool = True
    recommendations_show_watched: bool = False
    preferred_language: str = Field(default="ru", max_length=40)
    free_only: bool = False
    auto_select_source: bool = True
    auto_search: bool = True
    auto_open: bool = True
    ask_paid: bool = True
    internet_search: bool = True
    search_provider: Literal["tavily", "brave", "safari"] = "tavily"
    prefer_free: bool = True
    prefer_russian: bool = True
    open_in_safari: bool = False
    autoplay: bool = True
    remember_voice: bool = True
    offer_next: bool = True
    resume_rewind_seconds: int = Field(default=3, ge=0, le=30)
    use_last_source: bool = True
    embedded_playback: bool = True
    allow_browser: bool = True
    personalize: bool = True
    russian_titles: bool = True
    hide_animation: bool = True
    hide_anime: bool = True
    hide_documentary: bool = False
    hide_watched: bool = False
    hide_watchlist: bool = False
    hide_saved: bool = False
    default_sort: Literal["popularity", "rating", "date", "title"] = "popularity"
    cards_per_row: int = Field(default=0, ge=0, le=8)
    resume_position: bool = True
    recommend_ratings: bool = True
    recommend_favorites: bool = True
    recommend_history: bool = True
    catalogue_filters: dict = Field(default_factory=dict)

    @field_validator("catalogue_filters")
    @classmethod
    def saved_catalogue_filters(cls, value):
        keys = {
            "kind",
            "genres",
            "genre_mode",
            "year",
            "year_min",
            "year_max",
            "rating",
            "country",
            "original_language",
        }
        if set(value) - keys:
            raise ValueError("Неизвестная настройка каталога.")
        clean = CatalogueQuery.model_validate(value)
        return {key: getattr(clean, key) for key in value}


class CatalogueQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feed: Literal[
        "browse",
        "trending",
        "popular_movies",
        "popular_series",
        "new",
        "upcoming",
        "top",
        "scifi",
        "comedy",
        "horror",
        "thriller",
        "mystery",
        "drama",
        "animation",
        "recommended",
        "hidden",
    ] = "browse"
    query: str = Field(default="", max_length=200)
    page: int = Field(default=1, ge=1, le=500)
    kind: Literal["all", "movie", "series"] = "all"
    genre: str = Field(default="", max_length=80)
    genres: list[str] = Field(default_factory=list, max_length=30)
    genre_mode: Literal["or", "and"] = "or"
    year: int = Field(default=0, ge=0, le=2200)
    year_min: int = Field(default=0, ge=0, le=2200)
    year_max: int = Field(default=0, ge=0, le=2200)
    russian_titles: bool | None = None
    hide_animation: bool | None = None
    hide_anime: bool | None = None
    hide_documentary: bool | None = None
    hide_watched: bool | None = None
    hide_watchlist: bool | None = None
    hide_saved: bool | None = None
    relax_restrictions: bool = False
    rating: float = Field(default=0, ge=0, le=10, allow_inf_nan=False)
    country: str = Field(default="", pattern=r"^(?:[A-Z]{2})?$")
    original_language: str = Field(default="", pattern=r"^(?:[a-z]{2})?$")
    sort: Literal["popularity", "rating", "date", "title"] = "popularity"

    @model_validator(mode="after")
    def years(self):
        if self.year_min and self.year_max and self.year_min > self.year_max:
            raise ValueError("Начало диапазона годов не может быть позже конца.")
        return self

    @field_validator("genres")
    @classmethod
    def selected_genres(cls, values):
        if any(not value.strip() or len(value) > 80 for value in values):
            raise ValueError("Некорректный жанр.")
        return list(dict.fromkeys(value.strip().casefold() for value in values))
