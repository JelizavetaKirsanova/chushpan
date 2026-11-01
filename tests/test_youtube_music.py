"""No real app, network, audio or user library is touched by these tests."""

import asyncio
import json
import plistlib
from unittest.mock import AsyncMock

import pytest

from backend.config import Settings
from backend.context import DeviceContext, RequestContext, request_scope
from backend.media.safari import SafariMusic, player_state, same_page, validate_url
from backend.media.youtube import (
    MusicAPI,
    YouTubeMusic,
    candidates,
    music_url,
    normal,
    scenario_key,
)
from backend.memory.store import MemoryStore
from backend.state import State
from backend.tools import build_registry
from backend.tools.media import MediaService
from backend.tools.registry import ToolError, user_text


@pytest.fixture
def settings():
    s = Settings()
    s.media.provider = "youtube_music"
    s.media.youtube_music.auth_file = None
    s.media.youtube_music.main_playlist.id = ""
    s.media.youtube_music.main_playlist.title = ""
    return s


def run(coro):
    return asyncio.run(coro)


def node(path, role, title="", **extra):
    return {"path": path, "role": role, "title": title, "enabled": "1", **extra}


def player(playing=False, position="0:01 / 3:20"):
    return [
        node([0], "AXWebArea", url="https://music.youtube.com/watch?v=video123&list=PLone"),
        node([0, 0], "AXToolbar", "Панель проигрывателя"),
        node([0, 0, 0], "AXButton", "Приостановить" if playing else "Воспроизвести"),
        node([0, 0, 1], "AXHeading", "Song"),
        node([0, 0, 2], "AXLink", "Artist", url="https://music.youtube.com/channel/UCartist"),
        node([0, 0, 3], "AXStaticText", value=position),
        node([0, 0, 4], "AXSlider", "Громкость", value="73"),
    ]


class FakeApp:
    def __init__(self):
        self.plays = []
        self.navigated = []
        self.changed_volume = []

    async def launch(self):
        return {"ok": True}

    async def library_page(self):
        return await self.navigate("https://music.youtube.com/library/playlists")

    async def navigate(self, url):
        self.navigated.append(url)
        return [
            node([0], "AXWebArea", url=url),
            node(
                [0, 0],
                "AXLink",
                "Favourite Songs",
                url="https://music.youtube.com/playlist?list=PLone",
            ),
            node([0, 1], "AXLink", "My music", url="https://music.youtube.com/playlist?list=PLtwo"),
        ]

    async def snapshot(self):
        return player()

    async def play_page(self, url, title="", video=False):
        self.plays.append((url, title, video))
        return {"ok": True, "state": "playing", "track": title or "Song"}

    async def now_playing(self):
        return {"state": "paused", "volume": 73}

    async def set_volume(self, level):
        self.changed_volume.append(level)
        return {"ok": True, "volume": level}

    async def control(self, action):
        return {"ok": True, "action": action}


class FakeAPI:
    def __init__(self):
        self.calls = []

    async def call(self, method, *args, **kw):
        self.calls.append((method, args, kw))
        if method == "get_library_playlists":
            return [{"playlistId": "PLone", "title": "Favourite Songs"}]
        if method == "get_album":
            return {"audioPlaylistId": "OLAKalbum"}
        if method == "get_artist":
            return {"radioId": "RDEMartist"}
        if method == "get_playlist":
            return {
                "title": "Favourite Songs",
                "tracks": [
                    {"videoId": "video123", "title": "Song", "artists": [{"name": "Artist"}]}
                ],
            }
        kind = kw["filter"]
        return [
            {
                "videoId": "video123",
                "browseId": "UCartist" if kind == "artists" else "MPREalbum",
                "title": "Artist" if kind == "artists" else "Song",
                "artists": [{"name": "Artist"}],
            }
        ]


@pytest.fixture
def service(settings):
    return YouTubeMusic(settings, app=FakeApp(), api=FakeAPI())


@pytest.mark.parametrize(
    "kind,item_id,path",
    [
        ("song", "abc_DEF-123", "/watch?v=abc_DEF-123"),
        ("playlist", "PLone", "/playlist?list=PLone"),
        ("album", "MPREalbum", "/browse/MPREalbum"),
        ("artist", "UCartist", "/channel/UCartist"),
    ],
)
def test_links(kind, item_id, path):
    assert music_url(kind, item_id) == "https://music.youtube.com" + path


@pytest.mark.parametrize("item_id", ["a&list=other", "../../app", "x;open spotify", "", "a b"])
def test_rejects_ids_that_change_url_or_inject_commands(item_id):
    with pytest.raises(ToolError):
        music_url("song", item_id)


@pytest.mark.parametrize(
    "url",
    [
        "http://music.youtube.com/",
        "https://evil.test/",
        "https://music.youtube.com@evil.test/",
        "file:///tmp/x",
    ],
)
def test_navigation_allowlist(url):
    with pytest.raises(ToolError):
        validate_url(url)


def test_navigation_confirmation_checks_id():
    assert same_page(
        "https://music.youtube.com/playlist?list=PLone&x=1",
        "https://music.youtube.com/playlist?list=PLone",
    )
    assert not same_page(
        "https://music.youtube.com/playlist?list=PLtwo",
        "https://music.youtube.com/playlist?list=PLone",
    )


def test_native_navigation_prefers_link_over_menu_and_avoids_reload(settings, monkeypatch):
    import backend.media.safari as safari

    app = SafariMusic(settings)
    app.discover = lambda: (settings.data_path, "test.bundle")
    app.request = AsyncMock(return_value={"running": True})
    app.act = AsyncMock()
    target = "https://music.youtube.com/playlist?list=PLone"
    app.snapshot = AsyncMock(
        side_effect=[
            [
                node([0], "AXWebArea", url="https://music.youtube.com/library"),
                node([0, 0], "AXLink", "Меню действий", url=target),
                node([0, 1], "AXLink", "Favourite Songs", url=target),
            ],
            [node([0], "AXWebArea", url=target)],
        ]
    )
    process = AsyncMock()
    monkeypatch.setattr(safari, "process", process)
    assert run(app.navigate(target))[0]["url"] == target
    assert app.act.call_args.args[0]["title"] == "Favourite Songs"
    process.assert_not_awaited()


@pytest.mark.parametrize("filled", [False, True])
def test_native_search_uses_placeholder_and_targets_only_search_field(settings, filled):
    app = SafariMusic(settings)
    app.launch = AsyncMock()
    app.request = AsyncMock(return_value={"ok": True})
    field = node([0, 0], "AXComboBox")
    initial = [node([0], "AXWebArea"), field]
    if not filled:
        initial.append(node([0, 0, 0], "AXStaticText", value="Поиск треков и исполнителей"))
    else:
        field["value"] = "previous query"
    app.snapshot = AsyncMock(
        side_effect=[
            initial,
            [node([0], "AXWebArea", url="https://music.youtube.com/search?q=AC%2FDC")],
        ]
    )
    run(app.search("AC/DC"))
    assert app.request.call_args.args == ("search",)
    assert app.request.call_args.kwargs["query"] == "AC/DC"
    assert app.request.call_args.kwargs["path"] == field["path"]


def test_favorite_spelling_and_stress():
    assert normal("Favourite Songs") == normal("Favorite Songs")
    assert normal("Чушпа́н") == normal("Чушпан")


def test_duplicate_titles_and_stt_typos_require_choice():
    items = [{"id": "one", "title": "Songs"}, {"id": "two", "title": "Songs"}]
    assert candidates("Songs", items)["status"] == "choose"
    assert candidates("Sogns", items)["status"] == "choose"
    assert candidates("one", items)["item"]["id"] == "one"


@pytest.mark.parametrize(
    "kind,filter_name", [("song", "songs"), ("artist", "artists"), ("album", "albums")]
)
def test_search_kinds(service, kind, filter_name):
    result = run(service.search("Artist" if kind == "artist" else "Song", kind))
    assert result["item"]["kind"] == kind
    assert service.api.calls[-1][2]["filter"] == filter_name
    assert not service.app.plays


def test_library_uses_signed_in_app_without_reading_credentials(service):
    data = run(service.library())
    assert [p["title"] for p in data["playlists"]] == ["Favourite Songs", "My music"]
    assert data["source"] == "safari_accessibility" and not data["complete"]
    assert service.api.calls == []
    assert run(service.search("Favorite Songs", "playlist"))["item"]["id"] == "PLone"


def test_library_optional_authenticated_api(service):
    service.s.media.youtube_music.auth_file = "private/headers.json"
    assert run(service.library())["source"] == "ytmusicapi"
    assert service.api.calls[0][0] == "get_library_playlists"


def test_api_missing_credentials_does_not_expose_secrets(settings):
    settings.media.youtube_music.auth_file = settings.data_path / "missing.json"
    with pytest.raises(ToolError, match="Файл авторизации"):
        run(MusicAPI(settings).call("get_library_playlists"))


def test_tracks(service):
    result = run(service.playlist("tracks", playlist_id="PLone"))
    assert result["tracks"] == [{"id": "video123", "title": "Song", "artist": "Artist"}]


def test_playlist_assignment_requires_human_request_and_persists(service, settings):
    token = user_text.set("Какие у меня плейлисты?")
    try:
        with pytest.raises(ToolError, match="явной просьбе"):
            run(service.playlist("assign", "Favourite Songs"))
        user_text.set("Сделай Favorite Songs моим основным плейлистом")
        result = run(service.playlist("assign", "Favorite Songs"))
        assert result["playlist"]["id"] == "PLone"
    finally:
        user_text.reset(token)
    recreated = YouTubeMusic(settings, app=FakeApp(), api=FakeAPI())
    assert run(recreated.playlist("get"))["playlist"]["id"] == "PLone"


def test_scenarios_share_existing_memory_database(service, settings):
    token = user_text.set("Запомни, этот плейлист для учёбы")
    try:
        run(service.playlist("assign", "My music", situation="для учёбы"))
    finally:
        user_text.reset(token)
    with_store = MemoryStore(settings.db_path)
    assert with_store.preference("youtube_music", "study")["id"] == "PLtwo"
    assert with_store.facts() == []  # preferences do not invent biographical facts
    with_store.close()
    assert scenario_key("музыка для работы") == "work"
    assert run(service.play(situation="study"))["ok"]
    assert service.app.plays[0][0].endswith("list=PLtwo")


def test_first_setup_does_not_choose_playlist(service):
    result = run(service.play())
    assert result["status"] == "choose" and len(result["playlists"]) == 2
    assert not service.app.plays and service.preference("default") is None


def test_main_playlist_uses_id_and_preserves_volume(service):
    service.preference("default", {"id": "PLone", "title": "Favourite Songs"})
    assert run(service.play("мою музыку"))["ok"]
    assert service.app.plays[0][0].endswith("list=PLone")
    assert not service.app.changed_volume


def test_llm_cannot_invent_scenario_from_time_of_day(service):
    service.preference("default", {"id": "PLone", "title": "Favourite Songs"})
    token = user_text.set("Включи мою музыку")
    try:
        assert run(service.play("мою музыку", situation="rest"))["ok"]
    finally:
        user_text.reset(token)
    assert service.app.plays[-1][0].endswith("list=PLone")


def test_removed_default_does_not_play_other_playlist(service):
    service.preference("default", {"id": "PLremoved", "title": "Old"})
    assert run(service.play())["status"] == "unavailable"
    assert not service.app.plays


def test_play_song_album_artist_and_open_only(service):
    run(service.play("Song Artist", "song"))
    assert service.app.plays[-1] == ("https://music.youtube.com/watch?v=video123", "Song", True)
    run(service.play("Song", "album"))
    assert service.app.plays[-1][0].endswith("list=OLAKalbum")
    run(service.play("Artist", "artist"))
    assert service.app.plays[-1][:2] == ("https://music.youtube.com/channel/UCartist", "Artist")
    count = len(service.app.plays)
    assert run(service.play("Favourite Songs", "playlist", open_only=True))["action"] == "opened"
    assert len(service.app.plays) == count


def test_volume_changes_only_on_explicit_tool_call(service):
    assert run(service.volume("down"))["volume"] == 63
    assert run(service.volume(level=30))["volume"] == 30
    with pytest.raises(ToolError):
        run(service.volume())


def test_music_volume_request_cannot_mute_mac_or_guess_zero(service, monkeypatch):
    from backend.tools import system

    system_run = AsyncMock()
    monkeypatch.setattr(system, "run", system_run)
    token = user_text.set("Сделай музыку потише")
    try:
        with pytest.raises(ToolError, match="media_volume"):
            run(system.set_volume(mute=True))
        system_run.assert_not_awaited()
        assert run(service.volume(level=0))["volume"] == 63
    finally:
        user_text.reset(token)


def test_wrong_llm_volume_tool_is_routed_to_music(settings, monkeypatch):
    volume = AsyncMock(return_value={"ok": True, "volume": 63})
    monkeypatch.setattr(MediaService, "media_volume", volume)
    reg = build_registry(settings)
    token = user_text.set("Сделай музыку потише")
    try:
        with request_scope(RequestContext(DeviceContext.local_mac(), "Сделай музыку потише")):
            assert json.loads(run(reg.call("set_volume", {"mute": True})))["volume"] == 63
    finally:
        user_text.reset(token)
    volume.assert_awaited_once_with(direction="down", level=None)


def test_routing_and_llm_registration(settings):
    store = MemoryStore(settings.db_path)
    reg = build_registry(settings, store=store)
    assert {"play_music", "music_search", "music_playlist", "media_control", "media_volume"} <= set(
        reg.names
    )
    assert "YouTube Music" in " ".join(reg.capabilities("ru"))
    assert reg.is_slow("play_music") and not reg.is_quiet("play_music")
    settings.media.provider = "spotify"
    assert "music_playlist" not in build_registry(settings, store=store).names
    settings.media.provider = "youtube_music"
    settings.media.youtube_music.enabled = False
    assert "play_music" not in build_registry(settings, store=store).names
    store.close()


def test_youtube_voice_state_does_not_duck_spotify(settings):
    media = MediaService(settings)
    media.ducker.on_state = lambda *_: pytest.fail("must not touch Spotify")
    media.on_state(State.SLEEPING, State.LISTENING)


def test_player_handles_real_russian_pause_label():
    assert player_state(player(True))["state"] == "playing"
    assert player_state(player(False))["state"] == "paused"
    assert player_state(player(True))["artist"] == "Artist"
    assert player_state([])["state"] == "unknown"


def test_empty_api_falls_back_to_app_search(service):
    service.api.call = AsyncMock(return_value=[])
    service.app.search = AsyncMock(
        return_value=[
            node([0], "AXWebArea", url="https://music.youtube.com/search?q=Artist"),
            node([0, 0], "AXLink", "Artist", url="https://music.youtube.com/channel/UCartist"),
        ]
    )
    result = run(service.search("Artist", "artist"))
    assert result["source"] == "safari_accessibility"
    assert result["item"]["id"] == "UCartist"


def test_failed_api_auth_does_not_leak_headers(settings, monkeypatch):
    auth = settings.data_path / "fake-auth.json"
    auth.write_text('{"Cookie": "private-test-secret"}')
    settings.media.youtube_music.auth_file = auth

    def rejected(*args, **kwargs):
        raise RuntimeError("Authorization: private-test-secret")

    monkeypatch.setattr("ytmusicapi.YTMusic", rejected)
    with pytest.raises(ToolError) as error:
        run(MusicAPI(settings).call("search", "test"))
    assert "private-test-secret" not in str(error.value)


def test_assignment_query_does_not_change_default(service):
    token = user_text.set("Какой у меня основной плейлист?")
    try:
        with pytest.raises(ToolError):
            run(service.playlist("assign", "My music"))
    finally:
        user_text.reset(token)
    assert service.preference("default") is None


def test_selected_song_id_cannot_trigger_default_playlist(service):
    with pytest.raises(ToolError, match="Назови"):
        run(service.play(kind="song", item_id="video123"))
    assert not service.app.plays


def test_repeat_is_verified_and_unsupported_controls_do_not_mutate(settings, monkeypatch):
    app = SafariMusic(settings)
    app.snapshot = AsyncMock(
        side_effect=[
            player(False),
            [
                node([0], "AXToolbar", "Панель проигрывателя"),
                node([0, 0], "AXButton", "Повтор отключен"),
            ],
            [
                node([0], "AXToolbar", "Панель проигрывателя"),
                node([0, 0], "AXButton", "Повторять все треки"),
            ],
        ]
    )
    app.act = AsyncMock()
    monkeypatch.setattr("backend.media.safari.asyncio.sleep", AsyncMock())
    assert run(app.control("repeat_on")) == {"ok": True, "repeat": "all"}
    app.act.assert_awaited_once()
    app.snapshot = AsyncMock(return_value=player(False))
    app.act.reset_mock()
    with pytest.raises(ToolError, match="перемешивания"):
        run(app.control("shuffle_on"))
    with pytest.raises(ToolError, match="Перемотка"):
        run(app.seek(20))
    app.act.assert_not_awaited()


def test_like_needs_explicit_request_and_verifies_checkbox(settings):
    app = SafariMusic(settings)
    base = player(False)
    unchecked = base + [node([0, 0, 5], "AXCheckBox", "Нравится", value="0")]
    checked = base + [node([0, 0, 5], "AXCheckBox", "Нравится", value="1")]
    app.snapshot = AsyncMock(return_value=unchecked)
    app.act = AsyncMock()
    token = user_text.set("Что играет?")
    try:
        with pytest.raises(ToolError, match="прямой просьбе"):
            run(app.control("like"))
        app.act.assert_not_awaited()
        user_text.set("Поставь лайк этой песне")
        app.snapshot = AsyncMock(side_effect=[unchecked, checked])
        assert run(app.control("like"))["liked"]
        app.act.assert_awaited_once()
    finally:
        user_text.reset(token)


def test_playback_requires_progress_not_just_button(settings, monkeypatch):
    app = SafariMusic(settings)
    app.config.timeout_s = 0.01
    app.now_playing = AsyncMock(
        return_value={"state": "playing", "track": "Song", "position": "0:00"}
    )
    monkeypatch.setattr("backend.media.safari.asyncio.sleep", AsyncMock())
    with pytest.raises(ToolError, match="не подтверждено"):
        run(app.wait_playing())
    app.config.timeout_s = 1
    app.now_playing = AsyncMock(
        side_effect=[
            {"state": "playing", "track": "Song", "position": "0:00"},
            {"state": "playing", "track": "Song", "position": "0:01"},
        ]
    )
    assert run(app.wait_playing())["ok"]


def test_pause_and_volume_confirm_state(settings):
    app = SafariMusic(settings)
    app.snapshot = AsyncMock(return_value=player(True))
    app.act = AsyncMock()
    app.now_playing = AsyncMock(return_value={"state": "paused", "volume": 30})
    assert run(app.control("pause"))["ok"]
    assert app.act.call_args.args[0]["title"] == "Приостановить"
    assert run(app.set_volume(30))["ok"]
    app.now_playing.return_value = {"state": "playing", "volume": 73}
    with pytest.raises(ToolError, match="не подтвержден"):
        run(app.control("pause"))
    with pytest.raises(ToolError, match="не подтвержден"):
        run(app.set_volume(30))


def test_native_permission_error_is_explained(settings):
    app = SafariMusic(settings)
    app.discover = lambda: (settings.data_path, "com.apple.Safari.WebApp.test")
    app._helper = AsyncMock(return_value="fake-native-helper")
    import backend.media.safari as safari

    original = safari.process
    try:
        safari.process = AsyncMock(return_value=json.dumps({"error": "accessibility_permission"}))
        with pytest.raises(ToolError, match="Универсальный доступ"):
            run(app.request("snapshot"))
    finally:
        safari.process = original


def test_installed_app_discovery_validates_manifest(settings, tmp_path, monkeypatch):
    app_path = tmp_path / "Music.app"
    (app_path / "Contents").mkdir(parents=True)
    info = {
        "CFBundleIdentifier": "com.apple.Safari.WebApp.test",
        "Manifest": {"start_url": "https://music.youtube.com/"},
    }
    (app_path / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    settings.media.youtube_music.app_path = app_path
    settings.media.youtube_music.bundle_id = "com.apple.Safari.WebApp.test"
    assert SafariMusic(settings).discover() == (app_path, info["CFBundleIdentifier"])
    settings.media.youtube_music.bundle_id = "com.apple.Safari.WebApp.missing-test-only"
    with pytest.raises(ToolError, match="Не найден"):
        SafariMusic(settings).discover()
