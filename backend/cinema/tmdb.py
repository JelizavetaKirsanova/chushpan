"""Official TMDB metadata only. Credentials never enter snapshots, tools or URLs in logs."""

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import time
from collections import OrderedDict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from backend.tools.registry import ToolError

ATTRIBUTION = "This product uses the TMDB API but is not endorsed or certified by TMDB."


class CredentialLogFilter(logging.Filter):
    def filter(self, record):
        # httpx logs URLs at INFO even for successful requests. v3 keys must use
        # a query parameter; redact that parameter before any handler sees it.
        if record.args and not isinstance(record.args, dict):
            record.args = tuple(
                re.sub(r"([?&]api_key=)[^&\s]+", r"\1[redacted]", str(arg))
                if isinstance(arg, (str, httpx.URL)) and "api.themoviedb.org/" in str(arg)
                else arg
                for arg in record.args
            )
        return True


logging.getLogger("httpx").addFilter(CredentialLogFilter())


class TMDBRateLimited(ToolError):
    def __init__(self, seconds):
        self.retry_after = max(1, math.ceil(seconds))
        super().__init__(f"Лимит TMDB: повтори через {self.retry_after} с.")


def retry_delay(value):
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            seconds = 5
    return max(1, math.ceil(seconds)) if math.isfinite(seconds) else 5


class TMDBCredentials:
    """An owner-only integration file outside source control and ordinary settings exports."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "tmdb.json"

    @property
    def configured(self):
        return self.path.is_file()

    def read(self):
        if not self.configured:
            raise ToolError("Подключи TMDB в настройках интеграций кинотеки.")
        try:
            return json.loads(self.path.read_text())["credential"]
        except (OSError, ValueError, KeyError):
            raise ToolError("Не удалось прочитать ключ TMDB. Сохрани его повторно.") from None

    def save(self, credential):
        if not isinstance(credential, str):
            raise ToolError("Нужен API-ключ или Read Access Token TMDB.")
        credential = credential.strip()
        if not credential:
            self.path.unlink(missing_ok=True)
            return
        if not re.fullmatch(r"[A-Za-z0-9_.-]{20,4096}", credential):
            raise ToolError("Проверь формат API-ключа или Read Access Token TMDB.")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        fd, name = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(fd, "w") as out:
                json.dump({"credential": credential}, out)
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)


class TMDB:
    CACHE_LIMIT = 160
    CACHE_TTL = 900

    def __init__(self, credentials, *, transport=None, request_interval=0.25, clock=time.monotonic):
        self.credentials, self.transport = credentials, transport
        self.genre_cache = {}
        self.cache = OrderedDict()
        self.inflight = {}
        self.rate_lock = asyncio.Lock()
        self.request_slots = asyncio.Semaphore(3)
        self.request_interval, self.clock = request_interval, clock
        self.next_request = self.blocked_until = 0

    def clear_cache(self):
        self.cache.clear()
        self.genre_cache.clear()

    async def close(self):
        tasks = list(self.inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def get(self, path, **params):
        credential = await asyncio.to_thread(self.credentials.read)
        # Partition the cache by credential without retaining it in cache keys.
        key = (hashlib.sha256(credential.encode()).digest(), path, tuple(sorted(params.items())))
        cached = self.cache.get(key)
        if cached and cached[0] > self.clock():
            self.cache.move_to_end(key)
            return cached[1]
        if key not in self.inflight:
            self.inflight[key] = asyncio.create_task(
                self._cached_request(key, credential, path, params)
            )
        return await asyncio.shield(self.inflight[key])

    async def _cached_request(self, key, credential, path, params):
        try:
            data = await self._fetch(credential, path, params)
            self.cache[key] = (self.clock() + self.CACHE_TTL, data)
            self.cache.move_to_end(key)
            while len(self.cache) > self.CACHE_LIMIT:
                self.cache.popitem(last=False)
            return data
        finally:
            self.inflight.pop(key, None)

    async def _fetch(self, credential, path, params):
        headers = {"Accept": "application/json"}
        params = dict(params)
        if re.fullmatch(r"[a-fA-F0-9]{32}", credential):
            params["api_key"] = credential
        else:
            headers["Authorization"] = f"Bearer {credential}"
        try:
            async with self.request_slots:
                async with self.rate_lock:
                    if self.blocked_until > self.clock():
                        raise TMDBRateLimited(math.ceil(self.blocked_until - self.clock()))
                    await asyncio.sleep(max(0, self.next_request - self.clock()))
                    if self.blocked_until > self.clock():
                        raise TMDBRateLimited(math.ceil(self.blocked_until - self.clock()))
                    self.next_request = self.clock() + self.request_interval
                async with httpx.AsyncClient(
                    base_url="https://api.themoviedb.org/3/",
                    timeout=8,
                    transport=self.transport,
                    follow_redirects=False,
                ) as client:
                    response = await client.get(
                        path, params={"language": "ru-RU", **params}, headers=headers
                    )
            if response.status_code in {401, 403}:
                raise ToolError("TMDB отклонил ключ. Проверь настройки интеграции.")
            if response.status_code == 429:
                delay = retry_delay(response.headers.get("Retry-After", "5"))
                self.blocked_until = max(self.blocked_until, self.clock() + delay)
                raise TMDBRateLimited(delay)
            response.raise_for_status()
            data = response.json()
            expected = (
                list if path in {"configuration/countries", "configuration/languages"} else dict
            )
            if not isinstance(data, expected) or len(response.content) > 2_000_000:
                raise ValueError()
            return data
        except (httpx.HTTPError, ValueError, TypeError):
            raise ToolError(
                "TMDB временно недоступен. Личная медиатека сохранена локально."
            ) from None

    async def genres(self, kind):
        if kind not in self.genre_cache:
            data = await self.get(f"genre/{kind}/list")
            self.genre_cache[kind] = {g["id"]: g["name"] for g in data.get("genres", [])}
        return self.genre_cache[kind]

    def metadata(self, row, kind, genres=None):
        date = row.get("release_date" if kind == "movie" else "first_air_date") or ""
        poster = row.get("poster_path") or ""
        return {
            "title": row.get("title" if kind == "movie" else "name")
            or row.get("original_title" if kind == "movie" else "original_name")
            or "Без названия",
            "kind": "movie" if kind == "movie" else "series",
            "year": int(date[:4]) if date[:4].isdigit() else 0,
            "catalogue_key": f"tmdb:{kind}:{int(row['id'])}",
            "poster_url": f"https://image.tmdb.org/t/p/w500{poster}"
            if re.fullmatch(r"/[\w.]+", poster)
            else "",
            "description": (row.get("overview") or "")[:3000],
            "metadata": {
                "genres": [g["name"] for g in row.get("genres", [])]
                or [genres[g] for g in row.get("genre_ids", []) if genres and g in genres],
                "runtime": row.get("runtime") or next(iter(row.get("episode_run_time") or []), 0),
                "rating": row.get("vote_average") or 0,
                "seasons": [
                    {
                        "season": s["season_number"],
                        "episodes": s.get("episode_count", 0),
                        "name": s.get("name", ""),
                    }
                    for s in row.get("seasons", [])
                    if s.get("season_number", 0) > 0
                ],
                "episode_count": row.get("number_of_episodes") or 0,
                "original_title": row.get("original_title" if kind == "movie" else "original_name")
                or "",
                "original_language": row.get("original_language") or "",
                "countries": row.get("origin_country")
                or [c["iso_3166_1"] for c in row.get("production_countries", [])],
                "release_date": date,
                "popularity": row.get("popularity") or 0,
            },
        }

    async def search(self, query="", *, kind="all", genre="", year=0, rating=0, page=1):
        # Compatibility for deterministic voice commands and existing catalogue adapters.
        from backend.cinema.catalogue import Catalogue
        from backend.cinema.models import CatalogueQuery

        result = await Catalogue(self).page(
            CatalogueQuery(
                query=query,
                kind=kind,
                genre=genre,
                year=year,
                rating=rating,
                page=page,
                sort="rating",
            )
        )
        return result["items"]

    @staticmethod
    def identity(key):
        match = re.fullmatch(r"tmdb:(movie|tv):([1-9]\d{0,9})", str(key))
        if not match:
            raise ToolError("Неверный идентификатор TMDB.")
        return match[1], int(match[2])

    async def detail(self, key):
        kind, identifier = self.identity(key)
        row = await self.get(f"{kind}/{identifier}")
        if not row.get("overview"):
            fallback = await self.get(f"{kind}/{identifier}", language="en-US")
            row = {**row, "overview": fallback.get("overview") or ""}
        return self.metadata(row, kind)

    async def episodes(self, key, season):
        kind, identifier = self.identity(key)
        if kind != "tv" or type(season) is not int or not 1 <= season <= 999:
            raise ToolError("Выбери сезон сериала.")
        data = await self.get(f"tv/{identifier}/season/{season}")
        return [
            {"season": season, "episode": ep["episode_number"], "name": ep.get("name", "")}
            for ep in data.get("episodes", [])[:1000]
            if ep.get("episode_number", 0) > 0
        ]

    async def providers(self, key, region):
        kind, identifier = self.identity(key)
        data = await self.get(f"{kind}/{identifier}/watch/providers")
        country = data.get("results", {}).get(region, {})
        return {
            "region": region,
            "url": country.get("link", ""),
            "providers": [
                {"name": provider["provider_name"], "type": mode}
                for mode in ("flatrate", "free", "ads", "rent", "buy")
                for provider in country.get(mode, [])
            ],
            "attribution": "JustWatch",
            "playback_confirmed": False,
        }
