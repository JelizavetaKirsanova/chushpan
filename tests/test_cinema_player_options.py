import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.cinema.learned_player import LearnedPlayer, voice_option
from backend.cinema.player_options import QualityController, preferred_voice, quality_height
from backend.tools.registry import ToolError


@pytest.mark.parametrize(
    "current,choices,language,playing,expected",
    [
        ("Eng. Orig.", ["Eng. Orig.", "LostFilm", "NewStudio"], "en", False, "LostFilm"),
        ("Original", ["Original"], "ru", True, "Original"),
        ("Рус. Оригинальный", ["Рус. Оригинальный", "LostFilm"], "ru", True, "Рус. Оригинальный"),
        ("NewStudio", ["NewStudio", "LostFilm"], "en", True, "NewStudio"),
        ("Eng. Orig.", ["Eng. Orig.", "Рус. Дублированный"], "en", False, "Рус. Дублированный"),
        ("", [], "ru", False, ""),
    ],
)
def test_voice_priority_preserves_suitable_playing_track(
    current, choices, language, playing, expected
):
    assert (
        preferred_voice(current, choices, original_language=language, playing=playing) == expected
    )


def test_resolution_and_speed_are_never_audio_tracks():
    tree = [
        {"path": [0, 1, i], "role": "AXStaticText", "value": value}
        for i, value in enumerate(["1 сезон", "1 серия", "1080p", "Auto", "1.5", "Качество"])
    ]
    assert voice_option(tree) is None
    assert quality_height("1080p") == 1080
    assert quality_height("1920x1080") == 0
    assert quality_height("Auto") == 0


class MenuPlayer:
    def __init__(self, options, *, fail_max=False, reload=False, menu=True):
        self.options = options
        self.selected = options[0]
        self.screen = "closed"
        self.requested = []
        self.position = 120
        self.fail_max, self.reload, self.has_menu = fail_max, reload, menu
        self.last_sample = None
        self.seeks = []

    async def reveal(self):
        pass

    async def action_button(self, nodes, pattern):
        return {"role": "AXButton", "title": "Настройки"} if self.has_menu else None

    async def press_settings(self, node):
        self.screen = "root" if self.screen == "closed" else "closed"

    async def nodes(self):
        values = (
            [self.selected]
            if self.screen == "root"
            else self.options
            if self.screen == "options"
            else []
        )
        return [{"role": "AXStaticText", "value": value} for value in ["Качество", *values]]

    async def sample(self):
        return {"position": self.position, "duration": 2000, "paused": False}

    async def press(self, node):
        if self.screen == "root":
            self.screen = "options"
        else:
            self.selected = node["value"]
            self.requested.append(self.selected)
            self.screen = "root"
            if self.reload:
                self.position = 0

    async def wait_nodes(self, predicate):
        tree = await self.nodes()
        if not predicate(tree):
            raise ToolError("Menu did not confirm selection")
        return tree

    async def confirm(self, nodes):
        if self.fail_max and self.selected == self.options[-1]:
            raise ToolError("Selected stream stalled")
        self.position += 1
        return {
            "position": self.position,
            "duration": 2000,
            "paused": False,
            "playback_confirmed": True,
        }

    async def seek(self, position):
        self.seeks.append(position)
        self.position = position


@pytest.mark.parametrize(
    "values,expected", [(["480p", "720p"], "720p"), (["720p", "1080p"], "1080p")]
)
def test_max_quality_is_applied_and_time_restored_after_reload(values, expected):
    player = MenuPlayer(values, reload=True)
    result = asyncio.run(QualityController(player).select_maximum())
    assert result["selected"] == expected and result["maximum_confirmed"]
    assert result["playback_confirmed"] and player.last_sample["position"] >= 120
    assert player.seeks == [120] and player.screen == "closed"


def test_failed_max_quality_retries_once_then_restores_previous_working_option():
    player = MenuPlayer(["480p", "1080p"], fail_max=True)
    result = asyncio.run(QualityController(player).select_maximum())
    assert player.requested == ["1080p", "480p"]
    assert result["status"] == "fallback" and result["selected"] == "480p"
    assert result["playback_confirmed"] and not result["maximum_confirmed"]
    assert player.screen == "closed"


@pytest.mark.parametrize(
    "menu,values,status", [(False, ["480p"], "unavailable"), (True, ["Auto"], "auto")]
)
def test_absent_or_auto_only_quality_does_not_claim_maximum(menu, values, status):
    player = MenuPlayer(values, menu=menu)
    result = asyncio.run(QualityController(player).select_maximum())
    assert result["status"] == status and not result["maximum_confirmed"]
    assert player.requested == [] and player.screen == "closed"


def test_buffering_confirm_never_toggles_an_already_running_player():
    player = LearnedPlayer(None, None)
    player.playing = True
    player.observe_playback = AsyncMock(return_value=(None, []))
    player.action_button = AsyncMock(return_value=None)
    player.key = AsyncMock()
    with pytest.raises(ToolError) as error:
        asyncio.run(player.confirm([]))
    assert error.value.code == "buffering"
    player.key.assert_not_awaited()


@pytest.mark.parametrize("error", [ToolError("Menu unavailable"), TimeoutError()])
@pytest.mark.parametrize("failed_stage", ["", "QUALITY_SELECTED", "PLAYING_CONFIRMED"])
def test_optional_audio_discovery_cannot_discard_confirmed_page(monkeypatch, error, failed_stage):
    class Player(LearnedPlayer):
        async def open_page(self, *args):
            self.session = object()
            self.engine = "venom_ax"

        async def reveal(self, **kwargs):
            pass

        async def prepare(self, *args):
            return []

        async def nodes(self):
            return []

        async def discover_audio_tracks(self, nodes):
            raise error

        async def confirm(self, nodes):
            self.playing = True
            return {
                "position": 2,
                "duration": 1800,
                "paused": False,
                "playback_confirmed": True,
                "message": "Confirmed",
            }

    monkeypatch.setattr(
        QualityController, "select_maximum", AsyncMock(return_value={"status": "unavailable"})
    )
    player = Player(None, None)
    player.detach = AsyncMock()

    async def diagnostic(name, status, message):
        if name == failed_stage:
            raise ValueError("Invalid playback transition")

    result = asyncio.run(
        player.open(
            {
                "item": {"title": "Title"},
                "episode": {"season": 1, "episode": 1},
                "recipe": {"url": "https://example.org/title"},
                "auto_fullscreen": False,
                "diagnostic": diagnostic,
            }
        )
    )
    assert result["playback_confirmed"] and player.session is not None
    player.detach.assert_not_awaited()


def test_missing_voice_menu_skips_lostfilm_without_pressing_any_control():
    class Player(LearnedPlayer):
        async def nodes(self):
            return [
                {"path": [0, 1, i], "role": "AXStaticText", "value": value}
                for i, value in enumerate(["1 сезон", "1 серия", "1080p", "00:01 / 50:00"])
            ]

    from backend.cinema.learned_player import SiteRecipe

    player = Player(None, None)
    player.press = AsyncMock()
    asyncio.run(
        player.prepare(
            SiteRecipe(url="https://example.org/title", voice="LostFilm"),
            {"season": 1, "episode": 1},
        )
    )
    assert player.voice == "" and player.audio_tracks == []
    player.press.assert_not_awaited()


def test_broken_audio_restores_previous_track_episode_and_position():
    class Player(LearnedPlayer):
        selected = "LostFilm"
        position = 120
        selected_tracks = []
        seeks = []

        async def sample(self):
            return {"position": 120, "duration": 1800, "paused": False}

        async def page(self, **kwargs):
            return {"fullscreen": False}

        async def reveal(self, **kwargs):
            pass

        async def nodes(self):
            return [
                {"role": "AXStaticText", "path": [0, 1, i], "value": v}
                for i, v in enumerate(["1 сезон", "2 серия", self.selected])
            ]

        async def select(self, tree, menu, desired):
            self.selected = desired
            self.position = 0
            self.selected_tracks.append(desired)

        async def confirm(self, tree):
            if self.selected == "Рус. Другая":
                raise ToolError("Audio stream unavailable")
            return {
                "position": self.position + 1,
                "duration": 1800,
                "paused": False,
                "playback_confirmed": True,
            }

        async def seek(self, position):
            self.seeks.append(position)
            self.position = position

    player = Player(None, None)
    player.voice = "LostFilm"
    player.audio_tracks = ["LostFilm", "Рус. Другая"]
    player.episode = {"season": 1, "episode": 2}
    result = asyncio.run(player.set_voice("Рус. Другая"))
    assert not result["voice_confirmed"] and result["selected_voice"] == "LostFilm"
    assert result["position"] == 121 and player.seeks == [120]
    assert player.selected_tracks == ["Рус. Другая", "LostFilm"]
    assert player.episode == {"season": 1, "episode": 2}


@pytest.mark.parametrize("fullscreen_error", [False, True])
def test_iframe_recovery_stays_on_exact_url_and_invalidates_old_quality(fullscreen_error):
    player = LearnedPlayer(None, None)
    player.url = "https://example.org/show#chushpan_session=owned"
    player.frame_host = "old-player.example"
    player.page = AsyncMock(
        return_value={"frames": [{"host": "new-player.example"}], "fullscreen": True}
    )
    player.reveal = AsyncMock()
    player.nodes = AsyncMock(return_value=[])
    player.confirm = AsyncMock(
        return_value={
            "position": 150,
            "duration": 1800,
            "paused": False,
            "playback_confirmed": True,
        }
    )
    player.fullscreen = AsyncMock(
        side_effect=ToolError("No fullscreen") if fullscreen_error else None,
        return_value={"fullscreen_confirmed": True, "capabilities": {"fullscreen": True}},
    )
    player.open_page = AsyncMock()
    player.detach = AsyncMock()
    result = asyncio.run(player.recover())
    assert player.url == "https://example.org/show#chushpan_session=owned"
    assert player.frame_host == "new-player.example" and player.engine == "semantic_ax"
    assert not result["capabilities"]["quality"]["maximum_confirmed"]
    assert result["capabilities"]["fullscreen"] is not fullscreen_error
    player.open_page.assert_not_awaited()
    player.detach.assert_not_awaited()


def test_late_resume_confirmation_after_optional_failure_is_not_blocked():
    from backend.cinema.playback import PlaybackManager, PlaybackState

    manager = PlaybackManager()
    manager.transition(PlaybackState.READY)
    for name, status in [
        ("OPEN_SAFARI", "running"),
        ("DETECT_PLAYER", "success"),
        ("PLAY", "running"),
        ("PLAYING_CONFIRMED", "success"),
        ("RESTORE_POSITION", "running"),
        ("RESTORE_POSITION", "limited"),
        ("QUALITY_SELECTED", "limited"),
        ("RESTORE_POSITION", "success"),
        ("PLAYING_CONFIRMED", "success"),
        ("FULLSCREEN", "running"),
        ("FULLSCREEN_CONFIRMED", "success"),
    ]:
        manager.stage(name, status, name)
    assert manager.state == PlaybackState.ENTERING_FULLSCREEN
    assert manager.snapshot()["pipeline_phase"] == "FULLSCREEN_CONFIRMED"


@pytest.mark.parametrize("manual_exit", [False, True])
def test_late_clock_confirmation_enters_fullscreen_once_unless_user_exited(manual_exit):
    from types import SimpleNamespace

    from backend.cinema.playback import PlaybackManager, PlaybackSource
    from backend.cinema.service import Cinema

    async def run():
        adapter = SimpleNamespace(
            fullscreen=AsyncMock(
                return_value={"fullscreen_confirmed": True, "capabilities": {"fullscreen": True}}
            )
        )
        manager = PlaybackManager()
        source = PlaybackSource(
            "https://example.org/show", "title", "episode", "source", "link", "safari", "episode"
        )
        session = manager.bind(
            source,
            token="token",
            adapter=adapter,
            mode="learned",
            errors=0,
            fullscreen_pending=not manual_exit,
            target={},
        )
        holder = SimpleNamespace(
            session=session,
            closed=False,
            playback=manager,
            lock=asyncio.Lock(),
            tools=SimpleNamespace(enabled=lambda key: True),
            db=AsyncMock(return_value=SimpleNamespace(poll_seconds=0)),
            message="",
        )

        async def sample(event):
            proof = {"position": 10, "duration": 1800, "paused": False, "playback_confirmed": True}
            manager.confirmed(proof)
            return proof

        async def publish():
            holder.closed = True

        holder.sample = sample
        holder.publish = publish
        await Cinema.poll(holder)
        assert source.url == "https://example.org/show" and not session["fullscreen_pending"]
        if manual_exit:
            adapter.fullscreen.assert_not_awaited()
        else:
            adapter.fullscreen.assert_awaited_once()
            assert source.capabilities["fullscreen"]

    asyncio.run(run())


def test_initial_unlabelled_play_is_not_toggled_twice_while_loading():
    player = LearnedPlayer(None, None)
    player.engine = "venom_ax"
    player.observe_playback = AsyncMock(return_value=(None, []))
    player.action_button = AsyncMock(return_value=None)
    player.nodes = AsyncMock(return_value=[])
    player.key = AsyncMock()
    with pytest.raises(ToolError):
        asyncio.run(player.confirm([]))
    player.key.assert_awaited_once_with("play_pause")
