"""Official API contract through httpx MockTransport, without credentials or network."""

import asyncio
import json
import stat

import httpx
import pytest

from backend.cinema.tmdb import TMDB, TMDBCredentials
from backend.tools.registry import ToolError


def client(tmp_path, handler):
    credentials = TMDBCredentials(tmp_path / "integrations")
    credentials.save("a" * 32)
    return TMDB(credentials, transport=httpx.MockTransport(handler))


def test_tmdb_search_filters_details_seasons_and_official_providers(tmp_path):
    calls = []

    def response(request):
        calls.append(request)
        assert request.url.host == "api.themoviedb.org"
        assert request.url.params["api_key"] == "a" * 32
        path = request.url.path
        if "/genre/" in path:
            data = {"genres": [{"id": 53, "name": "триллер"}]}
        elif "/search/movie" in path or "/discover/movie" in path:
            data = {
                "results": [
                    {
                        "id": 1,
                        "title": "Триллер",
                        "release_date": "2025-01-01",
                        "genre_ids": [53],
                        "vote_average": 8,
                        "poster_path": "/poster.jpg",
                    },
                    {"id": 2, "title": "Низкий рейтинг", "genre_ids": [53], "vote_average": 4},
                    {"id": 3, "title": "Другой жанр", "genre_ids": [35], "vote_average": 9},
                    {
                        "id": 4,
                        "title": "Adult",
                        "genre_ids": [53],
                        "vote_average": 8,
                        "adult": True,
                    },
                ]
            }
        elif "/season/" in path:
            data = {"episodes": [{"episode_number": 2, "name": "Вторая"}]}
        elif "/watch/providers" in path:
            data = {
                "results": {
                    "EE": {
                        "link": "https://www.themoviedb.org/movie/1/watch",
                        "flatrate": [{"provider_name": "Official service"}],
                    }
                }
            }
        else:
            data = {
                "id": 5,
                "name": "Сериал",
                "first_air_date": "2024-01-01",
                "genres": [{"id": 53, "name": "триллер"}],
                "episode_run_time": [42],
                "vote_average": 8.1,
                "number_of_episodes": 12,
                "seasons": [{"season_number": 1, "episode_count": 12, "name": "Сезон 1"}],
            }
        return httpx.Response(200, json=data)

    async def run():
        api = client(tmp_path, response)
        results = await api.search("Триллер", kind="movie", genre="триллер", year=2025, rating=7)
        assert len(results) == 1 and results[0]["title"] == "Триллер"
        assert results[0]["poster_url"] == "https://image.tmdb.org/t/p/w500/poster.jpg"
        assert results[0]["catalogue_key"] == "tmdb:movie:1"
        assert "url" not in results[0] and "source_id" not in results[0]
        await api.search(kind="movie", genre="триллер", rating=7)
        discover = next(r for r in calls if "/discover/" in r.url.path)
        assert discover.url.params["with_genres"] == "53"
        assert discover.url.params["vote_average.gte"] == "7.0"
        item = await api.detail("tmdb:tv:5")
        assert item["metadata"]["runtime"] == 42
        assert item["metadata"]["episode_count"] == 12
        assert item["metadata"]["seasons"][0]["episodes"] == 12
        assert await api.episodes("tmdb:tv:5", 1) == [{"season": 1, "episode": 2, "name": "Вторая"}]
        providers = await api.providers("tmdb:movie:1", "EE")
        assert providers["providers"][0]["name"] == "Official service"
        assert providers["attribution"] == "JustWatch" and not providers["playback_confirmed"]
        assert (await api.providers("tmdb:movie:1", "US"))["providers"] == []
        assert sum("/genre/" in r.url.path for r in calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_tmdb_errors_hide_credential_and_request_url(tmp_path, status):
    async def run():
        api = client(tmp_path, lambda _: httpx.Response(status, text="private response"))
        with pytest.raises(ToolError) as error:
            await api.search("test", kind="movie")
        assert "a" * 32 not in str(error.value)
        assert "api_key" not in str(error.value) and "private response" not in str(error.value)

    asyncio.run(run())


def test_integration_secret_is_owner_only_and_token_uses_header(tmp_path):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"genres": []})

    async def run():
        api = client(tmp_path, handle)
        assert stat.S_IMODE(api.credentials.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(api.credentials.directory.stat().st_mode) == 0o700
        token = "placeholder.read.access.token"
        api.credentials.save(token)
        await api.get("genre/tv/list")
        assert seen[0].headers["authorization"] == "Bearer " + token
        assert token not in str(seen[0].url)
        api.credentials.save("")
        assert not api.credentials.configured
        with pytest.raises(ToolError, match="Подключи TMDB"):
            await api.search("x")
        assert len(seen) == 1
        with pytest.raises(ToolError):
            api.credentials.save("invalid\nheader")
        with pytest.raises(ToolError):
            await api.detail("tmdb:movie:1/../private")

    asyncio.run(run())


def test_service_hud_search_add_and_secret_never_enters_snapshot(tmp_path):
    from types import SimpleNamespace

    from backend.cinema.service import Cinema
    from backend.config import Settings
    from backend.context import DeviceContext
    from backend.hud.bus import HudBus
    from backend.mac_agent import LocalMacAgent
    from backend.memory.store import MemoryStore
    from backend.tools.registry import ToolRegistry

    def handle(request):
        if "/genre/" in request.url.path:
            return httpx.Response(200, json={"genres": []})
        if "/search/" in request.url.path:
            return httpx.Response(200, json={"results": [{"id": 1, "title": "Film"}]})
        return httpx.Response(200, json={"id": 1, "title": "Film", "runtime": 100})

    async def run():
        api = client(tmp_path, handle)
        memory = MemoryStore(tmp_path / "assistant.sqlite")
        registry = ToolRegistry()
        registry.mac_agent = LocalMacAgent()
        service = Cinema(registry, Settings(data_dir=tmp_path / "data"), memory, tmdb=api)
        bus = HudBus()
        service.bind(bus, SimpleNamespace(device=DeviceContext.local_mac()))
        await service.handle(
            {"op": "search", "query": "Film", "kind": "movie", "relax_restrictions": True}
        )
        snapshot = next(v for v in bus.snapshot() if v["type"] == "cinema")
        assert snapshot["matches"][0]["title"] == "Film"
        assert "a" * 32 not in json.dumps(snapshot)
        await service.handle({"op": "catalogue_add", "key": "tmdb:movie:1"})
        item = (await service.db("library"))[0]
        assert item["status"] == "planned" and item["metadata"]["runtime"] == 100
        await service.handle({"op": "focus", "id": item["id"]})
        assert service.focused == item["id"]
        await service.close()
        memory.close()

    asyncio.run(run())


def test_httpx_info_logging_redacts_v3_key(tmp_path, caplog):
    import logging

    async def run():
        api = client(tmp_path, lambda _: httpx.Response(200, json={"genres": []}))
        with caplog.at_level(logging.INFO, logger="httpx"):
            await api.get("genre/movie/list")
        assert "api.themoviedb.org" in caplog.text
        assert "a" * 32 not in caplog.text
        assert "[redacted]" in caplog.text

    asyncio.run(run())
