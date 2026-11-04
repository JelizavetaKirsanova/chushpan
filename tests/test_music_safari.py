"""No Apple Events, browser launches or real music. Fake native helper and DOM state."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend.config import Settings
from backend.media.music_safari import (
    MusicBrowserError,
    SafariMusicTab,
    SafariYouTubeMusic,
    validate_music_url,
)


def app_with_runner(automation="granted", tabs="10:1\n", state=None):
    calls = []

    async def runner(*args, data=None, timeout=None):
        calls.append((args, data))
        if data["op"] in {"probe", "request"}:
            return json.dumps({"automation": automation, "pid": 123, "ppid": 122})
        script = data["source"]
        if " is running" in script:
            return json.dumps({"result": "true"})
        if "set found" in script:
            return json.dumps({"result": tabs})
        return json.dumps({"result": json.dumps(state or {"state": "paused"})})

    app = SafariMusicTab(Settings(), runner=runner)
    app._binary = Path("/fake/music-safari")
    return app, calls


@pytest.mark.parametrize(
    "state,code",
    [
        ("denied", "automation_denied"),
        ("not_determined", "automation_unknown"),
        ("target_not_running", "safari_closed"),
        ("unknown", "automation_unknown"),
    ],
)
def test_unavailable_permission_blocks_every_script(state, code):
    app, calls = app_with_runner(state)
    report = asyncio.run(app.diagnose())
    assert report["automation"] == state and report["error_code"] == code
    assert all(data["op"] == "probe" for _, data in calls)


def test_same_binary_probe_and_script_nonprompting_readonly_diagnostic():
    app, calls = app_with_runner()
    report = asyncio.run(app.diagnose())
    assert report["tab_found"] and report["javascript"] == "granted"
    assert not report["playback_confirmed"]
    assert all(args == ("/fake/music-safari",) for args, _ in calls)
    scripts = [data for _, data in calls if data["op"] == "script"]
    assert scripts and all(not data["request_access"] for data in scripts)
    assert all(
        "set URL" not in data["source"] and "make new" not in data["source"] for data in scripts
    )
    assert all(
        r"\"op\": \"inspect\"" in data["source"]
        for data in scripts
        if "do JavaScript" in data["source"]
    )


def test_explicit_access_request_is_readonly():
    app, calls = app_with_runner("not_determined")
    asyncio.run(app.request_access())
    assert calls[0][1] == {
        "op": "request",
        "request_access": True,
        "target": "com.apple.Safari",
        "launch_target": True,
    }


def test_reuse_single_tab_and_reject_multiple_without_creating():
    app, calls = app_with_runner(tabs="10:2\r")
    assert asyncio.run(app.target(create=True)) == {"window": 10, "tab": 2}
    assert not any("make new" in data.get("source", "") for _, data in calls)
    app, calls = app_with_runner(tabs="10:1\n10:2\n")
    with pytest.raises(MusicBrowserError, match="несколько"):
        asyncio.run(app.target(create=True))
    assert not any("make new" in data.get("source", "") for _, data in calls)


@pytest.mark.parametrize("code", ["javascript_disabled", "automation_denied", "tab_changed"])
def test_native_errors_are_sanitized(code):
    async def runner(*args, **kwargs):
        return json.dumps({"error": code, "private": "secret-private-title"})

    app = SafariMusicTab(Settings(), runner=runner)
    app._binary = Path("/fake/helper")
    with pytest.raises(MusicBrowserError) as exc:
        asyncio.run(app._run("secret-private-script"))
    assert exc.value.code == code
    assert "secret-private" not in str(exc.value)


def playing(position, **kwargs):
    return {
        "state": "playing",
        "track": "Song",
        "artist": "Artist",
        "position": position,
        "ready": True,
        "ad": False,
        **kwargs,
    }


def test_playback_requires_advancing_position_and_correct_track():
    app, _ = app_with_runner()
    app.now_playing = AsyncMock(side_effect=[playing(1, track="Other"), playing(2), playing(3)])
    result = asyncio.run(app.wait_playing("Song", "Artist"))
    assert result["ok"] and app.stages["playback_confirmed"]


@pytest.mark.parametrize(
    "state,code",
    [
        (playing(1, play_error="autoplay_blocked"), "autoplay_blocked"),
        (playing(1, logged_out=True), "authorization_required"),
        (playing(1, muted=True), "audio_muted"),
        (playing(1, volume=0), "audio_muted"),
        (playing(1), "playback_blocked"),
        (playing(1, ad=True), "playback_blocked"),
    ],
)
def test_opened_or_frozen_player_never_means_success(state, code):
    app, _ = app_with_runner()
    app.timeout = 0.02
    app.now_playing = AsyncMock(return_value=state)
    with pytest.raises(MusicBrowserError) as exc:
        asyncio.run(app.wait_playing())
    assert exc.value.code == code and not app.stages["playback_confirmed"]


def test_search_and_play_are_distinct_stages_and_use_dom_only():
    app, _ = app_with_runner()
    app.navigate = AsyncMock()
    app.call = AsyncMock(
        return_value={
            "items": [
                {
                    "title": "Song",
                    "artist": "Artist",
                    "href": "https://music.youtube.com/watch?v=abc",
                }
            ]
        }
    )
    music = SafariYouTubeMusic(Settings(), app=app)
    music.api = None  # Even an absent legacy API must not affect the Safari backend.
    result = asyncio.run(music.search("Song", "song"))
    assert result["item"]["id"] == "abc"
    assert app.stages["search_executed"] and app.stages["track_found"]
    assert not app.stages["play_requested"] and not app.stages["playback_confirmed"]
    assert app.navigate.call_args.args[0] == "https://music.youtube.com/search?q=Song"


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com/watch?v=a",
        "https://music.youtube.com.evil/",
        "https://user:pass@music.youtube.com/",
        "file:///etc/passwd",
    ],
)
def test_music_cannot_navigate_outside_its_origin(url):
    with pytest.raises(MusicBrowserError):
        validate_music_url(url)


def test_new_song_waits_for_matching_media_before_resuming():
    app, _ = app_with_runner()
    app.now_playing = AsyncMock(side_effect=[playing(1, track="Previous"), playing(0)])
    app.call = AsyncMock(return_value={})
    asyncio.run(app.start_song("Song"))
    assert app.now_playing.await_count == 2
    app.call.assert_awaited_once_with("resume")


def test_new_song_never_resumes_previous_track():
    app, _ = app_with_runner()
    app.timeout = 0.02
    app.now_playing = AsyncMock(return_value=playing(1, track="Previous"))
    app.call = AsyncMock()
    with pytest.raises(MusicBrowserError) as exc:
        asyncio.run(app.start_song("Song"))
    assert exc.value.code == "no_media"
    app.call.assert_not_awaited()


def test_permission_can_become_granted_without_restarting():
    app, calls = app_with_runner("not_determined")
    asyncio.run(app.diagnose())

    async def allowed(*args, **kwargs):
        return json.dumps({"automation": "granted"})

    app.runner = allowed
    asyncio.run(app._ready())
    assert app._permission["automation"] == "granted"


def test_safe_cli_never_instantiates_playback_service(monkeypatch, capsys):
    import backend.tools.media
    from backend.media.cli import check_music

    settings = Settings()
    monkeypatch.setattr(
        SafariMusicTab,
        "diagnose",
        AsyncMock(return_value={"automation": "granted", "tab_found": True, "state": "paused"}),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("read-only diagnostics instantiated playback")

    monkeypatch.setattr(backend.tools.media, "MediaService", forbidden)
    assert asyncio.run(check_music(settings, json_output=True)) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "paused"


def test_music_commands_do_not_target_night_video_on_fresh_start():
    from backend.music import MusicCommands
    from tests.test_music_commands import Player

    settings, player = Settings(), Player()
    settings.media.provider = "youtube_music"
    player._last_control_target = "video"
    result = asyncio.run(MusicCommands(settings, player).execute("Пауза музыки"))
    assert result["ok"]
    assert player.calls == [("pause", "music")]


def test_unconfirmed_playback_is_not_started_again():
    from backend.music import MusicCommands
    from tests.test_music_commands import Player

    settings, player = Settings(), Player()
    settings.media.provider = "youtube_music"
    player.play_music = AsyncMock(side_effect=MusicBrowserError("playback_blocked"))
    controller = MusicCommands(settings, player)
    asyncio.run(controller.execute("Включи музыку"))
    result = asyncio.run(controller.execute("Включи музыку"))
    assert not result["ok"] and player.play_music.await_count == 1


@pytest.mark.parametrize("request_access", [False, True])
def test_in_app_diagnostics_use_same_live_adapter_without_playing(request_access):
    from types import SimpleNamespace

    from backend.music import MusicCommands, route

    settings = Settings()
    settings.media.provider = "youtube_music"
    app = SimpleNamespace(
        diagnose=AsyncMock(return_value={"automation": "granted", "tab_found": True}),
        request_access=AsyncMock(),
    )
    media = SimpleNamespace(youtube=SimpleNamespace(app=app))
    command = (
        "Чушпан, разреши управление YouTube Music" if request_access else "Проверь YouTube Music"
    )
    assert route(command)[0] == "music_command"
    result = asyncio.run(MusicCommands(settings, media).execute(command))
    assert result["ok"] and "не подтверждено" in result["message"]
    assert app.request_access.await_count == int(request_access)
    app.diagnose.assert_awaited_once()


@pytest.mark.parametrize(
    "actual", ["https://music.youtube.com/playlist?list=verified", "https://music.youtube.com/"]
)
def test_opening_playlist_requires_confirmed_target_url(actual):
    app, _ = app_with_runner()
    app.target = AsyncMock(return_value={"window": 1, "tab": 1})
    app._run = AsyncMock(return_value=actual)
    request = app.navigate("https://music.youtube.com/playlist?list=verified")
    if "list=verified" in actual:
        asyncio.run(request)
    else:
        with pytest.raises(MusicBrowserError):
            asyncio.run(request)
