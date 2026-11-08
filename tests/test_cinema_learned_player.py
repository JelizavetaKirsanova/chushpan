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
        player = LearnedPlayer(None, None, tabs=tabs)
        player.page = AsyncMock(return_value={"fullscreen": True})
        await player.activate()
        action = tabs._owned.call_args.args[1]
        assert "activate" in action
        assert "current tab" not in action and "set index" not in action
        player.page.return_value = {"fullscreen": False}
        await player.activate()
        assert "set current tab" in tabs._owned.call_args.args[1]

    asyncio.run(run())
