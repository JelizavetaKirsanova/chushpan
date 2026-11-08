import asyncio

import pytest

from backend.cinema.learned_player import LearnedPlayer, SiteRecipe, player_state, title_agrees
from backend.cinema.store import CinemaStore


def nodes(*labels):
    return [
        {"role": "AXButton" if label.startswith("Пауза") else "AXStaticText", "value": label}
        for label in labels
    ]


def test_player_requires_one_full_timer_with_matching_episode_and_voice():
    player = LearnedPlayer(None, None)
    player.episode = {"season": 1, "episode": 2}
    player.voice = "LostFilm"
    player.minimum = 1200
    good = nodes("1 сезон", "2 серия", "LostFilm", "00:03 / 58:17", "Пауза (пробел)")
    assert player.verified_state(good)["position"] == 3
    assert player.verified_state(good)["paused"] is False
    assert not player.verified_state(nodes("1 сезон", "1 серия", "LostFilm", "00:03 / 58:17"))
    assert not player.verified_state(nodes("1 сезон", "2 серия", "LostFilm", "00:03 / 01:17"))
    assert not player.verified_state(nodes("1 сезон", "2 серия", "Other", "00:03 / 58:17"))
    assert player_state(good + nodes("00:01 / 00:20")) is None
    assert title_agrees("Последнее королевство (1-5 сезон)", {"title": "Последнее королевство"})
    assert not title_agrees("Другое королевство (1-5 сезон)", {"title": "Последнее королевство"})


def test_prepare_waits_for_episode_controls_after_iframe_skeleton():
    class DynamicFrame:
        calls = 0
        operations = []

        async def cinema(self, op, **kwargs):
            self.calls += 1
            self.operations.append(op)
            loading = [{"role": "AXGroup", "path": [i]} for i in range(4)]
            ready = [
                {**node, "path": [i]}
                for i, node in enumerate(nodes("1 сезон", "1 серия", "LostFilm"))
            ]
            return {"nodes": loading if self.calls == 1 else ready}

    async def run():
        ax = DynamicFrame()
        player = LearnedPlayer(None, ax)
        player.frame_host = "player.example"
        recipe = SiteRecipe(url="https://some.example/show", voice="LostFilm")
        result = await player.prepare(recipe, {"season": 1, "episode": 1})
        assert ax.operations[:2] == ["cinema_snapshot", "cinema_snapshot"]
        assert result[0]["value"] == "1 сезон"

    asyncio.run(run())


def test_taught_recipe_uses_public_https_and_is_versioned_without_resetting_progress(tmp_path):
    with pytest.raises(ValueError):
        SiteRecipe(url="https://some.example/show#chushpan_session=other")
    with pytest.raises(ValueError):
        SiteRecipe(url="https://local/")
    with pytest.raises(ValueError):
        SiteRecipe(url="https://user:password@some.example/show")

    store = CinemaStore(tmp_path / "library.sqlite")
    item = store.add({"title": "Последнее королевство", "season": 1, "episode": 1})
    ep = item["current"]
    store.progress(ep["id"], 37, 3500)
    recipe = SiteRecipe(url="https://some.example/show", player="Плеер 1", voice="LostFilm")
    first = store.save_site_recipe(item, ep, recipe.model_dump(), "player.example")
    second = store.save_site_recipe(item, ep, recipe.model_dump(), "player.example")
    assert first == second
    result = store.detail(item["id"])
    assert result["site_recipe"]["episode"] == 1
    assert result["site_recipe"]["voice"] == "LostFilm"
    assert result["current"]["position"] == 37
    assert result["current"]["links"][0]["id"] == first
    store.close()


@pytest.mark.parametrize("auto_fullscreen", [True, False])
def test_resume_precedes_fullscreen_and_unverified_fullscreen_is_reported(auto_fullscreen):
    actions = []

    class Player(LearnedPlayer):
        async def open_page(self, *args):
            self.engine = "venom_ax"

        async def reveal(self):
            pass

        async def prepare(self, *args):
            return []

        async def nodes(self):
            return []

        async def confirm(self, nodes):
            actions.append("confirm")
            return {"position": 100 if "seek" in actions else 1, "message": "Playing"}

        async def seek(self, position):
            assert position == 100
            actions.append("seek")
            return {"seek_confirmed": True, "message": "Restored"}

        async def fullscreen(self):
            actions.append("fullscreen")
            return {"fullscreen_confirmed": False}

    async def diagnostic(stage, status, message):
        actions.append((stage, status))

    result = asyncio.run(
        Player(None, None).open(
            {
                "recipe": {"url": "https://example.org/title"},
                "item": {"title": "Title"},
                "episode": {"season": 1, "position": 100},
                "auto_fullscreen": auto_fullscreen,
                "diagnostic": diagnostic,
            }
        )
    )
    assert result["resume_confirmed"] is True
    assert [action for action in actions if isinstance(action, str)] == [
        "confirm",
        "seek",
        "confirm",
        *(["fullscreen"] if auto_fullscreen else []),
    ]
    if auto_fullscreen:
        assert result["fullscreen_confirmed"] is False
        assert ("FULLSCREEN", "limited") in actions
    else:
        assert "fullscreen_confirmed" not in result


def test_fullscreen_controls_preserve_safari_video_window():
    from unittest.mock import AsyncMock

    async def run():
        tabs = type("Tabs", (), {"_owned": AsyncMock(return_value="sent")})()
        ax = type("AX", (), {"cinema": AsyncMock(return_value={"frontmost": True})})()
        player = LearnedPlayer(None, ax, tabs=tabs)
        player.page = AsyncMock(return_value={"fullscreen": True})
        await player.activate()
        action = tabs._owned.call_args.args[1]
        assert "activate" in action
        assert "current tab" not in action and "set index" not in action
        player.page.return_value = {"fullscreen": False}
        await player.activate()
        assert "set current tab" in tabs._owned.call_args.args[1]

    asyncio.run(run())


def test_site_autonext_requires_countdown_adjacent_episode_voice_and_advancing_clock():
    player = LearnedPlayer(None, None)
    player.engine = "venom_ax"
    player.episode = {"season": 1, "episode": 1}
    player.voice = "LostFilm"
    player.minimum = 720
    player.report = {"heading": "Title (1-5 сезон)"}
    old = nodes(
        "1 сезон",
        "1 серия",
        "LostFilm",
        "58:59 / 59:02",
        "Title (1 сезон) - 2 серия",
        "Следующая серия запустится через",
    )
    player.last_sample = player.verified_state(old)
    assert player.last_sample["position"] == 3539
    wrong = nodes("1 сезон", "3 серия", "LostFilm", "00:01 / 58:42")
    assert player.site_advance(wrong) is None
    assert player.site_advance(nodes("1 сезон", "2 серия", "Other", "00:01 / 58:42")) is None
    first = player.site_advance(nodes("1 сезон", "2 серия", "LostFilm", "00:01 / 58:42"))
    assert first["observation_stale"] and "site_next_episode" not in first
    assert (
        player.site_advance(nodes("1 сезон", "2 серия", "LostFilm", "00:01 / 58:42")).get(
            "site_next_episode"
        )
        is None
    )
    second = player.site_advance(nodes("1 сезон", "2 серия", "LostFilm", "00:02 / 58:42"))
    assert second["site_next_episode"] == {"season": 1, "episode": 2}
    assert second["position"] == 3539 and second["ended"] is False
    player.pending_next = None
    assert player.site_advance(nodes("1 сезон", "2 серия", "LostFilm", "00:03 / 58:42")) is None


def test_audio_menu_uses_normal_layout_and_restores_player_fullscreen():
    class Player(LearnedPlayer):
        is_fullscreen = True

        async def page(self, **values):
            return {"fullscreen": self.is_fullscreen}

        async def sample(self):
            return {"position": 900, "duration": 3600, "paused": False}

        async def control(self, action, position=0):
            assert action == "fullscreen"
            self.is_fullscreen = False

        async def reveal(self):
            assert not self.is_fullscreen

        async def nodes(self):
            assert not self.is_fullscreen
            return [{"role": "AXStaticText", "value": self.voice, "path": [0, 1, 2]}]

        async def select(self, nodes, menu, desired):
            assert not self.is_fullscreen

        async def confirm(self, nodes):
            self.playing = True
            return {"position": 901, "duration": 3600, "paused": False}

        async def fullscreen(self):
            self.is_fullscreen = True
            return {"fullscreen_confirmed": True, "capabilities": {"fullscreen": True}}

    player = Player(None, None)
    player.voice = "Original"
    player.audio_tracks = ["Original", "LostFilm"]
    result = asyncio.run(player.set_voice("LostFilm"))
    assert result["voice_confirmed"] and result["fullscreen_confirmed"]
    assert player.voice == "LostFilm" and player.is_fullscreen
    assert result["position"] == 901
