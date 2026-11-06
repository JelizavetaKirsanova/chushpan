"""Public source APIs and media events are mocked. No real browser or device use."""

import asyncio
import gzip
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.cinema.commands import command
from backend.cinema.models import Source
from backend.cinema.sources import PublicHTTP, SourceManager, capabilities
from backend.cinema.store import CinemaStore
from backend.tools.registry import ToolError


class HTTP:
    def __init__(self, statuses=None, *, full_pages=False):
        self.statuses = statuses or {}
        self.full_pages = full_pages
        self.calls = []

    async def request(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "advancedsearch.php" in url:
            return httpx.Response(
                200,
                json={
                    "response": {
                        "docs": [
                            {"identifier": "right", "title": "Film", "year": 2024},
                            {"identifier": "remake", "title": "Film", "year": 2025},
                            {"identifier": "trailer", "title": "Film Trailer", "year": 2024},
                            {"identifier": "restricted", "title": "Film", "year": 2024},
                        ]
                    }
                },
            )
        if "/metadata/" in url:
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "title": "Film",
                        "year": 2024,
                        "mediatype": "movies",
                        "language": "en",
                        "access-restricted-item": url.endswith("restricted"),
                    }
                },
            )
        return httpx.Response(
            self.statuses.get(url, 200),
            headers={
                "content-type": "video/mp4"
                if url.endswith(".mp4") and not self.full_pages
                else "text/html"
            },
            text='<script type="application/ld+json">'
            + json.dumps(
                {
                    "@type": "Movie",
                    "name": "Film",
                    "datePublished": "2024",
                    "associatedMedia": {"contentUrl": url + "/full.mp4"},
                }
            )
            + "</script>"
            if self.full_pages
            else "",
        )


def manager(store, http=None, **kwargs):
    async def db(name, *args, **values):
        return getattr(store, name)(*args, **values)

    return SourceManager(db, http=http or HTTP(), **kwargs)


def linked(store, name, base, title_id=None, **values):
    source = store.save_source({"name": name, "base_url": base, **values})
    item = store.add(
        {
            "title_id": title_id,
            "title": "Film",
            "kind": "movie",
            "year": 2024,
            "source_id": source["id"],
            "url": base + "/film.mp4",
        }
    )
    return source, item


def test_source_priority_pin_last_page_progress_and_deletion_survive_restart(tmp_path):
    path = tmp_path / "cinema.sqlite"
    store = CinemaStore(path)
    source, item = linked(store, "First", "https://first.example")
    second, item = linked(store, "Second", "https://second.example", item["id"], preferred=True)
    ep = item["current"]
    link = next(link for link in ep["links"] if link["source_id"] == source["id"])
    store.progress(ep["id"], 321, 1000, "manual", link["id"])
    store.remember_source(item["id"], source["id"], ep["id"], link["id"], pin=True)
    store.source_health(source["id"], "available", playback=True)
    store.reorder_sources([second["id"], source["id"]])
    store.save_source({**store.source(source["id"]).model_dump(), "name": "Renamed"})
    store.close()
    store = CinemaStore(path)
    assert store.sources()[0]["id"] == second["id"]
    memory = store.detail(item["id"])["source_memory"]
    assert memory["pinned_source_id"] == source["id"] and memory["page_url"].endswith("film.mp4")
    assert (
        store.sources()[1]["health"]["successes"] == 1
    )  # Updating a source must not delete its health.
    store.unpin_source(item["id"])
    assert store.detail(item["id"])["source_memory"]["last_source_id"] == source["id"]
    store.delete_source(source["id"])
    assert store.detail(item["id"])["current"]["position"] == 321
    assert len(store.history()) == 1 and store.history()[0]["link_id"] is None
    assert store.snapshot()["history"][0]["source_name"] == "Renamed"
    assert len(store.library()) == 1
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    store.close()


def test_manual_fallback_is_never_claimed_as_verified_video_and_disabled_not_contacted(tmp_path):
    async def run():
        store = CinemaStore(tmp_path / "cinema.sqlite")
        source = store.save_source(
            {
                "name": "Site",
                "base_url": "https://site.example",
                "search_url": "https://site.example/?q={query}",
            }
        )
        store.save_source({"name": "Disabled", "base_url": "https://off.example", "enabled": False})
        item = store.add({"title": "Film", "kind": "movie", "year": 2024})
        http = HTTP()
        result = await manager(store, http).search(item["id"])
        assert result["selected_id"] is None
        row = result["results"][0]
        assert row["source_id"] == source["id"] and row["kind"] == "search"
        assert not row["playback_confirmed"] and not row["embedded"] and row["match"] == 0
        assert "Film%202024" in row["url"] and not http.calls
        store.close()

    asyncio.run(run())


def test_official_api_rejects_remakes_trailers_restricted_items_and_series(tmp_path):
    async def run():
        store = CinemaStore(tmp_path / "cinema.sqlite")
        store.save_source(
            {"name": "Archive", "base_url": "https://archive.org", "search_mode": "archive"}
        )
        item = store.add({"title": "Film", "kind": "movie", "year": 2024})
        http = HTTP()
        search = manager(store, http)
        result = await search.search(item["id"])
        assert len(result["results"]) == 1 and result["selected_id"] is None
        assert not result["results"][0]["content_verified"]
        row = result["results"][0]
        assert row["url"] == "https://archive.org/details/right"
        assert row["match"] == 1 and row["kind"] == "page" and not row["embedded"]
        assert not row["playback_confirmed"] and row["access_type"] == "unknown"
        assert not any(
            "metadata/remake" in url or "metadata/trailer" in url for url, _ in http.calls
        )
        series = store.add({"title": "Film", "kind": "series", "year": 2024})
        assert (await search.search(series["id"]))["selected_id"] is None
        store.close()

    asyncio.run(run())


def test_selection_prefers_pinned_and_last_but_falls_back_when_unavailable(tmp_path):
    async def run():
        store = CinemaStore(tmp_path / "cinema.sqlite")
        first, item = linked(store, "First", "https://first.example", priority=0, preferred=True)
        last, item = linked(store, "Last", "https://last.example", item["id"], priority=1)
        for source in (first, last):
            store.source_health(source["id"], "available", playback=True)
        ep = item["current"]
        link = next(link for link in ep["links"] if link["source_id"] == last["id"])
        store.remember_source(item["id"], last["id"], ep["id"], link["id"], pin=True)
        http = HTTP(full_pages=True)
        search = manager(store, http)
        result = await search.search(item["id"])
        assert search.result(result["selected_id"])["source_id"] == last["id"]
        http.statuses[link["url"]] = 404
        result = await search.search(item["id"], refresh=True)
        assert search.result(result["selected_id"])["source_id"] == first["id"]
        result = await search.search(item["id"], alternative=True, refresh=True)
        assert all(row["source_id"] != last["id"] for row in result["results"])
        store.configure({"auto_select_source": False})
        assert (await search.search(item["id"], refresh=True))["selected_id"] is None
        store.close()

    asyncio.run(run())


def test_unplayed_source_needs_an_explicit_first_choice_even_after_http_success(tmp_path):
    async def run():
        store = CinemaStore(tmp_path / "cinema.sqlite")
        try:
            source = store.save_source(
                {"name": "Site", "base_url": "https://site.example", "preferred": True}
            )
            item = store.add({"title": "Film", "kind": "movie", "year": 2024})
            search = manager(store, HTTP(full_pages=True))

            def install_search():
                search.adapter(store.source(source["id"]).model_dump()).search = AsyncMock(
                    return_value=[
                        {
                            "url": "https://site.example/film",
                            "kind": "page",
                            "match": 1,
                            "availability": "available",
                            "embedded": False,
                            "content_verified": True,
                            "video_type": "full_movie",
                            "reason": "Fixture provider record",
                        }
                    ]
                )

            install_search()
            first = await search.search(item["id"])
            assert first["results"][0]["content_verified"] and first["selected_id"] is None
            store.source_health(source["id"], "available")
            install_search()
            assert (await search.search(item["id"], refresh=True))["selected_id"] is None
            store.source_health(source["id"], "available", playback=True)
            install_search()
            assert (await search.search(item["id"], refresh=True))["selected_id"] is not None
            store.save_source({**source, "adapter": "html5"})
            assert (await search.search(item["id"], refresh=True))["selected_id"] is None
        finally:
            store.close()

    asyncio.run(run())


def test_cache_coalesces_expires_and_free_filter_requires_known_access(tmp_path):
    async def run():
        store = CinemaStore(tmp_path / "cinema.sqlite")
        _, item = linked(store, "Site", "https://site.example")
        http = HTTP()
        tick = [1]
        search = manager(store, http, clock=lambda: tick[0])
        a, b = await asyncio.gather(search.search(item["id"]), search.search(item["id"]))
        assert a == b and len(http.calls) == 1
        assert await search.search(item["id"]) == a and len(http.calls) == 1
        tick[0] += 301
        with pytest.raises(ToolError, match="устарел"):
            search.result(a["selected_id"])
        store.configure({"free_only": True})
        filtered = await search.search(item["id"])
        assert filtered["selected_id"] is None and not filtered["results"][0]["allowed"]
        assert len(http.calls) == 2
        store.close()

    asyncio.run(run())


def test_inflight_search_cannot_restore_results_from_old_source_settings(tmp_path):
    async def run():
        store = CinemaStore(tmp_path / "cinema.sqlite")
        _, item = linked(store, "Site", "https://site.example")
        started, release = asyncio.Event(), asyncio.Event()

        class SlowHTTP(HTTP):
            async def request(self, url, **kwargs):
                started.set()
                await release.wait()
                return await super().request(url, **kwargs)

        search = manager(store, SlowHTTP())
        pending = asyncio.create_task(search.search(item["id"]))
        await started.wait()
        search.invalidate()
        release.set()
        with pytest.raises(ToolError, match="изменились"):
            await pending
        assert not search.cache and not search.results
        store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "resolved", [["127.0.0.1"], ["::1"], ["10.0.0.2"], ["1.1.1.1", "192.168.0.1"]]
)
def test_http_blocks_private_dns_and_mixed_addresses(resolved):
    async def run():
        async def resolve(host, port):
            return resolved

        def handle(request):
            raise AssertionError("Private addresses must never be requested")

        http = PublicHTTP(transport=httpx.MockTransport(handle), resolver=resolve)
        with pytest.raises(ToolError, match="частных"):
            await http.request("https://site.example")

    asyncio.run(run())


def test_http_pins_public_ip_preserves_tls_hostname_and_rechecks_redirects():
    async def run():
        calls = []

        async def resolve(host, port):
            return ["127.0.0.1"] if host == "local.example" else ["1.1.1.1"]

        def handle(request):
            calls.append(request)
            assert request.url.host == "1.1.1.1"
            assert request.headers["host"] == "site.example"
            assert request.extensions["sni_hostname"] == "site.example"
            return httpx.Response(302, headers={"location": "https://local.example/private"})

        http = PublicHTTP(transport=httpx.MockTransport(handle), resolver=resolve)
        with pytest.raises(ToolError):
            await http.request("https://site.example")
        assert len(calls) == 1

    asyncio.run(run())


def test_public_http_decodes_compressed_metadata_only_once():
    async def run():
        async def resolve(host, port):
            return ["1.1.1.1"]

        def handle(request):
            return httpx.Response(
                200,
                content=gzip.compress(b'{"metadata":{"title":"Film"}}'),
                headers={"content-encoding": "gzip", "content-type": "application/json"},
            )

        http = PublicHTTP(transport=httpx.MockTransport(handle), resolver=resolve)
        response = await http.request("https://site.example/metadata/film")
        assert response.json()["metadata"]["title"] == "Film"
        assert "content-encoding" not in response.headers

    asyncio.run(run())


def test_direct_embedding_requires_declared_permission_and_video_response(tmp_path):
    async def run():
        with pytest.raises(ValueError):
            Source(name="Direct", base_url="https://site.example", adapter="direct")
        store = CinemaStore(tmp_path / "cinema.sqlite")
        source, item = linked(
            store, "Direct", "https://site.example", adapter="direct", embed_allowed=True
        )
        search = manager(store)
        rows = (await search.search(item["id"]))["results"]
        assert rows[0]["embedded"] and capabilities(source)["embedded"]
        assert not capabilities({**source, "adapter": "manual"})["embedded"]
        assert not rows[0]["playback_confirmed"]
        store.close()

    asyncio.run(run())


def test_ready_shelf_requires_playable_content_not_just_a_healthy_website(tmp_path):
    store = CinemaStore(tmp_path / "cinema.sqlite")
    source, item = linked(store, "Site", "https://site.example")
    store.source_health(source["id"], "available")
    assert store.ready_keys() == []
    link = item["current"]["links"][0]
    store.link_check(link["id"], "available", False)
    assert store.ready_keys() == []
    store.link_check(link["id"], "available", True)
    assert store.ready_keys() == [f"local:{item['id']}"]
    store.close()


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("найди где посмотреть Интерстеллар", "find_sources"),
        ("включи мой сериал", "continue"),
        ("продолжи просмотр", "continue"),
        ("включи вторую серию третьего сезона", "episode"),
        ("найди другой сайт для этого сериала", "alternative"),
        ("посоветуй мне хороший триллер", "recommend"),
        ("найди что-нибудь похожее на мои любимые фильмы", "recommend"),
        ("покажи мой кинопрофиль", "profile"),
        ("что мне сегодня посмотреть", "recommend"),
    ],
)
def test_new_voice_forms_are_deterministic(text, action):
    assert command(text)["action"] == action
    assert command("не " + text) is None
    if action == "episode":
        assert command(text)["season"] == 3 and command(text)["episode"] == 2


def test_embedded_open_waits_for_real_progress_and_validates_session_events(tmp_path):
    from backend.cinema.service import Cinema
    from backend.config import Settings
    from backend.context import DeviceContext
    from backend.hud.bus import HudBus
    from backend.mac_agent import LocalMacAgent
    from backend.memory.store import MemoryStore
    from backend.tools.registry import ToolRegistry

    async def run():
        memory = MemoryStore(tmp_path / "assistant.sqlite")
        tools = ToolRegistry()
        tools.mac_agent = LocalMacAgent()

        async def no_browser(url):
            raise AssertionError("Browser opening is disabled")

        service = Cinema(tools, Settings(data_dir=tmp_path / "data"), memory, opener=no_browser)
        service.sources.http = HTTP()
        service.bind(HudBus(), SimpleNamespace(device=DeviceContext.local_mac()))
        source = await service.db(
            "save_source",
            {
                "name": "Direct",
                "base_url": "https://site.example",
                "adapter": "direct",
                "embed_allowed": True,
            },
        )
        item = await service.db(
            "add",
            {
                "title": "Film",
                "kind": "movie",
                "source_id": source["id"],
                "url": "https://site.example/film.mp4",
            },
        )
        await service.db(
            "configure",
            {"allow_browser": False, "open_in_safari": False, "resume_rewind_seconds": 0},
        )
        await service.db(
            "verify_link",
            item["current"]["links"][0]["id"],
            {"video_type": "full_movie", "content_verified": True},
        )
        result = await service.open(item["id"])
        assert not result["playback_confirmed"] and service.session["mode"] == "embedded"
        token = service.session["token"]
        base = {"op": "embedded_event", "token": token, "duration": 100, "paused": False}
        service.session["expected_duration"] = 100
        await service.embedded_event(
            {**base, "duration": 30, "sequence": 1, "event": "playing", "position": 5}
        )
        assert service.playback.code == "duration_mismatch"
        assert not service.session.get("counted")
        assert (await service.db("detail", item["id"]))["current"]["position"] == 0
        from dataclasses import replace

        from backend.security import ActionDenied

        original_device = service.conv.device
        service.conv.device = replace(original_device, capabilities=frozenset())
        with pytest.raises(ActionDenied):
            await service.embedded_event({**base, "sequence": 1, "event": "playing", "position": 1})
        service.conv.device = original_device
        await service.embedded_event({**base, "sequence": 1, "event": "playing", "position": 0})
        assert not service.session.get("counted")
        await service.embedded_event({**base, "sequence": 2, "event": "playing", "position": 5})
        assert service.session["counted"]
        assert (await service.db("detail", item["id"]))["current"]["position"] == 5
        await service.embedded_event(
            {**base, "token": "wrong", "sequence": 3, "event": "pause", "position": 30}
        )
        assert (await service.db("detail", item["id"]))["current"]["position"] == 5
        pause = asyncio.create_task(service.execute("поставь фильм на паузу"))
        for _ in range(100):
            if service.session.get("control"):
                break
            await asyncio.sleep(0)
        command_id = service.session["control"]["id"]
        await service.embedded_event(
            {
                **base,
                "sequence": 3,
                "event": "pause",
                "position": 8,
                "paused": True,
                "command_id": command_id,
            }
        )
        assert (await pause)["result_status"] == "success"
        assert "на паузе" in service.message
        await service.embedded_event({**base, "sequence": 3, "event": "pause", "position": 50})
        assert (await service.db("detail", item["id"]))["current"]["position"] == 8
        await service.close()
        memory.close()
        reopened = CinemaStore(tmp_path / "assistant.sqlite")
        assert reopened.detail(item["id"])["current"]["position"] == 8
        reopened.close()

    asyncio.run(run())


def test_hls_mime_is_case_insensitive_and_protected_manifests_are_rejected():
    from backend.cinema.sources import WebsiteAdapter

    class HLS:
        def __init__(self, status=200, protected=False):
            self.status, self.protected = status, protected

        async def request(self, url, **kwargs):
            return httpx.Response(
                self.status,
                headers={"content-type": "application/x-mpegURL"},
                text="#EXTM3U\n"
                + (
                    '#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.apple.streamingkeydelivery"\n'
                    if self.protected
                    else "#EXTINF:10\nsegment.ts\n"
                ),
            )

    async def run():
        source = {"adapter": "direct", "base_url": "https://video.example", "embed_allowed": True}
        for status, protected, expected in [
            (200, False, True),
            (200, True, False),
            (451, False, False),
            (401, False, False),
        ]:
            row = await WebsiteAdapter(source, HLS(status, protected)).resolve(
                {"url": "https://video.example/full.m3u8"}
            )
            assert row["embedded"] is expected
            if status == 451:
                assert row["region_access"] == "blocked" and row["error_code"] == "region_blocked"
            if protected:
                assert row["error_code"] == "protected_player"

    asyncio.run(run())


def test_region_metadata_is_preserved_and_ee_is_passed_to_verifier():
    from backend.cinema.sources import WebsiteAdapter

    class Page:
        async def request(self, url, **kwargs):
            if url.endswith("robots.txt"):
                return httpx.Response(200, text="User-agent: *\nAllow: /")
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text='<script type="application/ld+json">'
                + json.dumps(
                    {
                        "@type": "Movie",
                        "name": "Фильм",
                        "datePublished": "2024",
                        "regionsAllowed": ["EE"],
                        "associatedMedia": {
                            "@type": "VideoObject",
                            "contentUrl": "https://page.example/full.mp4",
                        },
                    }
                )
                + "</script>",
            )

    async def run():
        adapter = WebsiteAdapter({"adapter": "html5", "base_url": "https://page.example"}, Page())
        for region, expected in [("EE", "allowed"), ("FI", "blocked")]:
            result = await adapter.resolve(
                {
                    "url": "https://page.example/film",
                    "_expected": {
                        "title": "Фильм",
                        "kind": "movie",
                        "year": 2024,
                        "region": region,
                    },
                }
            )
            assert result["region_access"] == expected

    asyncio.run(run())
