"""Catalogue policy, notes, lossless transfers and explicit native save boundaries."""

import asyncio
import json
import sqlite3
import stat
from types import SimpleNamespace

import httpx
import pytest

from backend.cinema.catalogue import Catalogue
from backend.cinema.commands import command
from backend.cinema.filters import allowed, with_preferences
from backend.cinema.models import CatalogueQuery, Preferences
from backend.cinema.personal import Recommender, taste
from backend.cinema.store import MIGRATIONS, CinemaStore
from backend.cinema.tmdb import TMDB, TMDBCredentials
from backend.cinema.transfer import validate_document


def media(title="История", *, genres=(), countries=(), keywords=()):
    return {
        "title": title,
        "metadata": {
            "genre_ids": list(genres),
            "genres": [],
            "countries": list(countries),
            "keywords": list(keywords),
        },
    }


@pytest.mark.parametrize(
    ("item", "patch", "expected"),
    [
        (media("Original title"), {}, False),
        (media("Русское название", countries=["US"]), {}, True),
        (media("日本語", countries=["RU"]), {}, False),
        (media("Русский перевод", genres=[16]), {}, False),
        (media("Русский перевод", genres=[16], countries=["JP"]), {"hide_animation": False}, False),
        (media("Японская драма", genres=[18], countries=["JP"]), {}, True),
        (
            media(
                "Манга: игровая версия", genres=[18], countries=["JP"], keywords=["based on manga"]
            ),
            {},
            True,
        ),
        (media("Аниме", genres=[18], keywords=["anime"]), {"hide_animation": False}, False),
        (media("Документальный", genres=[99]), {}, True),
        (media("Документальный", genres=[99]), {"hide_documentary": True}, False),
        (
            media("Original title", genres=[16]),
            {"russian_titles": False, "hide_animation": False},
            True,
        ),
    ],
)
def test_shared_policy_does_not_confuse_title_language_with_country_or_all_japanese_films(
    item, patch, expected
):
    assert allowed(item, Preferences(**patch)) is expected


def test_manual_search_can_relax_policy_without_relaxing_year_genre_and_rating():
    query = with_preferences(
        CatalogueQuery(query="Original", relax_restrictions=True, year_min=2000, year_max=2010),
        Preferences(),
    )
    assert allowed(media("Original", genres=[16]), query)
    assert not allowed(
        media("Original"), with_preferences(CatalogueQuery(relax_restrictions=True), Preferences())
    )
    assert not Catalogue.matches(
        {"release_date": "1990-01-01", "vote_average": 8}, query, "movie", []
    )
    with pytest.raises(ValueError):
        CatalogueQuery(year_min=2020, year_max=2000)


def test_applied_catalogue_filters_are_persisted_and_validated(tmp_path):
    path = tmp_path / "filters.sqlite"
    store = CinemaStore(path)
    settings = {
        "kind": "movie",
        "genres": ["триллер", "детектив"],
        "genre_mode": "and",
        "year_min": 1990,
        "year_max": 2026,
        "rating": 7.5,
        "country": "US",
    }
    store.configure({"catalogue_filters": settings})
    store.close()
    store = CinemaStore(path)
    assert store.preferences().catalogue_filters == settings
    with pytest.raises(ValueError):
        store.configure({"catalogue_filters": {"year_min": 2026, "year_max": 1990}})
    with pytest.raises(ValueError):
        store.configure({"catalogue_filters": {"page": 500, "query": "private search"}})
    assert store.preferences().catalogue_filters == settings
    store.close()


def test_catalogue_policy_filters_server_pages_and_keeps_raw_pagination(tmp_path):
    seen = []

    def handle(request):
        seen.append(request)
        if "/genre/" in request.url.path:
            return httpx.Response(
                200,
                json={"genres": [{"id": 18, "name": "драма"}, {"id": 16, "name": "мультфильм"}]},
            )
        return httpx.Response(
            200,
            json={
                "page": 1,
                "total_pages": 3,
                "total_results": 4,
                "results": [
                    {"id": 1, "title": "Original", "release_date": "2005-01-01", "genre_ids": [18]},
                    {"id": 2, "title": "Перевод", "release_date": "2005-01-01", "genre_ids": [18]},
                    {
                        "id": 3,
                        "title": "Мультфильм",
                        "release_date": "2005-01-01",
                        "genre_ids": [16],
                    },
                    {"id": 4, "title": "Старое", "release_date": "1990-01-01", "genre_ids": [18]},
                ],
            },
        )

    async def run():
        credentials = TMDBCredentials(tmp_path / "credentials")
        credentials.save("fixture-token-for-filter-tests")
        api = TMDB(credentials, transport=httpx.MockTransport(handle), request_interval=0)
        result = await Catalogue(api).page(
            with_preferences(
                CatalogueQuery(kind="movie", year_min=2000, year_max=2010), Preferences()
            )
        )
        assert [i["title"] for i in result["items"]] == ["Перевод"]
        assert result["next_page"] == 2 and not result["filtered_total"]
        params = seen[-1].url.params
        assert params["language"] == "ru-RU" and params["without_genres"] == "16"
        assert params["primary_release_date.gte"] == "2000-01-01"
        assert params["primary_release_date.lte"] == "2010-12-31"
        await api.close()

    asyncio.run(run())


def test_v4_migration_preserves_existing_rating_status_and_history(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE cinema_migrations(version INTEGER PRIMARY KEY)")
    for version, script in enumerate(MIGRATIONS[:-1], 1):
        db.executescript(script)
        db.execute("INSERT INTO cinema_migrations VALUES (?)", (version,))
    stamp = "2026-11-04T12:00:00+00:00"
    db.execute(
        "INSERT INTO cinema_titles"
        "(id,identity,title,kind,year,poster_url,description,catalogue_key,updated_at) "
        "VALUES ('old','old','Фильм','movie',2020,'','','',?)",
        (stamp,),
    )
    db.execute(
        "INSERT INTO cinema_library(title_id,status,favorite,user_rating,added_at,updated_at) "
        "VALUES ('old','watched',1,9.5,?,?)",
        (stamp, stamp),
    )
    db.execute(
        "INSERT INTO cinema_episodes(id,title_id,season,episode,position,duration) "
        "VALUES ('ep','old',0,0,80,100)"
    )
    db.commit()
    db.close()
    store = CinemaStore(path)
    item = store.detail("old")
    assert (item["user_rating"], item["favorite"], item["status"], item["user_note"]) == (
        9.5,
        1,
        "watched",
        "",
    )
    assert item["current"]["position"] == 80
    store.update_title("old", {"user_note": "Люблю эту историю"})
    store.replay("old")
    assert (
        store.title("old")["user_rating"] == 9.5
        and store.title("old")["user_note"] == "Люблю эту историю"
    )
    store.close()
    reopened = CinemaStore(path)
    assert reopened.title("old")["user_note"] == "Люблю эту историю"
    assert reopened.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    reopened.close()


def populated(path):
    store = CinemaStore(path)
    source = store.save_source(
        {"name": "Мой сайт", "base_url": "https://cinema.example", "preferred": True}
    )
    item = store.add(
        {
            "title": "Мой сериал",
            "kind": "series",
            "year": 2020,
            "source_id": source["id"],
            "url": "https://cinema.example/one",
        }
    )
    item = store.add(
        {
            "title_id": item["id"],
            "title": item["title"],
            "kind": "series",
            "source_id": source["id"],
            "url": "https://cinema.example/two",
            "episode": 2,
        }
    )
    ep = item["episodes"][1]
    store.progress(ep["id"], 121, 1000, "manual", ep["links"][0]["id"])
    store.update_title(
        item["id"], {"user_rating": 9.5, "user_note": "Сильный финал", "favorite": True}
    )
    store.remember_source(item["id"], source["id"], ep["id"], ep["links"][0]["id"], pin=True)
    store.configure({"russian_titles": False, "default_sort": "title", "recommend_history": False})
    return store, item["id"]


def test_export_import_roundtrip_progress_notes_sources_and_journal_without_duplicates(tmp_path):
    source, identifier = populated(tmp_path / "source.sqlite")
    document = source.export_library()
    assert "credential" not in json.dumps(document) and document["schema"] == "chushpan.cinema"
    target = CinemaStore(tmp_path / "target.sqlite")
    result = target.import_library(document)
    assert result["added"] == 1
    item = target.library()[0]
    assert (item["user_rating"], item["user_note"], item["favorite"], item["status"]) == (
        9.5,
        "Сильный финал",
        1,
        "watching",
    )
    assert item["current"]["episode"] == 2 and item["current"]["position"] == 121
    assert item["source_memory"]["pinned_source_id"] == target.sources()[0]["id"]
    assert target.sources()[0]["preferred"] and target.preferences().default_sort == "title"
    assert len(target.history()) == len(source.history())
    assert (
        target.db.execute("SELECT created_at FROM cinema_rating_events").fetchone()[0]
        == source.db.execute("SELECT created_at FROM cinema_rating_events").fetchone()[0]
    )
    target.update_title(
        item["id"], {"user_rating": 7.5, "user_note": "Новая заметка", "status": "planned"}
    )
    target.progress(item["current"]["id"], 150, 1000, "manual")
    history_count = len(target.history())
    assert target.import_library(document)["added"] == 0
    assert (
        target.title(item["id"])["user_rating"] == 7.5
        and target.title(item["id"])["user_note"] == "Новая заметка"
    )
    assert target.detail(item["id"])["current"]["position"] == 150
    assert len(target.history()) == history_count
    assert not target.db.execute("PRAGMA foreign_key_check").fetchall()
    validate_document(target.export_library())
    source.close()
    target.close()


@pytest.mark.parametrize("mutation", ["url", "position", "note", "rating", "cross_link", "schema"])
def test_invalid_import_never_changes_existing_library(tmp_path, mutation):
    source, _ = populated(tmp_path / "source.sqlite")
    document = source.export_library()
    if mutation == "url":
        document["items"][0]["episodes"][0]["links"][0]["url"] = "https://other.example/one"
    elif mutation == "position":
        document["items"][0]["episodes"][0]["position"] = float("nan")
    elif mutation == "note":
        document["items"][0]["user_note"] = "x" * 2001
    elif mutation == "rating":
        document["items"][0]["user_rating"] = 10.5
    elif mutation == "cross_link":
        document["history"][0]["link_id"] = document["items"][0]["episodes"][0]["links"][0]["id"]
    else:
        document["schema"] = "other"
    target = CinemaStore(tmp_path / "target.sqlite")
    before = target.snapshot()
    with pytest.raises(ValueError):
        target.import_library(document)
    assert target.snapshot() == before
    target.close()
    source.close()


def test_import_sql_failure_rolls_back_sources_titles_and_preferences(tmp_path):
    source, _ = populated(tmp_path / "source.sqlite")
    target = CinemaStore(tmp_path / "target.sqlite")
    target.db.execute(
        "CREATE TRIGGER reject_import BEFORE UPDATE ON cinema_library "
        "BEGIN SELECT RAISE(ABORT,'fixture'); END"
    )
    target.db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        target.import_library(source.export_library())
    assert target.sources() == [] and target.library() == [] and target.preferences().russian_titles
    source.close()
    target.close()


def test_reimport_of_series_starting_midseason_does_not_invent_first_episode(tmp_path):
    source = CinemaStore(tmp_path / "source.sqlite")
    source.add({"title": "Поздний сезон", "kind": "series", "season": 3, "episode": 2})
    target = CinemaStore(tmp_path / "target.sqlite")
    document = source.export_library()
    target.import_library(document)
    target.import_library(document)
    assert [(ep["season"], ep["episode"]) for ep in target.library()[0]["episodes"]] == [(3, 2)]
    source.close()
    target.close()


def test_recommendation_factors_are_independent_and_new_pools_obey_policy():
    item = {
        "id": "favorite",
        "title": "Любимое",
        "kind": "movie",
        "year": 2020,
        "catalogue_key": "tmdb:movie:1",
        "user_rating": 10,
        "favorite": True,
        "status": "watched",
        "metadata": {"genres": ["драма"], "genre_ids": [18], "rating": 8, "enriched": True},
    }
    prefs = Preferences(recommend_ratings=False, recommend_favorites=False, recommend_history=False)
    assert taste([item], prefs=prefs)[1] == 0
    assert taste(
        [item],
        prefs=Preferences(
            recommend_ratings=True, recommend_favorites=False, recommend_history=False
        ),
    )[0]

    async def run():
        candidates = [
            {**item, "id": "2", "catalogue_key": "tmdb:movie:2", "title": "Original"},
            {
                **item,
                "id": "3",
                "catalogue_key": "tmdb:movie:3",
                "title": "Мультфильм",
                "metadata": {**item["metadata"], "genre_ids": [16]},
            },
        ]

        class CatalogueFixture:
            async def page(self, query, **kwargs):
                assert query.russian_titles and query.hide_animation
                return {"items": candidates}

        result = await Recommender(SimpleNamespace(), CatalogueFixture()).recommendations(
            [item], {"reset_at": "", "dismissed": []}, Preferences()
        )
        assert all(not shelf["items"] for shelf in result["shelves"] if shelf["id"] != "rewatch")
        assert result["shelves"][-1]["name"] == "Пересмотреть любимое"

    asyncio.run(run())


def test_native_export_uses_explicit_save_panel_and_private_file(tmp_path):
    from backend.hud.window import _Api

    source, _ = populated(tmp_path / "source.sqlite")
    destination = tmp_path / "library.json"
    api = _Api()
    api.window = SimpleNamespace(create_file_dialog=lambda *args, **kwargs: [str(destination)])
    assert api.save_cinema_library(source.export_library())["ok"]
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert json.loads(destination.read_text())["items"][0]["user_note"] == "Сильный финал"
    api.window = SimpleNamespace(create_file_dialog=lambda *args, **kwargs: None)
    assert api.save_cinema_library(source.export_library())["cancelled"]
    source.close()


def test_new_rating_voice_commands_are_local_and_keep_other_state(tmp_path):
    from backend.brain.conversation import Conversation
    from backend.cinema.service import Cinema
    from backend.config import Settings
    from backend.hud.bus import HudBus
    from backend.mac_agent import LocalMacAgent
    from backend.memory.store import MemoryStore
    from backend.tools.registry import ToolRegistry
    from scripts.check_cinema_browser import NoLLM

    assert command("поставь этому фильму девять с половиной")["rating"] == 9.5
    assert command("поставь этому фильму десять с половиной") is None

    async def run():
        memory = MemoryStore(tmp_path / "assistant.sqlite")
        tools = ToolRegistry()
        tools.mac_agent = LocalMacAgent()
        settings = Settings(language="ru-RU", data_dir=tmp_path / "data")
        cinema = Cinema(tools, settings, memory)
        tools.cinema = cinema
        conv = Conversation(settings, NoLLM(), memory, tools=tools)
        cinema.bind(HudBus(), conv)
        item = await cinema.db("add", {"title": "Фильм", "kind": "movie"})
        await cinema.handle({"op": "focus", "id": item["id"]})
        for text in ("добавь этот фильм в избранное", "поставь этому фильму девять с половиной"):
            answer = "".join([part async for part in conv.reply("Чушпан, " + text)])
            assert conv.last_stats.tools == ["cinema_command"], answer
        saved = await cinema.db("title", item["id"])
        assert saved["favorite"] and saved["user_rating"] == 9.5 and saved["status"] == "planned"
        await cinema.close()
        memory.close()

    asyncio.run(run())
