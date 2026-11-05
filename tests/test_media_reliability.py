"""Media regressions with public-API doubles; never play user audio or request TCC."""

import asyncio
import plistlib
from unittest.mock import AsyncMock

import pytest

from backend.cinema.adapters import HTML5Adapter
from backend.config import Settings
from backend.media.safari import MusicAppNotFound, SafariMusic
from backend.memory.store import MemoryStore
from backend.tools.media import MediaService, media_tools
from backend.tools.registry import ToolError


def application(tmp_path, *, safari=True):
    path = tmp_path / "YouTube Music.app"
    (path / "Contents").mkdir(parents=True)
    bundle = "com.apple.Safari.WebApp.test" if safari else "example.music.player"
    data = {"CFBundleIdentifier": bundle, "CFBundleDisplayName": "YouTube Music"}
    if safari:
        data["Manifest"] = {"start_url": "https://music.youtube.com/?source=pwa"}
    (path / "Contents/Info.plist").write_bytes(plistlib.dumps(data))
    return path, bundle


def test_installed_app_wins_over_legacy_forced_safari_and_choice_survives_restart(tmp_path):
    path, bundle = application(tmp_path)
    settings = Settings()
    settings.media.provider = "youtube_music"
    config = settings.media.youtube_music
    config.transport, config.app_path, config.bundle_id = "safari", path, "com.apple.Safari"
    store = MemoryStore(":memory:")
    service = MediaService(settings, store=store)
    assert isinstance(service.youtube.app, SafariMusic)
    assert service.youtube.app.discover() == (path, bundle)
    assert service.music_application["mode"] == "webapp"
    saved = store.preference("media", "youtube_application")
    assert saved["transport"] == "auto" and saved["bundle_id"] == bundle
    fresh = Settings()
    fresh.media.provider = "youtube_music"
    restored = MediaService(fresh, store=store)
    assert isinstance(restored.youtube.app, SafariMusic)
    assert restored.youtube.app.discover()[0] == path
    store.close()


def test_missing_app_requires_explicit_browser_fallback(monkeypatch):
    def missing(*_):
        raise MusicAppNotFound("missing")

    monkeypatch.setattr(SafariMusic, "discover", missing)
    settings = Settings()
    settings.media.provider = "youtube_music"
    config = settings.media.youtube_music
    config.transport, config.app_path, config.bundle_id = "auto", None, None
    config.allow_browser = False
    media = MediaService(settings)
    assert isinstance(media.youtube.app, SafariMusic)
    config.allow_browser = True
    media.refresh_youtube()
    from backend.media.music_safari import SafariMusicTab

    assert isinstance(media.youtube.app, SafariMusicTab)


def test_native_app_needs_explicit_selection_and_launch_verifies_activation(tmp_path):
    path, bundle = application(tmp_path, safari=False)
    with pytest.raises(ToolError, match="не подтверждено"):
        SafariMusic.application(path)
    settings = Settings()
    settings.media.youtube_music.app_path = path
    settings.media.youtube_music.bundle_id = bundle
    app = SafariMusic(settings)
    assert SafariMusic.application(str(path), selected=True)["bundle_id"] == bundle
    app.request = AsyncMock(return_value={"activated": True})
    result = asyncio.run(app.launch())
    assert result["activated"]
    app.request.assert_awaited_once_with("launch", path=str(path))
    app.request = AsyncMock(return_value={"activated": False})
    with pytest.raises(ToolError, match="активацию"):
        asyncio.run(app.launch())


def test_tool_handlers_follow_application_switch(tmp_path):
    path, bundle = application(tmp_path)
    settings = Settings()
    settings.media.provider = "youtube_music"
    settings.media.youtube_music.app_path = path
    settings.media.youtube_music.bundle_id = bundle
    service = MediaService(settings)
    tools = media_tools(settings, service)
    old = service.youtube
    service.refresh_youtube()
    assert service.youtube is not old
    service.youtube.search = AsyncMock(return_value={"ok": True, "new_app": True})
    tool = next(t for t in tools if t.name == "music_search")
    assert asyncio.run(tool.handler(query="jazz"))["new_app"]


def test_player_verifies_play_and_fullscreen_independently():
    class Bridge:
        def __init__(self, blocked=False):
            self.position, self.paused, self.fullscreen = 0, True, False
            self.blocked = blocked
            self.actions = []

        async def evaluate(self, url, request):
            action = request["action"]
            self.actions.append(action)
            if action == "seek":
                self.position = request["position"]
            if action == "play":
                self.paused = False
            if action == "fullscreen" and not self.blocked:
                self.fullscreen = True
            if not self.paused:
                self.position += 0.5
            return {
                "position": self.position,
                "duration": 1000,
                "paused": self.paused,
                "fullscreen": self.fullscreen,
                "fullscreen_error": "gesture_required" if self.blocked else None,
            }

    async def run():
        for blocked in (False, True):
            bridge = Bridge(blocked)
            result = await HTML5Adapter(bridge).open(
                {"link": {"url": "https://example.org/one"}, "episode": {"position": 300}}
            )
            assert result["playback_confirmed"]
            assert result["fullscreen_confirmed"] is not blocked
            assert (
                bridge.actions.index("seek")
                < bridge.actions.index("play")
                < bridge.actions.index("fullscreen")
            )
            assert ("offer_gesture" in bridge.actions) is blocked
            if blocked:
                assert result["result_status"] == "partial_success"

    asyncio.run(run())


def test_music_missing_deep_link_never_opens_a_browser(tmp_path, monkeypatch):
    path, bundle = application(tmp_path)
    settings = Settings()
    settings.media.youtube_music.app_path = path
    settings.media.youtube_music.bundle_id = bundle
    app = SafariMusic(settings)
    app.config.timeout_s = 0.01
    app.request = AsyncMock(return_value={"running": True})
    app.snapshot = AsyncMock(
        return_value=[{"role": "AXWebArea", "url": "https://music.youtube.com/library"}]
    )
    process = AsyncMock()
    monkeypatch.setattr("backend.media.safari.process", process)
    with pytest.raises(ToolError, match="Safari не открывался"):
        asyncio.run(app.navigate("https://music.youtube.com/playlist?list=missing"))
    process.assert_not_awaited()
