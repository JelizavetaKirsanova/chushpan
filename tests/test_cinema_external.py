"""Provider and diagnostic regressions; these doubles are not live-site evidence."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import httpx
import pytest

from backend.cinema.archive_open import FILMS, find, resolve
from backend.cinema.diagnostics import DiagnosticSafariBridge, SourceDiagnostics
from backend.cinema.playback import SourceValidator
from backend.cinema.sources import SourceManager
from backend.cinema.store import CinemaStore
from backend.config import Settings
from backend.tools.registry import ToolError


class HTTP:
    def __init__(self, change=None):
        self.change = change or {}

    async def request(self, url, **kwargs):
        film = next(f for f in FILMS if f["id"] == url.rsplit("/", 1)[-1])
        data = {
            "metadata": {
                "title": film["title"],
                "creator": "Blender Foundation",
                "year": film["year"],
                "mediatype": "movies",
                "licenseurl": "http://creativecommons.org/licenses/by/3.0/",
            },
            "files": [
                {"name": film["file"], "source": "original", "length": str(film["duration"])}
            ],
        }
        data["metadata"].update(self.change)
        return httpx.Response(200, json=data)


SOURCE = {
    "name": "Archive",
    "base_url": "https://archive.org",
    "adapter": "direct",
    "search_mode": "archive",
    "embed_allowed": True,
}


def test_open_film_and_each_episode_are_exactly_bound():
    async def run():
        for episode in (1, 2, 3):
            query = {
                "title": "Caminandes",
                "kind": "series",
                "year": 2013,
                "season": 1,
                "episode": episode,
            }
            row = await resolve(HTTP(), SOURCE, query=query)
            assert (
                row["content_verified"]
                and row["scope"] == "episode"
                and row["video_type"] == "episode"
            )
            assert row["media_url"].endswith(quote(FILMS[episode]["file"]))
        assert find({"title": "Caminandes", "kind": "series", "season": 2, "episode": 1}) is None
        assert find({"title": "Sintel", "kind": "movie", "year": 2026}) is None
        assert find({"title": "Unknown", "kind": "movie"}) is None
        assert find(url="https://evil.example/download/Sintel/trailer.mp4") is None

    asyncio.run(run())


@pytest.mark.parametrize(
    "change",
    [
        {"licenseurl": ""},
        {"creator": "Unknown uploader"},
        {"title": "Sintel Trailer"},
        {"year": "2011"},
        {"access-restricted-item": "true"},
    ],
)
def test_wrong_work_trailer_or_protected_item_cannot_inherit_provider_verification(change):
    with pytest.raises(ToolError):
        asyncio.run(
            resolve(HTTP(change), SOURCE, query={"title": "Sintel", "kind": "movie", "year": 2010})
        )


def test_archive_alias_does_not_authorize_other_media_or_episode():
    async def run():
        query = {"title": "Caminandes", "kind": "series", "year": 2013, "season": 1, "episode": 1}
        proof = await resolve(HTTP(), SOURCE, query=query)
        evidence = {
            "live": True,
            "page_url": proof["safari_url"],
            "media_url": proof["direct_media_urls"][1],
            "duration": 90,
        }
        assert SourceValidator.player(query, evidence, proof["safari_url"], proof)[
            "content_verified"
        ]
        for url in [
            "https://evil.example/serve/Caminandes1LlamaDrama/01_llama_drama_1080p.mp4",
            "https://archive.org/serve/Caminandes2GranDillama/02_gran_dillama_1080p.mp4",
            proof["media_url"] + "?redirect=trailer",
        ]:
            assert not SourceValidator.player(
                query, {**evidence, "media_url": url}, proof["safari_url"], proof
            )["content_verified"]

    asyncio.run(run())


class Adapter:
    def __init__(self, fail=None):
        self.position = 0
        self.fail = fail

    async def open(self, target):
        self.position = target["episode"]["position"]
        return await self.sample()

    async def sample(self):
        return {
            "position": self.position,
            "duration": 100,
            "ready": 4,
            "paused": True,
            "content_verified": True,
        }

    async def control(self, action, position=0):
        if action == self.fail:
            raise ToolError("Нужно действие пользователя.")
        if action == "play":
            self.position += 0.5
        if action == "seek":
            self.position = position
        return {
            **await self.sample(),
            "playback_confirmed": action == "play",
            "paused": action != "play",
        }

    async def detach(self):
        pass


def diagnostics_fixture(tmp_path, fail=None):
    store = CinemaStore(tmp_path / "cinema.sqlite")
    src = store.save_source(SOURCE)
    item = store.add(
        {
            "title": "Caminandes",
            "kind": "series",
            "year": 2013,
            "source_id": src["id"],
            "url": "https://archive.org/download/Caminandes1LlamaDrama/01_llama_drama_1080p.mp4",
        }
    )

    async def db(name, *args, **kwargs):
        return getattr(store, name)(*args, **kwargs)

    manager = SourceManager(db, http=HTTP())
    manager.adapter(src).checkAvailability = AsyncMock(
        return_value={"availability": "available", "reason": "HTTP 200"}
    )
    diag = SourceDiagnostics(manager, db, AsyncMock(), lambda: Adapter(fail), lambda: Adapter(fail))
    return store, src, item, diag


def test_http_success_never_confirms_player_and_trial_never_changes_library(tmp_path):
    async def run():
        store, src, item, diag = diagnostics_fixture(tmp_path, "fullscreen")
        try:
            before = json.dumps(store.export_library(), sort_keys=True)
            await diag.check(item["id"])
            row = store.source_diagnostics()["rows"][0]
            assert row["stages"]["site"]["status"] == "confirmed"
            assert row["stages"]["playback"]["status"] == "not_checked"
            assert row["playback_confirmed_at"] is None
            await diag.live(src["id"])
            row = store.source_diagnostics()["rows"][0]
            assert row["stages"]["playback"]["status"] == "confirmed"
            assert row["stages"]["resume"]["status"] == "confirmed"
            assert row["stages"]["fullscreen"]["status"] == "needs_user"
            assert not diag.running
            # Export creation time can change independently of the library.
            after = store.export_library()
            previous = json.loads(before)
            after.pop("exported_at", None)
            previous.pop("exported_at", None)
            assert after == previous
        finally:
            store.close()

    asyncio.run(run())


def test_failed_playback_cannot_keep_success_and_disabled_source_is_refused(tmp_path):
    async def run():
        store, src, item, diag = diagnostics_fixture(tmp_path, "play")
        try:
            await diag.check(item["id"])
            await diag.live(src["id"])
            row = store.source_diagnostics()["rows"][0]
            assert row["playback_confirmed_at"] is None
            assert row["stages"]["playback"]["status"] == "needs_user"
            store.save_source({**src, "enabled": False})
            with pytest.raises(ToolError, match="отключён"):
                await diag.live(src["id"])
        finally:
            store.close()

    asyncio.run(run())


def test_diagnostic_refuses_preexisting_tab_before_any_command():
    executor = SimpleNamespace(_run=AsyncMock(return_value="1"))
    opener = AsyncMock()
    with pytest.raises(ToolError, match="уже открыта"):
        asyncio.run(
            DiagnosticSafariBridge(Settings(), executor).ensure_page(
                "https://example.org/full", opener
            )
        )
    opener.assert_not_awaited()
    assert executor._run.await_count == 1


def test_head_405_uses_bounded_get_but_403_never_triggers_retry():
    from backend.cinema.sources import WebsiteAdapter

    async def run():
        http = SimpleNamespace(
            request=AsyncMock(
                side_effect=[
                    httpx.Response(405),
                    httpx.Response(200, headers={"content-type": "text/html"}),
                ]
            )
        )
        result = await WebsiteAdapter(
            {"base_url": "https://kion.ru", "adapter": "html5"}, http
        ).checkAvailability("https://kion.ru/serial/title")
        assert result["availability"] == "available" and result["http_status"] == 200
        assert http.request.await_args_list[1].kwargs["max_bytes"] == 4_000_000
        http = SimpleNamespace(request=AsyncMock(return_value=httpx.Response(403)))
        result = await WebsiteAdapter(
            {"base_url": "https://kion.ru", "adapter": "html5"}, http
        ).checkAvailability()
        assert result["availability"] == "restricted" and http.request.await_count == 1

    asyncio.run(run())


def test_remote_seek_waits_for_its_response_without_resubmitting(monkeypatch):
    from backend.cinema.native_player import AVPlayerAdapter

    async def run():
        waits = []

        async def wait_for(coroutine, timeout):
            waits.append(timeout)
            return await coroutine

        monkeypatch.setattr("backend.cinema.native_player.asyncio.wait_for", wait_for)
        player = AVPlayerAdapter(Settings())
        writes = []
        player.proc = SimpleNamespace(
            returncode=None,
            stdin=SimpleNamespace(write=writes.append, drain=AsyncMock()),
            stdout=SimpleNamespace(
                readline=AsyncMock(return_value=b'{"id":"1","position":20,"duration":90}\n')
            ),
        )
        result = await player.request("seek", position=20)
        assert result["position"] == 20 and waits == [30]
        assert len(writes) == 1 and json.loads(writes[0])["action"] == "seek"

    asyncio.run(run())


def test_refused_existing_tab_is_not_sampled_paused_or_detached_during_cleanup(tmp_path):
    from backend.cinema.adapters import HTML5Adapter

    async def run():
        store, src, item, diag = diagnostics_fixture(tmp_path)
        executor = SimpleNamespace(_run=AsyncMock(return_value="1"))
        adapter = HTML5Adapter(DiagnosticSafariBridge(Settings(), executor))
        diag.safari_factory = lambda: adapter
        try:
            await diag.check(item["id"])
            await diag.live(src["id"])
            assert executor._run.await_count == 1
            row = store.source_diagnostics()["rows"][0]
            assert (
                row["playback_confirmed_at"] is None
                and row["stages"]["player"]["status"] == "needs_user"
            )
            assert not diag.running
        finally:
            store.close()

    asyncio.run(run())
