"""Discovery, cost policy and one-click orchestration; no external effects in unit tests."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.cinema.models import Source
from backend.cinema.public_search import PublicCatalogue, Robots
from backend.cinema.service import Cinema
from backend.config import Settings
from backend.context import DeviceContext
from backend.hud.bus import HudBus
from backend.mac_agent import LocalMacAgent
from backend.memory.store import MemoryStore
from backend.tools.registry import ToolError, ToolRegistry

BASE = "https://public.example"
URL = BASE + "/serial/bar-odin-zvonok"
QUERY = {"title": "Бар «Один звонок»", "year": 2025, "kind": "series"}


class CatalogueHTTP:
    def __init__(self, *, year=2025, kind="TVSeries", name=QUERY["title"], status=200):
        self.year, self.kind, self.name, self.status = year, kind, name, status
        self.calls = []

    async def request(self, url, **kwargs):
        self.calls.append((url, kwargs.get("method", "GET")))
        if url.endswith("robots.txt"):
            return httpx.Response(
                200, text=f"User-agent: *\nDisallow: /search\nSitemap: {BASE}/map.xml"
            )
        if url.endswith("map.xml"):
            return httpx.Response(self.status, text=f"<urlset><url><loc>{URL}</loc></url></urlset>")
        if kwargs.get("method") == "HEAD":
            return httpx.Response(200, headers={"content-type": "text/html"})
        data = {"@type": self.kind, "name": self.name, "datePublished": self.year}
        return httpx.Response(
            200, text='<script type="application/ld+json">' + json.dumps(data) + "</script>"
        )


@pytest.mark.parametrize(
    "legacy,status",
    [
        ("unknown", "UNKNOWN"),
        ("free", "FREE"),
        ("subscription", "PAID"),
        ("rent", "PAID"),
        ("buy", "PAID"),
    ],
)
def test_cost_migration_is_explicit_and_unknown_never_paid(legacy, status):
    source = Source(name="Source", base_url=BASE, access_type=legacy)
    assert source.access_status == status
    assert Source(name="Mixed", base_url=BASE, access_status="MIXED").access_status == "MIXED"


def test_robots_duplicate_groups_wildcards_specific_agent_and_allow_tie():
    rules = Robots(
        "User-agent: *\nAllow: /\n\nUser-agent: *\nDisallow: /search\n"
        "Disallow: /*?*\nAllow: /allowed?x=1$\n"
    )
    assert rules.allowed(BASE + "/serial/show")
    assert not rules.allowed(BASE + "/search/film")
    assert not rules.allowed(BASE + "/?q=film")
    assert rules.allowed(BASE + "/allowed?x=1")
    assert not rules.allowed(BASE + "/allowed?x=12")
    specific = Robots(
        "User-agent: *\nDisallow: /\nUser-agent: Chushpan-Cinema\n"
        "Allow: /public\nDisallow: /public/private\n"
    )
    assert specific.allowed(BASE + "/public")
    assert not specific.allowed(BASE + "/public/private")


@pytest.mark.parametrize(
    "year,kind,name,expected",
    [
        (2025, "TVSeries", QUERY["title"], 1),
        (2024, "TVSeries", QUERY["title"], 0),
        (2025, "Movie", QUERY["title"], 0),
        (2025, "TVSeries", "Бар: другое кино", 0),
    ],
)
def test_sitemap_only_opens_actual_exact_title_year_type(year, kind, name, expected):
    async def run():
        http = CatalogueHTTP(year=year, kind=kind, name=name)
        catalogue = PublicCatalogue({"base_url": BASE}, http)
        rows = await catalogue.search(QUERY)
        assert len(rows) == expected
        if rows:
            assert rows[0]["url"] == URL and rows[0]["scope"] == "series"
        await catalogue.search(QUERY)
        assert sum(url.endswith("map.xml") for url, _ in http.calls) == 1
        assert not any("/search" in url for url, _ in http.calls)

    asyncio.run(run())


def test_blocked_sitemap_is_concrete_error_and_does_not_request_title():
    async def run():
        http = CatalogueHTTP(status=403)
        with pytest.raises(ToolError, match="HTTP 403"):
            await PublicCatalogue({"base_url": BASE}, http).search(QUERY)
        assert not any(url == URL for url, _ in http.calls)

    asyncio.run(run())


class Player:
    def __init__(self):
        self.opened = []

    async def detach(self):
        pass

    async def open(self, target):
        self.opened.append(target)
        return {
            "mode": "manual",
            "page_opened": True,
            "playback_confirmed": False,
            "message": "Открыта конкретная страница.",
        }


def setup(tmp_path):
    memory = MemoryStore(tmp_path / "assistant.sqlite")
    tools = ToolRegistry()
    tools.mac_agent = LocalMacAgent()
    player = Player()
    service = Cinema(
        tools, Settings(data_dir=tmp_path / "data"), memory, adapter_factory=lambda _: player
    )
    service.bind(HudBus(), SimpleNamespace(device=DeviceContext.local_mac()))
    service.sources.http = CatalogueHTTP()
    return service, memory, player


async def seed(service, *, status="UNKNOWN"):
    source = await service.db(
        "save_source",
        {
            "name": "Public catalogue",
            "base_url": BASE,
            "search_mode": "sitemap",
            "access_status": status,
        },
    )
    # Keep the optional official alternative disconnected during isolated tests.
    await service.db(
        "save_source", {"name": "KION", "base_url": "https://kion.ru", "enabled": False}
    )
    item = await service.db("add", QUERY)
    return source, item


def test_watch_search_open_saved_repeat_and_episode_restore(tmp_path):
    async def run():
        service, memory, player = setup(tmp_path)
        try:
            source, item = await seed(service)
            result = await service.resume(item["id"])
            assert not result["playback_confirmed"] and not player.opened
            assert (
                "подтвержд" in result["message"].lower() or "страница" in result["message"].lower()
            )
            choices = await service.sources.search(item["id"])
            await service.source_result(
                choices["results"][0]["id"], open_result=True, manual_choice=True
            )
            first = await service.db("detail", item["id"])
            ep = first["current"]
            await service.db("progress", ep["id"], 120, 1000, "manual", ep["links"][0]["id"])
            before = len(service.sources.http.calls)
            await service.resume(item["id"])
            assert not any(
                url.endswith("map.xml") for url, _ in service.sources.http.calls[before:]
            )
            assert (
                len(player.opened) == 1
            )  # A series landing page never becomes a verified episode.
            assert (await service.db("detail", item["id"]))["current"]["position"] == 120
            await service.db("season", item["id"], [{"season": 2, "episode": 3, "name": "Test"}])
            updated = await service.db("detail", item["id"])
            next_ep = next(e for e in updated["episodes"] if e["season"] == 2)
            result = await service.resume(item["id"], next_ep["id"])
            assert len(player.opened) == 1
            assert (
                "подтвержд" in result["message"].lower() or "страница" in result["message"].lower()
            )
            assert not result["playback_confirmed"]
            assert (await service.db("detail", item["id"]))["current"]["id"] == ep["id"]
            assert source["id"] == first["source_memory"]["last_source_id"]
        finally:
            await service.close()
            memory.close()

    asyncio.run(run())


def test_unknown_free_filter_keeps_manual_choice_without_automatic_open(tmp_path):
    async def run():
        service, memory, player = setup(tmp_path)
        try:
            _, item = await seed(service)
            await service.db("configure", {"free_only": True})
            choices = await service.sources.search(item["id"])
            row = choices["results"][0]
            assert (
                row["access_status"] == "UNKNOWN" and not row["allowed"] and row["manual_allowed"]
            )
            assert choices["selected_id"] is None
            with pytest.raises(ToolError, match="бесплатные"):
                await service.source_result(row["id"], open_result=True)
            assert not player.opened
            await service.source_result(row["id"], open_result=True, manual_choice=True)
            assert player.opened[-1]["link"]["url"] == URL
        finally:
            await service.close()
            memory.close()

    asyncio.run(run())


def test_paid_confirmation_bound_to_result_and_never_paywall_bypass(tmp_path):
    async def run():
        service, memory, player = setup(tmp_path)
        try:
            _, item = await seed(service, status="PAID")
            result = await service.resume(item["id"])
            assert not player.opened and not result["playback_confirmed"]
            choices = await service.sources.search(item["id"])
            await service.source_result(
                choices["results"][0]["id"], open_result=True, manual_choice=True
            )
            assert len(service.watch_confirmations) == 1
            token = next(iter(service.watch_confirmations))
            await service.handle({"op": "watch_confirm", "token": token})
            assert len(player.opened) == 1 and player.opened[-1]["link"]["url"] == URL
            await service.handle({"op": "watch_confirm", "token": token})
            assert len(player.opened) == 1
        finally:
            await service.close()
            memory.close()

    asyncio.run(run())


def test_disabled_auto_search_does_not_contact_new_sites(tmp_path):
    async def run():
        service, memory, player = setup(tmp_path)
        try:
            _, item = await seed(service)
            await service.db("configure", {"auto_search": False})
            await service.resume(item["id"])
            assert not service.sources.http.calls and not player.opened
        finally:
            await service.close()
            memory.close()

    asyncio.run(run())


def test_homepage_is_never_a_watch_result(tmp_path):
    async def run():
        service, memory, player = setup(tmp_path)
        try:
            source, item = await seed(service)
            await service.db(
                "add",
                {**QUERY, "title_id": item["id"], "source_id": source["id"], "url": BASE + "/"},
            )
            choices = await service.sources.search(item["id"], saved_only=True)
            assert not choices["selected_id"]
            remembered = await service.source_result(choices["results"][0]["id"])
            assert "Источник выбран вручную" in remembered["message"]
            saved = await service.db("detail", item["id"])
            assert saved["source_memory"]["last_source_id"] == source["id"]
            with pytest.raises(ToolError, match="главная"):
                await service.source_result(
                    choices["results"][0]["id"], open_result=True, manual_choice=True
                )
            assert not player.opened
        finally:
            await service.close()
            memory.close()

    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["resume", "seek_failure", "fallback", "background_close"])
def test_site_adapter_normal_watch_lifecycle_progress_and_fallback(tmp_path, monkeypatch, scenario):
    from backend.cinema import service as module
    from backend.cinema.learned_player import LearnedPlayer
    from backend.cinema.playback import PlaybackState

    calls = []

    class Player(LearnedPlayer):
        revealed = False

        async def open(self, target):
            calls.append(target)
            self.session = object()
            self.frame_host = "api.nextembed.ws"
            self.engine = "venom_ax"
            self.voice = "LostFilm"
            self.report = {"player": target["recipe"]["player"], "players": ["Плеер 1", "Плеер 3"]}
            diagnostic = target["diagnostic"]
            await diagnostic("OPEN_SAFARI", "running", "Opening")
            await diagnostic("DETECT_PLAYER", "success", "Found")
            if scenario == "fallback" and len(calls) == 1:
                raise ToolError("First player failed")
            await diagnostic("PLAY", "running", "Playing")
            await diagnostic("RESTORE_POSITION", "running", "Restoring")
            restored = scenario != "seek_failure"
            await diagnostic("RESTORE_POSITION", "success" if restored else "limited", "Position")
            if restored:
                await diagnostic("FULLSCREEN", "running", "Fullscreen")
                await diagnostic("FULLSCREEN", "success", "Verified")
            self.capabilities = {"play": True, "seek": True, "fullscreen": restored}
            self.last_sample = {
                "position": target["position"] + 1 if restored else 1,
                "duration": 3500,
                "paused": False,
                "observed_at": 1000,
                "content_verified": True,
                "playback_confirmed": True,
                "resume_confirmed": restored,
                "capabilities": self.capabilities,
                "message": "Playing",
            }
            return self.last_sample

        async def sample(self):
            if scenario == "background_close" and not self.revealed:
                return {**self.last_sample, "observation_stale": True}
            return self.last_sample

        async def reveal(self):
            self.revealed = True
            self.last_sample = {**self.last_sample, "position": 321, "observed_at": 300000}

        async def detach(self):
            self.session = None

    async def discover(*args):
        return [{"url": "https://kinogo.online/serialy/show.html"}]

    async def run():
        service, memory, _ = setup(tmp_path)
        try:
            await service.db(
                "save_source",
                {
                    "name": "Kinogo",
                    "base_url": "https://kinogo.online",
                    "adapter": "html5",
                    "access_status": "FREE",
                },
            )
            item = await service.db(
                "add", {"title": "Title", "kind": "series", "season": 1, "episode": 1}
            )
            episode = item["current"]
            await service.db("progress", episode["id"], 90, 3500, "manual")
            monkeypatch.setattr(module, "LearnedPlayer", Player)
            monkeypatch.setattr(module, "discover_pages", discover)
            result = await service.resume(item["id"], episode["id"])
            assert result["playback_confirmed"] is True
            assert service.playback.state == PlaybackState.PLAYING
            assert calls[0]["position"] == 87
            actual = await service.db("detail", item["id"])
            assert actual["current"]["position"] == (90 if scenario == "seek_failure" else 88)
            assert actual["site_recipe"]["voice"] == "LostFilm"
            assert len(await service.db("site_templates")) == 1
            if scenario == "background_close":
                await service.stop_tracking()
                actual = await service.db("detail", item["id"])
                assert actual["current"]["position"] == 321
            if scenario == "fallback":
                assert [call["recipe"]["player"] for call in calls] == ["Плеер 1", "Плеер 3"]
                assert any(row["stage"] == "SITE_PLAYER" for row in await service.db("diagnostics"))
        finally:
            await service.close()
            memory.close()

    asyncio.run(run())


def test_cancel_pending_watch_clears_busy_state_and_keeps_progress(tmp_path):
    from backend.cinema.playback import PlaybackState

    async def run():
        service, memory, _ = setup(tmp_path)
        waiting = asyncio.Event()

        async def pending(*args, **kwargs):
            service.playback.transition(PlaybackState.SEARCHING)
            waiting.set()
            await asyncio.Event().wait()

        try:
            item = await service.db("add", {"title": "Title"})
            await service.db("progress", item["current"]["id"], 90, 3500, "manual")
            service.resume = pending
            request = asyncio.create_task(
                service.handle({"op": "watch", "title_id": item["id"], "request_id": "cancel-test"})
            )
            await waiting.wait()
            await service.handle({"op": "watch_cancel", "request_id": "cancel-test"})
            await asyncio.gather(request, return_exceptions=True)
            assert service.hud_search is None
            assert service.playback.state == PlaybackState.IDLE
            assert (await service.db("detail", item["id"]))["current"]["position"] == 90
        finally:
            await service.close()
            memory.close()

    asyncio.run(run())
