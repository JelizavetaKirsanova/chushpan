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
