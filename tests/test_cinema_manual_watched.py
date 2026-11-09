"""Manual title marks never manufacture or erase playback evidence."""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.cinema.commands import command
from backend.cinema.service import Cinema
from backend.cinema.store import MIGRATIONS, CinemaStore
from backend.config import Settings
from backend.context import DeviceContext
from backend.hud.bus import HudBus
from backend.mac_agent import LocalMacAgent
from backend.memory.store import MemoryStore
from backend.tools.registry import ToolRegistry


def test_manual_progress_is_per_episode_persistent_and_resettable(tmp_path):
    path = tmp_path / "manual.sqlite"
    store = CinemaStore(path)
    item = store.add({"title": "Последнее королевство", "kind": "series"})
    saved = store.manual_progress(item["id"], 2, 5, 1965, 2700, "watching", "2026-11-08")
    assert (saved["current"]["season"], saved["current"]["episode"]) == (2, 5)
    assert saved["current"]["position"] == 1965
    assert saved["current"]["last_viewed"].startswith("2026-11-08")
    store.close()
    store = CinemaStore(path)
    assert store.detail(item["id"])["current"]["position"] == 1965
    store.manual_progress(item["id"], 2, 5, 0, 2700, "unwatched")
    assert store.detail(item["id"])["current"]["position"] == 0
    assert store.detail(item["id"])["current"]["last_viewed"] is None
    store.close()


def test_progress_voice_routes():
    assert command(
        "Чушпан, запомни, что я остановилась на третьем сезоне, пятой серии, на 24 минуте"
    ) == {"action": "set_progress", "title": "", "season": 3, "episode": 5, "position": 1440}
    assert command(
        "Поставь Последнее королевство на второй сезон, пятую серию, 32 минуты 45 секунд"
    ) == {
        "action": "set_progress",
        "title": "последнее королевство",
        "season": 2,
        "episode": 5,
        "position": 1965,
    }
    assert command("Я досмотрела вторую серию первого сезона") == {
        "action": "complete_episode",
        "episode": 2,
        "season": 1,
    }
    assert command("Продолжи Последнее королевство")["action"] == "continue_title"


def test_manual_edit_invalidates_old_active_player_samples(tmp_path):
    async def run():
        memory = MemoryStore(tmp_path / "assistant.sqlite")
        registry = ToolRegistry()
        registry.mac_agent = LocalMacAgent()
        cinema = Cinema(registry, Settings(data_dir=tmp_path / "data"), memory)
        cinema.bind(HudBus(), SimpleNamespace(device=DeviceContext.local_mac()))
        cinema.tavily_credentials.status = lambda: {"configured": False}
        item = await cinema.db("add", {"title": "Сериал", "kind": "series"})
        await cinema.db("season", item["id"], [{"season": 1, "episode": 1}])
        episode = (await cinema.db("detail", item["id"]))["current"]
        from backend.cinema.playback import PlaybackSource

        cinema.playback.bind(
            PlaybackSource("", item["id"], episode["id"], "", "", "safari", "unknown"),
            target={"item": item, "episode": episode},
            mode="html5",
            token="old-session",
            adapter=SimpleNamespace(),
        )
        cinema.stop_tracking = AsyncMock()
        await cinema.handle(
            {
                "op": "manual_progress",
                "title_id": item["id"],
                "season": 1,
                "episode": 1,
                "position": 1965,
                "duration": 2700,
                "status": "watching",
                "last_viewed": "2026-11-08",
            }
        )
        cinema.stop_tracking.assert_awaited_once_with(flush=False)
        assert (await cinema.db("detail", item["id"]))["current"]["position"] == 1965
        await cinema.close()
        memory.close()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["movie", "series"])
def test_manual_toggle_survives_restart_and_keeps_episode_evidence(tmp_path, kind):
    path = tmp_path / "cinema.sqlite"
    store = CinemaStore(path)
    item = store.add({"title": "История", "kind": kind})
    identifier, episode = item["id"], item["current"]["id"]
    if kind == "series":
        store.season(identifier, [{"season": 1, "episode": 2}])
    store.progress(episode, 30, 100, "pause")
    store.update_title(
        identifier, {"user_rating": 9.5, "favorite": True, "user_note": "Моя заметка"}
    )
    episodes = store.detail(identifier)["episodes"]
    history = store.history()
    for watched in (True, False, True):
        marked = store.update_title(identifier, {"watched": watched})
        assert marked["status"] == ("watched" if watched else "watching")
        assert marked["episodes"] == episodes
        assert store.history() == history
        assert marked["user_rating"] == 9.5 and marked["user_note"] == "Моя заметка"
        assert marked["favorite"]
        profile = store.analytics()
        assert profile["watched_movies" if kind == "movie" else "watched_series"] == int(watched)
    store.close()
    store = CinemaStore(path)
    assert store.detail(identifier)["status"] == "watched"
    # A live sample can save progress without clearing the explicit title mark.
    assert store.progress(episode, 35, 100, "periodic")["status"] == "watched"
    store.update_title(identifier, {"watched": False})
    assert store.progress(episode, 99, 100, "ended")["status"] == "watching"
    assert store.detail(identifier)["current"]["watched"] == 1
    if kind == "series":
        assert store.detail(identifier)["episodes"][1]["watched"] == 0
    store.close()


def test_automatic_completion_and_play_remain_separate_from_manual_mark(tmp_path):
    store = CinemaStore(tmp_path / "cinema.sqlite")
    item = store.add({"title": "Фильм", "kind": "movie"})
    identifier, episode = item["id"], item["current"]["id"]
    assert store.progress(episode, 1, 100, "playing")["status"] == "watching"
    assert store.progress(episode, 96, 100, "periodic")["status"] == "watched"
    before = store.detail(identifier)["episodes"]
    store.update_title(identifier, {"watched": False})
    store.close()
    store = CinemaStore(tmp_path / "cinema.sqlite")
    assert store.detail(identifier)["status"] == "watching"
    assert store.detail(identifier)["episodes"] == before
    assert store.progress(episode, 97, 100, "periodic")["status"] == "watching"
    # Explicit replay begins a new automatic completion cycle.
    store.replay(identifier)
    assert store.progress(episode, 96, 100)["status"] == "watched"
    store.close()


def test_legacy_migration_and_backup_preserve_marks(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE cinema_migrations (version INTEGER PRIMARY KEY)")
    for version, script in enumerate(MIGRATIONS[:-1], 1):
        db.executescript(script)
        db.execute("INSERT INTO cinema_migrations VALUES (?)", (version,))
    db.commit()
    db.close()
    store = CinemaStore(path)
    item = store.add({"title": "Фильм", "kind": "movie"})
    store.update_title(item["id"], {"watched": True, "user_rating": 8.5})
    store.update_title(item["id"], {"watched": False})
    exported = store.export_library()
    restored = CinemaStore(tmp_path / "restored.sqlite")
    restored.import_library(exported)
    saved = restored.library()[0]
    assert saved["manual_watched"] == 0 and saved["watchlist"] == 0
    assert saved["user_rating"] == 8.5
    assert restored.progress(saved["current"]["id"], 99, 100)["status"] == "watching"
    for values in ({"watched": 1}, {"watchlist": "yes"}, {"watched": True, "status": "planned"}):
        with pytest.raises(ValueError):
            restored.update_title(saved["id"], values)
    restored.close()
    store.close()


def test_hud_toggle_publishes_library_and_keeps_playback_tracking(tmp_path):
    async def run():
        memory = MemoryStore(tmp_path / "assistant.sqlite")
        registry = ToolRegistry()
        registry.mac_agent = LocalMacAgent()
        cinema = Cinema(registry, Settings(data_dir=tmp_path / "data"), memory)
        bus = HudBus()
        cinema.bind(bus, SimpleNamespace(device=DeviceContext.local_mac()))
        cinema.tavily_credentials.status = lambda: {"configured": False}
        item = await cinema.db("add", {"title": "Сериал", "kind": "series"})
        from backend.cinema.playback import PlaybackSource

        cinema.playback.bind(
            PlaybackSource("", item["id"], item["current"]["id"], "", "", "safari", "unknown"),
            target={"item": item, "episode": item["current"]},
            mode="html5",
            token="fixture",
            adapter=SimpleNamespace(),
        )
        cinema.stop_tracking = AsyncMock()
        for watched in (True, False):
            await cinema.handle({"op": "update", "id": item["id"], "values": {"watched": watched}})
            event = next(e for e in bus.snapshot() if e["type"] == "cinema")
            assert event["items"][0]["status"] == ("watched" if watched else "planned")
            assert event["saved"] == item["id"]
            await cinema.handle({"op": "profile", "request_id": "profile"})
            event = next(e for e in bus.snapshot() if e["type"] == "cinema")
            assert event["personal_result"]["profile"]["watched_series"] == int(watched)
        cinema.stop_tracking.assert_not_awaited()
        cinema.session = None
        await cinema.close()
        memory.close()

    asyncio.run(run())
