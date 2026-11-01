"""Browser playback is scoped to one YouTube player; no real browser or audio is used."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.config import Settings
from backend.context import DeviceContext, RequestContext, request_scope
from backend.media import youtube_video
from backend.media.youtube_video import SafariYouTube, video_key, video_players
from backend.tools import build_registry
from backend.tools.media import MediaService, requested_playback_target
from backend.tools.registry import ToolError, user_text


def page(state="playing", window=0, main="1", key="video123", language="ru"):
    root = [window]
    button = (
        'Для активации кнопки "Пауза" нажмите k'
        if state == "playing"
        else 'Для активации кнопки "Смотреть" нажмите k'
    )
    if language == "en":
        button = "Pause (k)" if state == "playing" else "Play (k)"
    return [
        {"path": root, "role": "AXDialog", "main": main},  # fullscreen Safari
        {"path": root + [0], "role": "AXWebArea", "url": f"https://www.youtube.com/watch?v={key}"},
        {"path": root + [0, 0], "role": "AXGroup", "description": "Проигрыватель YouTube"},
        {"path": root + [0, 0, 0], "role": "AXButton", "title": button, "enabled": "1"},
        # A recommendation outside the player must never become the target.
        {"path": root + [0, 1], "role": "AXButton", "title": "Воспроизвести", "enabled": "1"},
    ]


class FakeSafari(SafariYouTube):
    def __init__(self, state="playing", changes=True):
        super().__init__(Settings())
        self.state, self.changes = state, changes
        self.presses = []

    async def request(self, op, **kwargs):
        if op == "status":
            return {"running": True, "trusted": True, "frontmost": True}
        if op == "snapshot":
            return {"nodes": page(self.state)}
        assert op == "press"
        self.presses.append(kwargs)
        if self.changes:
            self.state = "paused" if self.state == "playing" else "playing"
        return {"ok": True}


@pytest.mark.parametrize(
    "url",
    [
        "https://music.youtube.com/watch?v=video123",
        "https://youtube.com.evil.test/watch?v=video123",
        "https://www.youtube.com@evil.test/watch?v=video123",
        "https://evil.test/watch?v=video123",
        "http://youtube.com/watch?v=video123",
        "https://www.youtube.com/",
    ],
)
def test_only_real_youtube_video_pages_are_eligible(url):
    assert video_key(url) is None


@pytest.mark.parametrize("language", ["ru", "en"])
def test_finds_only_the_player_button_including_fullscreen(language):
    players = video_players(page(language=language))
    assert len(players) == 1
    assert players[0].main and players[0].state == "playing"
    assert players[0].button["path"] == [0, 0, 0, 0]


@pytest.mark.parametrize(
    "name,state",
    [
        ("Пауза (k)", "playing"),
        ("Воспроизвести (k)", "paused"),
        ('Для активации кнопки "Пауза" нажмите k', "playing"),
        ('Для активации кнопки "Смотреть" нажмите k', "paused"),
    ],
)
def test_both_youtube_label_forms_after_a_real_press(name, state):
    nodes = page()
    nodes[3]["title"] = name
    assert video_players(nodes)[0].state == state


def test_pause_resume_are_verified_and_repeated_requests_never_toggle_back():
    async def scenario():
        app = FakeSafari()
        assert (await app.control("pause"))["state"] == "paused"
        assert (await app.control("pause"))["changed"] is False
        assert (await app.control("resume"))["state"] == "playing"
        assert (await app.control("resume"))["changed"] is False
        assert len(app.presses) == 2
        assert app.presses[0]["scope"] == {
            "path": [0, 0],
            "expected": {"role": "AXWebArea", "url": "https://www.youtube.com/watch?v=video123"},
        }

    asyncio.run(scenario())


def test_never_reports_success_when_the_player_did_not_change(monkeypatch):
    times = iter([0, 4])
    monkeypatch.setattr(youtube_video, "time", SimpleNamespace(monotonic=lambda: next(times)))
    app = FakeSafari(changes=False)
    with pytest.raises(ToolError, match="не подтвердил"):
        asyncio.run(app.control("pause"))
    assert len(app.presses) == 1  # no second toggle as a retry


def test_main_window_wins_and_multiple_ambiguous_players_are_not_clicked():
    app = FakeSafari()
    players = video_players(page(main="0") + page(window=1, key="other", main="1"))
    assert app.choose(players).key == "other"
    for player in players:
        player.main = False
    with pytest.raises(ToolError, match="несколько роликов"):
        app.choose(players)
    assert app.presses == []


def test_non_youtube_or_unavailable_controls_are_not_clicked():
    app = FakeSafari()
    nodes = page()
    nodes[1]["url"] = "https://example.com/watch?v=video123"
    with pytest.raises(ToolError, match="активной вкладке"):
        app.choose(video_players(nodes))
    nodes = page()
    nodes[3]["enabled"] = "0"
    with pytest.raises(ToolError, match="Кнопка плеера"):
        app.choose(video_players(nodes))
    assert app.presses == []


@pytest.mark.parametrize(
    "said,expected",
    [
        ("Поставь видео на паузу", "video"),
        ("Продолжи ролик в Safari", "video"),
        ("Включи обратно ютуб", "video"),
        ("Пауза YouTube Music", "music"),
        ("Поставь музон на паузу", "music"),
        ("Пауза", None),
    ],
)
def test_explicit_human_target(said, expected):
    assert requested_playback_target(said) == expected


def service():
    s = Settings()
    s.media.provider = "youtube_music"
    video = FakeSafari()
    media = MediaService(s, video=video)
    media.youtube.control = AsyncMock(return_value={"ok": True, "provider": "youtube_music"})
    return s, media


def test_human_words_override_wrong_model_target_without_touching_music():
    async def scenario():
        _, media = service()
        token = user_text.set("Поставь видео в Safari на паузу")
        try:
            result = await media.media_control("pause", target="music")
            assert result["provider"] == "youtube_video"
            media.youtube.control.assert_not_awaited()
        finally:
            user_text.reset(token)

    asyncio.run(scenario())


def test_music_command_never_changes_safari_even_with_wrong_model_target():
    async def scenario():
        _, media = service()
        token = user_text.set("Поставь музыку на паузу")
        try:
            await media.media_control("pause", target="video")
            media.youtube.control.assert_awaited_once_with("pause")
            assert media.video.presses == []
        finally:
            user_text.reset(token)

    asyncio.run(scenario())


def test_video_request_cannot_accidentally_start_the_default_music_playlist():
    async def scenario():
        _, media = service()
        media.youtube.play = AsyncMock()
        token = user_text.set("Продолжи видео в Safari")
        try:
            with pytest.raises(ToolError, match="target=video"):
                await media.play_music()
            media.youtube.play.assert_not_awaited()
        finally:
            user_text.reset(token)

    asyncio.run(scenario())


def test_auto_resume_remembers_the_video_target_when_safari_loses_focus():
    async def scenario():
        _, media = service()
        token = user_text.set("Пауза")
        try:
            await media.media_control("pause")
            media.video.has_active_video = AsyncMock(return_value=False)
            user_text.set("Продолжи")
            result = await media.media_control("resume")
            assert result["provider"] == "youtube_video" and result["state"] == "playing"
            media.youtube.control.assert_not_awaited()
        finally:
            user_text.reset(token)

    asyncio.run(scenario())


def test_existing_registry_exposes_video_target_and_keeps_music_search_arguments():
    async def scenario():
        s, media = service()
        s.tools.enabled = ["media"]
        media.youtube.play = AsyncMock(return_value={"ok": True})
        registry = build_registry(s, media=media)
        control = next(t for t in registry.schemas() if t["function"]["name"] == "media_control")
        assert "video" in control["function"]["parameters"]["properties"]["target"]["enum"]
        with request_scope(RequestContext(DeviceContext.local_mac(), "Включи альбом AM")):
            await registry.call(
                "play_music", {"query": "AM", "kind": "album", "item_id": "OLAKone"}
            )
        media.youtube.play.assert_awaited_once_with("AM", kind="album", item_id="OLAKone")
        assert media._last_control_target == "music"

    asyncio.run(scenario())
