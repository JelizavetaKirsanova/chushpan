"""Lifecycle and persistent boundaries; these doubles are not live-site evidence."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.cinema.adapters import HTML5Adapter, SafariBridge
from backend.cinema.playback import (
    PlaybackManager,
    PlaybackSource,
    PlaybackState,
    PlayerController,
    SourceValidator,
    playback_error,
)
from backend.cinema.service import Cinema
from backend.cinema.store import CinemaStore
from backend.config import Settings
from backend.context import DeviceContext
from backend.hud.bus import HudBus
from backend.mac_agent import LocalMacAgent
from backend.memory.store import MemoryStore
from backend.tools.registry import ToolError, ToolRegistry


def source():
    return PlaybackSource(
        "https://example.org/full", "title", "episode", "source", "link", "safari", "unknown"
    )


def bound():
    m = PlaybackManager()
    m.bind(source(), adapter=SimpleNamespace(), token="one", sample_position=12)
    return m


def test_page_open_is_blocked_and_never_confirms_capabilities():
    m = bound()
    m.outcome({"page_opened": True, "message": "Открыта страница."})
    assert m.state == PlaybackState.BLOCKED
    assert not m.snapshot()["source"]["capabilities"]
    assert not m.snapshot()["source"]["validation"].get("live_player_verified")
    with pytest.raises(ValueError):
        m.transition("PLAYING")


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"position": 0, "duration": 0},
        {"position": float("nan"), "duration": 120},
        {"position": 12, "duration": 120, "seeking": True},
    ],
)
def test_invalid_samples_cannot_confirm_playback(bad):
    m = bound()
    assert not m.confirmed({**bad, "playback_confirmed": True, "paused": False})
    assert m.state != PlaybackState.PLAYING


def test_stalled_player_recovers_only_on_movement(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("backend.cinema.playback.time.monotonic", lambda: now[0])
    m = bound()
    sample = {"position": 13, "duration": 120, "paused": False}
    m.observe(sample)
    assert m.state == PlaybackState.PLAYING
    now[0] += 31
    m.observe(sample)
    assert m.state == PlaybackState.BUFFERING and m.code == "playback_stalled"
    m.observe({**sample, "position": 14})
    assert m.state == PlaybackState.PLAYING
    m.session["counted"] = True
    m.observe({**sample, "position": 14, "paused": True})
    assert m.state == PlaybackState.PAUSED
    m.observe({**sample, "position": 80}, "seek")
    assert m.state != PlaybackState.PLAYING


def test_source_validator_rejects_saved_trailer_before_any_player_call():
    target = {
        "item": {"id": "title", "title": "Film", "year": 2026, "kind": "movie"},
        "episode": {"id": "ep", "season": 0, "episode": 0},
        "source": {"id": "src", "adapter": "html5"},
        "link": {"id": "link", "url": "https://example.org/film/trailer"},
    }
    with pytest.raises(ToolError, match="промо"):
        SourceValidator().validate(target, {})
    target["link"]["url"] = "https://example.org/film"
    with pytest.raises(ToolError):
        SourceValidator().validate(target, {"video_type": "trailer", "content_verified": True})


def test_false_adapter_confirmation_is_rejected():
    adapter = SimpleNamespace(
        open=AsyncMock(return_value={"playback_confirmed": True, "page_opened": True})
    )
    with pytest.raises(ToolError, match="время"):
        asyncio.run(PlayerController(adapter).open({}))


def test_safari_permission_denial_retains_machine_readable_reason():
    async def run():
        bridge = SafariBridge(
            Settings(),
            executor=SimpleNamespace(
                _run=AsyncMock(
                    side_effect=playback_error("automation_denied", "Нет разрешения Safari.")
                )
            ),
        )
        with pytest.raises(ToolError) as exc:
            await bridge.evaluate("https://example.org/full", {"action": "status"})
        m = bound()
        m.failure(exc.value)
        assert m.state == PlaybackState.BLOCKED and m.code == "automation_denied"

    asyncio.run(run())


def test_no_player_and_autoplay_block_do_not_report_playing(monkeypatch):
    async def run():
        # Advance deadlines without waiting fifteen seconds in a unit test.
        clock = iter(range(0, 10000, 5))
        monkeypatch.setattr("backend.cinema.adapters.time.monotonic", lambda: next(clock))
        monkeypatch.setattr("backend.cinema.adapters.asyncio.sleep", AsyncMock())
        bridge = SimpleNamespace(
            evaluate=AsyncMock(side_effect=playback_error("no_player", "Видео отсутствует.")),
            ensure_page=AsyncMock(),
        )
        result = await HTML5Adapter(bridge).open(
            {"link": {"url": "https://example.org/full"}, "episode": {"position": 55}}
        )
        assert not result["playback_confirmed"] and "отсутствует" in result["message"]
        clock = iter(range(10000))
        state = {"position": 55, "duration": 120, "paused": True, "play_error": "gesture_required"}
        adapter = HTML5Adapter(SimpleNamespace(evaluate=AsyncMock(return_value=state)))
        with pytest.raises(ToolError, match="автозапуск"):
            await adapter.control("play")

    asyncio.run(run())


class Player:
    def __init__(self):
        self.position, self.paused = 0, False

    async def open(self, target):
        self.position = target["episode"]["position"] + 0.2
        return {**await self.sample(), "playback_confirmed": True, "mode": "html5"}

    async def sample(self):
        return {"position": self.position, "duration": 1000, "paused": self.paused}

    async def detach(self):
        pass


def test_resume_next_and_failed_reads_preserve_existing_sqlite(tmp_path):
    async def run():
        path = tmp_path / "assistant.sqlite"
        memory = MemoryStore(path)
        tools = ToolRegistry()
        tools.mac_agent = LocalMacAgent()
        player = Player()
        cinema = Cinema(
            tools, Settings(data_dir=tmp_path), memory, adapter_factory=lambda _: player
        )
        cinema.bind(HudBus(), SimpleNamespace(device=DeviceContext.local_mac()))
        try:
            src = await cinema.db(
                "save_source",
                {"name": "Fixture", "base_url": "http://localhost:8765", "adapter": "html5"},
            )
            entry = {
                "title": "Fixture",
                "year": 2026,
                "kind": "series",
                "source_id": src["id"],
                "url": "http://localhost:8765/one",
            }
            item = await cinema.db("add", entry)
            item = await cinema.db(
                "add", {**entry, "episode": 2, "url": "http://localhost:8765/two"}
            )
            ep = item["episodes"][0]
            await cinema.db("progress", ep["id"], 160, 1000, "manual", ep["links"][0]["id"])
            result = await cinema.resume(item["id"], ep["id"])
            assert result["playback_confirmed"] and cinema.playback.state == PlaybackState.PLAYING
            assert 157 <= player.position < 158
            assert not await cinema.save(
                {"position": 0, "duration": 0, "error": "broken"}, "periodic"
            )
            assert (await cinema.db("target", item["id"], ep["id"]))["episode"]["position"] > 157
            await cinema.execute("Включи следующую серию")
            assert cinema.session["target"]["episode"]["episode"] == 2
            with pytest.raises(ToolError):
                await cinema.viewer_control(
                    {"op": "playback_control", "token": "old", "action": "pause"}
                )
        finally:
            await cinema.close()
            memory.close()
        reopened = CinemaStore(path)
        try:
            assert reopened.detail(item["id"])["episodes"][0]["position"] > 157
        finally:
            reopened.close()

    asyncio.run(run())


def test_audio_track_survives_restart_without_mutating_saved_link_variants(tmp_path):
    path = tmp_path / "cinema.sqlite"
    store = CinemaStore(path)
    src = store.save_source(
        {"name": "Fixture", "base_url": "https://example.org", "adapter": "html5"}
    )
    item = store.add(
        {
            "title": "Film",
            "kind": "movie",
            "source_id": src["id"],
            "url": "https://example.org/full",
            "voice": "Original",
        }
    )
    ep = item["current"]
    link = ep["links"][0]
    store.playback_state(ep["id"], link["id"], 42, 100, True, "English")
    store.playback_voice(ep["id"], link["id"], "Русский")
    store.close()
    store = CinemaStore(path)
    try:
        target = store.target(item["id"])
        assert target["link"]["voice"] == "Русский"
        assert target["episode"]["links"][0]["voice"] == "Original"
        assert target["episode"]["playback_state"]["position"] == 42
    finally:
        store.close()


def test_site_adapter_registration_is_explicit_and_host_scoped():
    from backend.cinema.sources import SourceManager, WebsiteAdapter

    class OfficialAdapter(WebsiteAdapter):
        pass

    manager = SourceManager(AsyncMock())
    manager.register_adapter("official", OfficialAdapter, hosts=("example.org",))
    source = {"name": "Official", "base_url": "https://example.org", "adapter": "html5"}
    assert isinstance(manager.adapter(source), OfficialAdapter)
    assert type(manager.adapter({**source, "base_url": "https://other.example"})) is WebsiteAdapter
    with pytest.raises(ValueError):
        manager.register_adapter("unsafe", OfficialAdapter, hosts=("*.example.org",))
    with pytest.raises(ValueError):
        manager.register_adapter("unsafe", OfficialAdapter)


def test_direct_provider_proof_is_bound_to_exact_active_asset_and_never_a_trailer():
    query = {"title": "Film", "year": 2026, "kind": "movie", "metadata": {"runtime": 100}}
    proof = {"content_verified": True, "video_type": "full_movie"}
    url = "https://example.org/full.mp4"
    evidence = {"live": True, "page_url": url, "media_url": url, "duration": 6000}
    assert SourceValidator.player(query, evidence, url, proof)["content_verified"]
    assert not SourceValidator.player(
        query, {**evidence, "media_url": "https://example.org/other.mp4"}, url, proof
    )["content_verified"]
    assert not SourceValidator.player(query, {**evidence, "duration": 60}, url, proof)[
        "content_verified"
    ]
    assert not SourceValidator.player(query, evidence, url, {})["content_verified"]
    assert (
        SourceValidator.player(
            query, {**evidence, "media_url": "https://example.org/trailer.mp4"}, url, proof
        )["video_type"]
        == "trailer"
    )
