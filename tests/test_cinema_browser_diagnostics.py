"""Guards around browser checks; live-site evidence is recorded separately."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from backend.cinema.archive_open import FILMS, verified_alias
from backend.cinema.browser_diagnostics import BrowserDiagnostics, configure_browser_cache
from backend.cinema.browser_search import BrowserSearch
from backend.cinema.evidence import diagnostic_reason, observations, public_url, safe_reason
from backend.cinema.internet import PageVerifier, SearchProviders
from backend.cinema.models import Preferences
from backend.cinema.playback import SourceValidator
from backend.cinema.sources import SourceManager
from backend.cinema.store import CinemaStore
from backend.hud.bus import HudBus
from backend.hud.server import create_app
from backend.permission_policy import requests_allowed


class OpenHTTP:
    async def request(self, url, **kwargs):
        assert "/metadata/" in url, "A verified free film must not wait for unrelated sites"
        film = next(f for f in FILMS if url.endswith("/" + f["id"]))
        return httpx.Response(
            200,
            json={
                "metadata": {
                    "title": film["title"],
                    "year": film["year"],
                    "mediatype": "movies",
                    "creator": film.get("creator", "Blender Foundation"),
                    "licenseurl": film.get(
                        "license", "https://creativecommons.org/licenses/by/3.0/"
                    ),
                },
                "files": [
                    {
                        "name": film["file"],
                        "source": "derivative" if film.get("original") else "original",
                        "original": film.get("original"),
                        "length": film["duration"],
                    },
                    {"name": film.get("original", "original.mov"), "source": "original"},
                ],
            },
        )


def test_first_watch_finds_full_film_without_a_key_saved_link_or_previous_success(tmp_path):
    async def run():
        store = CinemaStore(tmp_path / "memory.sqlite")
        store.save_source({"name": "Unrelated", "base_url": "https://bad.example"})
        item = store.add({"title": "Сита поёт блюз", "year": 2008, "kind": "movie"})

        async def db(name, *args, **kwargs):
            return getattr(store, name)(*args, **kwargs)

        manager = SourceManager(db, http=OpenHTTP())
        manager.web = SimpleNamespace(search=AsyncMock(side_effect=AssertionError("No API needed")))
        result = await manager.search(item["id"])
        row = manager.result(result["selected_id"])
        assert row["content_verified"] and row["duration"] > 4800 and row["auto_eligible"]
        assert row["source_config"]["adapter"] == "direct" and row["embedded"]
        assert row["viewing_status"] == "playable" and not row["playback_confirmed"]
        assert not item["current"]["links"]
        store.close()

    asyncio.run(run())


def test_cdn_alias_is_bound_to_exact_provider_work_file_scheme_and_duration():
    query = {"title": "Sita Sings the Blues", "year": 2008, "kind": "movie"}
    direct = "https://archive.org/download/Sita_Sings_the_Blues/Sita_Sings_the_Blues.mp4"
    cdn = "https://dn720802.ca.archive.org/0/items/Sita_Sings_the_Blues/Sita_Sings_the_Blues.mp4"
    proof = {
        "provider": "archive_open",
        "content_verified": True,
        "video_type": "full_movie",
        "duration": 4891.24,
    }
    evidence = {"live": True, "media_url": cdn, "duration": 4891.21}
    assert verified_alias(proof, direct, cdn, query)
    assert SourceValidator.player(query, evidence, direct, proof)["content_verified"]
    for wrong in [
        cdn.replace(".archive.org", ".archive.org.evil.test"),
        cdn.replace("Sita_Sings_the_Blues.mp4", "trailer.mp4"),
        cdn.replace("https:", "http:"),
        cdn + "?token=private",
        cdn.replace("/Sita_Sings_the_Blues/", "/Sintel/"),
    ]:
        assert not verified_alias(proof, direct, wrong, query)
    assert not SourceValidator.player(query, {**evidence, "duration": 120}, direct, proof)[
        "content_verified"
    ]


def test_page_identity_region_cost_controls_and_playback_are_independent():
    row = {
        "kind": "page",
        "match": 1,
        "scope": "movie",
        "availability": "available",
        "access_status": "UNKNOWN",
        "video_type": "unknown",
        "region_access": "unknown",
    }
    state = observations(row, "movie")
    assert state["browser_openable"] and state["viewing_status"] == "browser_only"
    assert state["identity_status"] == "confirmed" and state["playback_status"] == "unconfirmed"
    assert (
        observations({**row, "region_access": "blocked"}, "movie")["viewing_status"]
        == "region_blocked"
    )
    assert not observations({**row, "video_type": "trailer"}, "movie")["browser_openable"]
    assert "HTTP 403" in diagnostic_reason(
        {"reason": "HTTP 403", "classification_reason": "Тип не проверен"}
    )
    assert "secret" not in public_url("https://site.example/title?token=secret#secret")
    assert "private-value" not in safe_reason("Authorization: private-value")


def test_visible_title_year_is_only_a_manual_candidate_without_structured_identity():
    class HTML:
        async def request(self, url, **kwargs):
            return httpx.Response(
                200,
                text=""
                if url.endswith("robots.txt")
                else "<title>Последнее королевство 1–5 сезон (2015)</title>",
            )

    async def run():
        row = await PageVerifier(HTML()).verify(
            "https://source.test/last-kingdom",
            {
                "title": "Последнее королевство",
                "year": 2015,
                "kind": "series",
                "season": 1,
                "episode": 1,
            },
        )
        assert row["identity_status"] == "probable" and row["match"] < 0.95
        assert not row["content_verified"] and row["scope"] == "series"
        state = observations(row, "series")
        assert state["browser_openable"] and state["episode_status"] == "unconfirmed"

    asyncio.run(run())


def test_api_failure_uses_free_browser_but_never_another_paid_provider():
    async def run():
        api = SimpleNamespace(
            search=AsyncMock(return_value=[{"kind": "error", "reason": "HTTP 429"}])
        )
        free = SimpleNamespace(
            search=AsyncMock(return_value=[{"kind": "page", "url": "https://source.test/film"}])
        )
        paid = SimpleNamespace(search=AsyncMock(side_effect=AssertionError("Not selected")))
        engines = SearchProviders(None, {"tavily": api, "safari": free, "brave": paid})
        result = await engines.search({}, Preferences(), [])
        assert any(r["kind"] == "page" for r in result)
        free.search.assert_awaited_once()
        paid.search.assert_not_awaited()
        free.search.reset_mock()
        await engines.search({}, Preferences(allow_browser=False), [])
        free.search.assert_not_awaited()

    asyncio.run(run())


def test_safari_search_preserves_unicode_for_applescript_and_uses_ee_region(monkeypatch):
    async def run():
        scripts = []

        async def execute(script):
            assert not requests_allowed.get()
            scripts.append(script)
            if "make new document" in script:
                return "1234"
            if "do JavaScript" in script:
                return json.dumps({"urls": ["https://source.test/film"]})
            return "0"

        monkeypatch.setattr("backend.cinema.browser_search.asyncio.sleep", AsyncMock())
        result = await BrowserSearch(SimpleNamespace(_run=execute)).search("Фильм", "EE", "ru")
        assert result == ["https://source.test/film"]
        assert "kl=ee-et" in scripts[0]
        assert "подтвердите" in scripts[1] and r"\u043f" not in scripts[1]
        assert 'starts with "https://duckduckgo.com/"' in scripts[-1]

    asyncio.run(run())


def test_cancelled_browser_check_is_finalized_and_can_be_run_again(tmp_path, monkeypatch):
    import playwright.async_api

    async def run():
        entered = asyncio.Event()

        async def starting():
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(
            playwright.async_api, "async_playwright", lambda: SimpleNamespace(start=starting)
        )
        diagnostic = BrowserDiagnostics(tmp_path, AsyncMock())
        source = {"id": "source", "name": "Site", "base_url": "https://source.test"}
        task = asyncio.create_task(diagnostic.run(source, {"title": "Film"}, "EE"))
        await entered.wait()
        await diagnostic.cancel()
        assert (await task)["status"] == "cancelled" and diagnostic.task is None
        saved = json.loads((diagnostic.root / "latest.json").read_text())
        assert not saved["running"] and not saved["playback_confirmed"]

    asyncio.run(run())


def test_frozen_app_uses_standard_browser_cache_and_respects_an_explicit_path(monkeypatch):
    import os
    import sys
    from pathlib import Path

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/before-test")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    configure_browser_cache()
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(
        Path.home() / "Library/Caches/ms-playwright"
    )
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/custom/browser-cache")
    configure_browser_cache()
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/custom/browser-cache"


def test_private_browser_screenshot_rejects_foreign_embedding_and_path_traversal(tmp_path):
    from fastapi.testclient import TestClient

    identifier = "a" * 32
    folder = tmp_path / identifier
    folder.mkdir()
    (folder / "page.png").write_bytes(b"private screenshot")
    bus = HudBus()
    bus.cinema = SimpleNamespace(browser_diagnostics=SimpleNamespace(root=tmp_path))
    client = TestClient(create_app(bus, {"http://testserver"}, dist=tmp_path))
    endpoint = f"/cinema/diagnostics/{identifier}/image"
    assert client.get(endpoint, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert (
        client.get(endpoint, headers={"Sec-Fetch-Site": "same-origin"}).content
        == b"private screenshot"
    )
    assert (
        client.get(
            "/cinema/diagnostics/not-a-report/image", headers={"Sec-Fetch-Site": "same-origin"}
        ).status_code
        == 403
    )
