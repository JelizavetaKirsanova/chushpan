"""Official search protocol, independent verification and durable progress guards."""

import asyncio
import json
import stat
from types import SimpleNamespace

import httpx
import pytest

from backend.cinema.internet import (
    BraveSearch,
    InternetSearch,
    PageVerifier,
    SearchCredentials,
    verified_entity,
)
from backend.cinema.models import Preferences
from backend.cinema.public_search import Page, PublicCatalogue
from backend.cinema.service import Cinema
from backend.config import Settings
from backend.context import DeviceContext
from backend.hud.bus import HudBus
from backend.mac_agent import LocalMacAgent
from backend.memory.store import MemoryStore
from backend.tools.registry import ToolError, ToolRegistry

QUERY = {
    "title": "Test Series",
    "year": 2025,
    "kind": "series",
    "season": 2,
    "episode": 3,
    "metadata": {
        "original_title": "Original Series",
        "alternative_titles": ["Alias Series"],
        "imdb_id": "tt12345678",
    },
}
URL = "https://new-source.example/season-2/episode-3"


def entity(**changes):
    return {
        "@type": "TVEpisode",
        "name": "Episode 3",
        "partOfSeries": {"@type": "TVSeries", "name": "Test Series", "datePublished": "2025"},
        "partOfSeason": {"seasonNumber": 2},
        "episodeNumber": 3,
        "associatedMedia": {"@type": "VideoObject", "contentUrl": URL + "/full.mp4"},
        **changes,
    }


@pytest.mark.parametrize(
    "change,valid",
    [
        ({}, True),
        ({"episodeNumber": 4}, False),
        ({"partOfSeason": {"seasonNumber": 1}}, False),
        ({"partOfSeries": {"name": "Remake", "datePublished": "2025"}}, False),
        ({"partOfSeries": {"name": "Test Series", "datePublished": "2024"}}, False),
        ({"partOfSeries": {"name": "Alias Series", "datePublished": "2025"}}, True),
        (
            {
                "partOfSeries": {
                    "name": "Test Series",
                    "sameAs": "https://www.imdb.com/title/tt12345678/",
                }
            },
            True,
        ),
    ],
)
def test_episode_identity_needs_exact_series_year_or_imdb_and_coordinates(change, valid):
    result = verified_entity(entity(**change), QUERY)
    assert bool(result) == valid
    if result:
        assert result["scope"] == "episode" and result["episode_match"]


def test_free_and_region_are_independent_evidence_not_search_snippets():
    e = entity(isAccessibleForFree=True, regionsAllowed="RU, EE")
    r = verified_entity(e, {**QUERY, "region": "EE"})
    assert r["access_status"] == "UNKNOWN" and r["region_access"] == "allowed"
    e["license"] = "https://creativecommons.org/licenses/by/4.0/"
    r = verified_entity(e, {**QUERY, "region": "DE"})
    assert r["access_status"] == "FREE" and r["cost_verified"] and r["region_access"] == "blocked"


def test_search_keys_are_private_never_exported_and_can_be_deleted(tmp_path):
    credentials = SearchCredentials(tmp_path / "integrations")
    credentials.save("BSA_fixture_search_key")
    assert credentials.read() == "BSA_fixture_search_key"
    assert stat.S_IMODE(credentials.path.stat().st_mode) == 0o600
    assert not list(credentials.path.parent.glob("*.new"))
    credentials.save("")
    assert not credentials.configured
    with pytest.raises(ToolError):
        credentials.save("bad\nkey")


def test_brave_key_is_header_only_redirects_not_followed_and_ee_not_fabricated(tmp_path):
    async def run():
        key = SearchCredentials(tmp_path)
        key.save("BSA_fixture_search_key")
        requests = []

        def transport(request):
            requests.append(request)
            assert request.headers["x-subscription-token"] == key.read()
            assert key.read() not in str(request.url)
            assert request.url.params["country"] == "ALL"
            return httpx.Response(302, headers={"Location": "https://other.example/"})

        provider = BraveSearch(key, transport=httpx.MockTransport(transport))
        with pytest.raises(ToolError, match="302"):
            await provider.search("Test", "EE", "ru")
        assert len(requests) == 1

    asyncio.run(run())


class HTTP:
    def __init__(self, *, robots="", metadata=None):
        self.robots, self.metadata = robots, metadata or entity()
        self.calls = []

    async def request(self, url, **kwargs):
        self.calls.append(url)
        if url.endswith("robots.txt"):
            return httpx.Response(200, text=self.robots)
        return httpx.Response(
            200,
            text='<script type="application/ld+json">' + json.dumps(self.metadata) + "</script>",
            headers={"content-type": "text/html"},
        )


def test_verifier_enforces_robots_and_rejects_homepage_and_non_matching_html():
    async def run():
        http = HTTP(robots="User-agent: *\nDisallow: /season-2")
        with pytest.raises(ToolError, match="robots"):
            await PageVerifier(http).verify(URL, QUERY)
        assert http.calls == ["https://new-source.example/robots.txt"]
        with pytest.raises(ToolError):
            await PageVerifier(HTTP()).verify("https://new-source.example/", QUERY)
        with pytest.raises(ToolError, match="метаданные|Метаданные"):
            await PageVerifier(HTTP(metadata={"@type": "WebPage", "name": "Test Series"})).verify(
                URL, QUERY
            )
        assert (await PageVerifier(HTTP()).verify(URL, QUERY))["scope"] == "episode"

    asyncio.run(run())


def test_queries_include_tmdb_names_year_coordinates_and_language():
    queries = InternetSearch.queries(QUERY, Preferences())
    assert len(queries) == 3
    assert all("2025" in q and "S02E03" in q and "русская" in q for q in queries)
    assert (
        "Test Series" in queries[0]
        and "Original Series" in queries[1]
        and "Alias Series" in queries[2]
    )


def test_missing_search_credentials_is_explicit_and_does_not_fetch(tmp_path):
    async def run():
        http = HTTP()
        search = InternetSearch(SearchCredentials(tmp_path), http)
        rows = await search.search(QUERY, Preferences(), [])
        assert "не подключён" in rows[0]["reason"] and not http.calls
        assert rows[0]["configurable"] is False and not rows[0]["allowed"]

    asyncio.run(run())


def test_kion_only_uses_published_episode_link_with_matching_episode_metadata():
    async def run():
        url = "https://kion.ru/serial/test/sezon-2/seriya-3"
        c = PublicCatalogue(
            {"base_url": "https://kion.ru"},
            HTTP(metadata={"@type": "TVEpisode", "name": "Серия 3"}),
        )
        from backend.cinema.public_search import Robots

        c.rules = Robots("")
        page = Page(f'<a href="{url}">Серия 3</a>')
        r = await c.kion_episode(page, "https://kion.ru/serial/test", QUERY)
        assert r["url"] == url and r["scope"] == "episode"
        assert (
            await c.kion_episode(
                Page('<a href="/serial/other/sezon-2/seriya-3">wrong</a>'),
                "https://kion.ru/serial/test",
                QUERY,
            )
            is None
        )

    asyncio.run(run())


class Player:
    def __init__(self):
        self.opened = []

    async def open(self, target):
        self.opened.append(target)
        return {
            "mode": "html5",
            "playback_confirmed": True,
            "position": target["episode"]["position"] + 0.2,
            "duration": 1000,
            "paused": False,
        }

    async def sample(self):
        raise ToolError("closed")

    async def detach(self):
        pass


def test_zero_errors_unconfirmed_play_and_false_ended_do_not_destroy_progress(tmp_path):
    async def run():
        memory = MemoryStore(tmp_path / "assistant.sqlite")
        tools = ToolRegistry()
        tools.mac_agent = LocalMacAgent()
        service = Cinema(
            tools, Settings(data_dir=tmp_path / "data"), memory, adapter_factory=lambda _: Player()
        )
        service.bind(HudBus(), SimpleNamespace(device=DeviceContext.local_mac()))
        try:
            source = await service.db(
                "save_source",
                {"name": "Test", "base_url": "http://localhost:8765", "adapter": "html5"},
            )
            item = await service.db(
                "add",
                {
                    "title": "Series",
                    "source_id": source["id"],
                    "url": "http://localhost:8765/one.html",
                },
            )
            ep = item["current"]
            link = ep["links"][0]
            await service.db("progress", ep["id"], 100, 1000, "manual", link["id"])
            result = await service.resume(item["id"])
            assert result["playback_confirmed"]
            before = (await service.db("detail", item["id"]))["current"]["position"]
            assert 97 <= before < 98
            assert not await service.save({"position": 0, "duration": 0, "paused": True}, "close")
            assert not await service.save(
                {"position": 0, "duration": 1000, "paused": True}, "periodic"
            )
            assert not await service.save(
                {"position": 10, "duration": 1000, "paused": False, "ended": False}, "ended"
            )
            saved = await service.db("detail", item["id"])
            assert saved["current"]["position"] == before and not saved["current"]["watched"]
            assert saved["current"]["playback_state"]["confirmed"]
            assert any(r["status"] == "ignored" for r in await service.db("diagnostics"))
        finally:
            await service.close()
            memory.close()

    asyncio.run(run())


def test_web_discovery_new_domain_consent_then_one_click_saved_episode(tmp_path):
    async def run():
        memory = MemoryStore(tmp_path / "assistant.sqlite")
        tools = ToolRegistry()
        tools.mac_agent = LocalMacAgent()
        player = Player()
        service = Cinema(
            tools, Settings(data_dir=tmp_path / "data"), memory, adapter_factory=lambda _: player
        )
        service.bind(HudBus(), SimpleNamespace(device=DeviceContext.local_mac()))

        class Provider:
            def __init__(self):
                self.queries = []

            async def search(self, q, region, language):
                self.queries.append(q)
                return [URL]

        provider = Provider()
        try:
            service.search_credentials.save("BSA_fixture_search_key")
            service.sources.http = HTTP()
            service.sources.web.providers["brave"].provider = provider
            await service.db("configure", {"search_provider": "brave"})
            await service.db(
                "save_source", {"name": "KION", "base_url": "https://kion.ru", "enabled": False}
            )
            item = await service.db(
                "add",
                {
                    "title": QUERY["title"],
                    "year": 2025,
                    "kind": "series",
                    "season": 2,
                    "episode": 3,
                    "metadata": QUERY["metadata"],
                },
            )
            await service.resume(item["id"])
            assert not player.opened and service.watch_confirmations
            token = next(iter(service.watch_confirmations))
            await service.handle({"op": "watch_confirm", "token": token})
            assert len(player.opened) == 1 and player.opened[0]["link"]["url"] == URL
            assert len(provider.queries) == 3
            saved = await service.db("detail", item["id"])
            assert saved["current"]["playback_state"]["confirmed"]
            await service.resume(item["id"])
            assert len(player.opened) == 2 and len(provider.queries) == 3
            document = await service.db("export_library")
            assert "BSA_fixture_search_key" not in json.dumps(document)
        finally:
            await service.close()
            memory.close()

    asyncio.run(run())


def test_previously_working_safari_source_is_tried_before_switching_on_anonymous_403(tmp_path):
    from backend.cinema.sources import SourceManager
    from backend.cinema.store import CinemaStore

    async def run():
        store = CinemaStore(tmp_path / "assistant.sqlite")
        first = store.save_source(
            {"name": "Last played", "base_url": "https://last.example", "adapter": "html5"}
        )
        other = store.save_source(
            {"name": "Other", "base_url": "https://other.example", "preferred": True}
        )
        item = store.add(
            {
                "title": "Film",
                "kind": "movie",
                "year": 2025,
                "source_id": first["id"],
                "url": "https://last.example/movie",
            }
        )
        item = store.add(
            {
                "title_id": item["id"],
                "title": "Film",
                "kind": "movie",
                "year": 2025,
                "source_id": other["id"],
                "url": "https://other.example/movie",
            }
        )
        ep = item["current"]
        link = next(candidate for candidate in ep["links"] if candidate["source_id"] == first["id"])
        store.playback_state(ep["id"], link["id"], 100, 1000, True)

        async def db(name, *args, **kwargs):
            return getattr(store, name)(*args, **kwargs)

        class CheckHTTP:
            status = 403

            async def request(self, url, **kwargs):
                if "last.example" in url:
                    return httpx.Response(self.status)
                if url.endswith("robots.txt"):
                    return httpx.Response(200, text="User-agent: *\nAllow: /")
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    text='<script type="application/ld+json">'
                    + json.dumps(
                        {
                            "@type": "Movie",
                            "name": "Film",
                            "datePublished": "2025",
                            "associatedMedia": {"contentUrl": url + "/full.mp4"},
                        }
                    )
                    + "</script>",
                )

        http = CheckHTTP()
        manager = SourceManager(db, http=http)
        result = await manager.search(item["id"], saved_only=True)
        assert manager.result(result["selected_id"])["source_id"] == other["id"]
        assert not next(r for r in result["results"] if r["source_id"] == first["id"])[
            "content_verified"
        ]
        http.status = 404
        result = await manager.search(item["id"], saved_only=True, refresh=True)
        assert manager.result(result["selected_id"])["source_id"] == other["id"]
        store.close()

    asyncio.run(run())
