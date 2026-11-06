"""Personal data stays in temporary SQLite; TMDB and the LLM never run live."""

import asyncio

from backend.cinema.models import Preferences
from backend.cinema.personal import Recommender, profile, score, signal, taste
from backend.cinema.store import CinemaStore


def title(key, *, genre="драма", rating=None, status="planned", favorite=False, director=""):
    return {
        "id": key,
        "title": key,
        "kind": "movie",
        "year": 2024,
        "catalogue_key": "tmdb:movie:" + str(abs(sum(map(ord, key)))),
        "poster_url": "",
        "description": "",
        "user_rating": rating,
        "status": status,
        "favorite": favorite,
        "updated_at": "2026-01-01T00:00:00Z",
        "metadata": {
            "genres": [genre],
            "directors": [director] if director else [],
            "cast": ["Актёр"],
            "keywords": [genre],
            "rating": 8,
            "vote_count": 100,
            "enriched": True,
        },
    }


def test_profile_uses_real_sample_half_ratings_and_observed_dates():
    a = title("Любимое", rating=9.5, status="watched", favorite=True, director="Режиссёр")
    b = title("Не понравилось", genre="ужасы", rating=2, status="watched")
    c = {**title("Сериал", rating=8, status="watched"), "kind": "series"}
    data = profile(
        [a, b, c],
        [
            {"title_id": a["id"], "created_at": "2026-01-02T00:00:00Z"},
            {"title_id": a["id"], "created_at": "2026-01-03T00:00:00Z"},
            {"title_id": b["id"], "created_at": "2026-02-03T00:00:00Z"},
        ],
        [{"title_id": a["id"], "rating": 9.5, "created_at": "2026-03-01T00:00:00Z"}],
    )
    assert (data["watched_movies"], data["watched_series"], data["favorites"]) == (2, 1, 1)
    assert data["rated_count"] == 3 and data["average"] == 6.5
    assert data["confidence"] == "small"
    assert data["distribution"][8]["count"] == 1  # 9.5 belongs to 9–9.5.
    assert data["directors"] == [{"name": "Режиссёр", "count": 1}]
    assert data["top"][0]["id"] == a["id"] and data["least"][0]["id"] == b["id"]
    assert data["trend"][0]["count"] == 1 and data["trend"][0]["rating"] is None
    assert data["trend"][-1]["rating"] == 9.5  # Never backdate current ratings to viewing dates.


def test_negative_ratings_lower_genre_director_and_keyword_scores():
    liked = title("Любимое", genre="триллер", rating=10, director="Автор A")
    disliked = title("Плохое", genre="ужасы", rating=1, director="Автор B")
    weights, samples = taste([liked, disliked])
    good = title("Похожее", genre="триллер", director="Автор A")
    bad = title("Непохожее", genre="ужасы", director="Автор B")
    assert samples == 2
    assert score(good, weights)[0] > score(bad, weights)[0]
    assert weights["genre:ужасы"] < 0 and weights["director:автор b"] < 0
    assert "триллер" in score(good, weights)[1] or "Автор A" in score(good, weights)[1]


def test_reset_ignores_old_ratings_even_if_unrelated_library_status_changes():
    item = {
        **title("Старое", rating=10, favorite=True),
        "library_updated_at": "2026-11-04T12:00:00Z",
        "rating_updated_at": "2026-01-01T00:00:00Z",
        "favorite_added_at": "2026-01-01T00:00:00Z",
    }
    assert signal(item, "2026-11-04T10:00:00Z") == 0
    item["rating_updated_at"] = "2026-11-04T13:00:00Z"
    assert signal(item, "2026-11-04T10:00:00Z") > 0


def test_recommendations_exclude_watched_dismissals_and_offer_six_explained_shelves():
    async def run():
        watched = title("Уже видела", rating=10, status="watched", genre="триллер")
        legacy = title("Ручное", status="watched")
        legacy["catalogue_key"] = ""
        available = title("Посмотреть сейчас", genre="триллер")
        candidate = title("Ручное")
        dismissed = title("Скрытое")
        candidates = [watched, candidate, available, dismissed, title("Новое", genre="комедия")]

        class Catalogue:
            async def page(self, query, **kwargs):
                return {"items": candidates}

        class TMDB:
            async def detail(self, key):
                raise AssertionError("All metadata is already enriched")

        recommender = Recommender(TMDB(), Catalogue())
        result = await recommender.recommendations(
            [watched, legacy],
            {"dismissed": [dismissed["catalogue_key"]], "reset_at": ""},
            Preferences(),
            ready={available["catalogue_key"]},
        )
        assert len(result["shelves"]) == 6
        keys = {
            i["catalogue_key"]
            for shelf in result["shelves"]
            if shelf["id"] != "rewatch"
            for i in shelf["items"]
        }
        assert watched["catalogue_key"] not in keys
        assert candidate["catalogue_key"] not in keys and dismissed["catalogue_key"] not in keys
        assert (
            next(shelf for shelf in result["shelves"] if shelf["id"] == "now")["items"][0]["title"]
            == available["title"]
        )
        assert all(
            i["reason"]
            for shelf in result["shelves"]
            if shelf["id"] != "rewatch"
            for i in shelf["items"]
        )
        assert all(
            len({i["catalogue_key"] for i in shelf["items"]}) == len(shelf["items"])
            for shelf in result["shelves"]
        )
        assert "Мало оценок" in result["message"]

    asyncio.run(run())


def test_settings_feedback_reset_and_analytics_survive_restart_without_data_loss(tmp_path):
    path = tmp_path / "cinema.sqlite"
    store = CinemaStore(path)
    item = store.add({"title": "Фильм", "kind": "movie", "metadata": {"genres": ["драма"]}})
    store.update_title(item["id"], {"user_rating": 9.5, "favorite": True, "status": "watched"})
    store.progress(item["current"]["id"], 99, 100, "manual")
    store.configure({"preferred_language": "et", "free_only": True, "allow_browser": False})
    store.feedback("tmdb:movie:123")
    before = store.analytics()
    store.reset_recommendations()
    store.close()
    reopened = CinemaStore(path)
    assert reopened.analytics() == before
    assert (
        reopened.preferences().watch_region == "EE"
        and reopened.preferences().preferred_language == "et"
    )
    assert not reopened.preferences().allow_browser and reopened.preferences().free_only
    assert (
        reopened.recommendation_data()["reset_at"]
        and not reopened.recommendation_data()["dismissed"]
    )
    assert reopened.detail(item["id"])["user_rating"] == 9.5
    assert reopened.detail(item["id"])["current"]["position"] == 99
    assert len(reopened.history()) == 1
    reopened.close()


def test_profile_hud_and_voice_use_local_handler_without_llm(tmp_path):
    from backend.brain.conversation import Conversation
    from backend.cinema.service import Cinema
    from backend.config import Settings
    from backend.hud.bus import HudBus
    from backend.mac_agent import LocalMacAgent
    from backend.memory.store import MemoryStore
    from backend.tools.registry import ToolRegistry
    from scripts.check_cinema_browser import NoLLM

    async def run():
        memory = MemoryStore(tmp_path / "assistant.sqlite")
        registry = ToolRegistry()
        registry.mac_agent = LocalMacAgent()
        settings = Settings(language="ru-RU", data_dir=tmp_path / "data")
        cinema = Cinema(registry, settings, memory)
        registry.cinema = cinema
        conv = Conversation(settings, NoLLM(), memory, tools=registry)
        bus = HudBus()
        cinema.bind(bus, conv)
        answer = "".join([part async for part in conv.reply("Чушпан, покажи мой кинопрофиль")])
        assert "кинопрофиль" in answer
        await cinema.handle({"op": "profile", "request_id": "profile-1"})
        event = next(e for e in bus.snapshot() if e["type"] == "cinema")
        assert event["personal_result"]["profile"]["rated_count"] == 0
        assert event["personal_result"]["request_id"] == "profile-1"
        await cinema.close()
        memory.close()

    asyncio.run(run())


def test_all_new_voice_examples_route_through_conversation_without_llm_or_browser(tmp_path):
    from backend.brain.conversation import Conversation
    from backend.cinema.service import Cinema
    from backend.config import Settings
    from backend.hud.bus import HudBus
    from backend.mac_agent import LocalMacAgent
    from backend.memory.store import MemoryStore
    from backend.tools.registry import ToolRegistry
    from scripts.check_cinema_browser import NoLLM

    async def run():
        memory = MemoryStore(tmp_path / "assistant.sqlite")
        tools = ToolRegistry()
        tools.mac_agent = LocalMacAgent()
        settings = Settings(language="ru-RU", data_dir=tmp_path / "data")
        cinema = Cinema(tools, settings, memory)
        tools.cinema = cinema
        conv = Conversation(settings, NoLLM(), memory, tools=tools)
        cinema.bind(HudBus(), conv)
        await cinema.db("add", {"title": "Интерстеллар", "kind": "movie", "year": 2014})
        series = await cinema.db("add", {"title": "Мой сериал", "kind": "series"})
        series = await cinema.db("season", series["id"], [{"season": 3, "episode": 2}])
        await cinema.db("progress", series["current"]["id"], 1, 100, "manual")
        cinema.focused = series["id"]
        searches = []

        async def search(title_id, episode_id=None, **kwargs):
            searches.append((title_id, episode_id, kwargs))
            return {
                "title_id": title_id,
                "episode_id": episode_id,
                "results": [],
                "selected_id": None,
                "message": "Проверенного источника нет. Выбери вручную.",
            }

        async def recommendations(*args, **kwargs):
            return {
                "shelves": [{"items": [{"title": "Тестовый триллер"}]}],
                "message": "Предварительные рекомендации.",
                "mood": "any",
            }

        cinema.sources.search = search
        cinema.personal.recommendations = recommendations
        for text in [
            "найди где посмотреть Интерстеллар",
            "включи мой сериал",
            "продолжи просмотр",
            "включи вторую серию третьего сезона",
            "найди другой сайт для этого сериала",
            "посоветуй мне хороший триллер",
            "найди что-нибудь похожее на мои любимые фильмы",
            "покажи мой кинопрофиль",
            "что мне сегодня посмотреть",
        ]:
            answer = "".join([part async for part in conv.reply("Чушпан, " + text)])
            assert conv.last_stats.tools == ["cinema_command"], (text, answer)
            assert "включил фильм" not in answer.casefold()
        expected = next(e for e in series["episodes"] if e["season"] == 3 and e["episode"] == 2)
        assert any(episode_id == expected["id"] for _, episode_id, _ in searches)
        assert any(kwargs.get("alternative") for _, _, kwargs in searches)
        await cinema.close()
        memory.close()

    asyncio.run(run())
