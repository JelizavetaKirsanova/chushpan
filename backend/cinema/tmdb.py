"""Official TMDB metadata only. Credentials never enter snapshots, tools or URLs in logs."""

import asyncio
import json
import logging
import os
import re
import tempfile
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
    def __init__(self, credentials, *, transport=None):
        self.credentials, self.transport = credentials, transport
        self.genre_cache = {}

    async def get(self, path, **params):
        credential = await asyncio.to_thread(self.credentials.read)
        headers = {"Accept": "application/json"}
        if re.fullmatch(r"[a-fA-F0-9]{32}", credential):
            params["api_key"] = credential
        else:
            headers["Authorization"] = f"Bearer {credential}"
        try:
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
                raise ToolError("Лимит TMDB исчерпан. Повтори поиск позже.")
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (httpx.HTTPError, ValueError, TypeError):
            # Never forward httpx exceptions: request URLs may contain the API key.
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
            "title": row.get("title" if kind == "movie" else "name") or "Без названия",
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
            },
        }

    async def search(self, query="", *, kind="all", genre="", year=0, rating=0):
        if kind not in {"all", "movie", "series"}:
            raise ToolError("Выбери фильмы или сериалы.")
        if not 0 <= int(year) <= 2200 or not 0 <= float(rating) <= 10:
            raise ToolError("Проверь год и рейтинг.")
        if len(query) > 200 or len(genre) > 80:
            raise ToolError("Слишком длинный запрос.")
        kinds = ["movie", "tv"] if kind == "all" else ["movie" if kind == "movie" else "tv"]

        async def search_kind(media):
            genres = await self.genres(media)
            params = {"include_adult": "false"}
            genre_id = next(
                (key for key, name in genres.items() if name.casefold() == genre.casefold()), None
            )
            if genre and genre_id is None:
                return []
            if query.strip():
                params["query"] = query.strip()
                if year:
                    params["year" if media == "movie" else "first_air_date_year"] = int(year)
                endpoint = f"search/{media}"
            else:
                endpoint = f"discover/{media}"
                params.update({"sort_by": "popularity.desc", "vote_average.gte": float(rating)})
                if genre_id:
                    params["with_genres"] = genre_id
                if year:
                    params[
                        "primary_release_year" if media == "movie" else "first_air_date_year"
                    ] = int(year)
            data = await self.get(endpoint, **params)
            return [
                self.metadata(row, media, genres)
                for row in data.get("results", [])[:20]
                if not row.get("adult")
                and (not genre_id or genre_id in row.get("genre_ids", []))
                and (row.get("vote_average") or 0) >= float(rating)
            ]

        results = await asyncio.gather(*(search_kind(media) for media in kinds))
        return sorted(
            [row for group in results for row in group],
            key=lambda row: row["metadata"]["rating"],
            reverse=True,
        )

    @staticmethod
    def identity(key):
        match = re.fullmatch(r"tmdb:(movie|tv):([1-9]\d{0,9})", str(key))
        if not match:
            raise ToolError("Неверный идентификатор TMDB.")
        return match[1], int(match[2])

    async def detail(self, key):
        kind, identifier = self.identity(key)
        return self.metadata(await self.get(f"{kind}/{identifier}"), kind)

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
