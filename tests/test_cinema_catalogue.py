"""Paged catalogue over official API contracts. No live account or playback."""

import asyncio
from datetime import date

import httpx
import pytest
from pydantic import ValidationError

from backend.cinema.catalogue import FEEDS, Catalogue
from backend.cinema.models import CatalogueQuery
from backend.cinema.tmdb import TMDB, TMDBCredentials, TMDBRateLimited
from backend.tools.registry import ToolError


def movie(identifier, **values):
    return {
        "id": identifier,
        "title": f"Film {identifier}",
        "original_title": f"Original {identifier}",
        "release_date": "2025-10-01",
        "vote_average": 8,
        "genre_ids": [53, 878],
        "original_language": "en",
        "popularity": identifier,
        **values,
    }


def api(tmp_path, handler, **options):
    credentials = TMDBCredentials(tmp_path / "integrations")
    credentials.save("a" * 32)
    return TMDB(credentials, transport=httpx.MockTransport(handler), request_interval=0, **options)


def test_pagination_keeps_totals_beyond_twenty_and_deduplicates(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        if "/genre/" in request.url.path:
            return httpx.Response(200, json={"genres": [{"id": 53, "name": "триллер"}]})
        page = int(request.url.params["page"])
        rows = [movie(i) for i in range((page - 1) * 20 + 1, page * 20 + 1)]
        return httpx.Response(
            200,
            json={"page": page, "total_pages": 3, "total_results": 60, "results": rows + [rows[0]]},
        )

    async def run():
        tmdb = api(tmp_path, handle)
        catalogue = Catalogue(tmdb)
        first = await catalogue.page(CatalogueQuery(kind="movie"))
        second = await catalogue.page(CatalogueQuery(kind="movie", page=2))
        last = await catalogue.page(CatalogueQuery(kind="movie", page=3))
        assert len(first["items"]) == len(second["items"]) == 20
        assert first["next_page"] == 2 and second["next_page"] == 3 and last["next_page"] is None
        assert first["total_pages"] == 3 and first["total_results"] == 60
        assert {r["catalogue_key"] for r in first["items"]}.isdisjoint(
            r["catalogue_key"] for r in second["items"]
        )
        await catalogue.page(CatalogueQuery(kind="movie", page=2))
        assert len([r for r in calls if "/discover/" in r.url.path]) == 3
        await tmdb.close()

    asyncio.run(run())


def test_mixed_types_stop_requesting_exhausted_stream(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        if "/genre/" in request.url.path:
            return httpx.Response(200, json={"genres": []})
        tv = request.url.path.endswith("/tv")
        return httpx.Response(
            200,
            json={
                "total_pages": 3 if tv else 1,
                "results": [
                    movie(1, **({"name": "Series", "first_air_date": "2025-10-01"} if tv else {}))
                ],
            },
        )

    async def run():
        tmdb = api(tmp_path, handle)
        catalogue = Catalogue(tmdb)
        first = await catalogue.page(CatalogueQuery())
        assert len(first["items"]) == 2
        second = await catalogue.page(CatalogueQuery(page=2))
        assert second["next_page"] == 3 and len(second["items"]) == 1
        assert len([r for r in calls if r.url.path == "/3/discover/movie"]) == 1
        await tmdb.close()

    asyncio.run(run())


def test_combined_discover_filters_and_native_sorting(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        if "/genre/" in request.url.path:
            return httpx.Response(200, json={"genres": [{"id": 53, "name": "триллер"}]})
        return httpx.Response(
            200, json={"total_pages": 42, "results": [movie(1), movie(2, vote_average=3)]}
        )

    async def run():
        tmdb = api(tmp_path, handle)
        result = await Catalogue(tmdb).page(
            CatalogueQuery(
                kind="movie",
                genre="триллер",
                year=2025,
                rating=7,
                country="US",
                original_language="en",
                sort="title",
            )
        )
        params = calls[-1].url.params
        assert params["with_genres"] == "53"
        assert params["primary_release_year"] == "2025"
        assert params["vote_average.gte"] == "7.0"
        assert params["with_origin_country"] == "US"
        assert params["with_original_language"] == "en"
        assert params["sort_by"] == "title.asc"
        assert len(result["items"]) == 1  # Discover movie rows omit countries; server applies it.
        assert result["sort_scope"] == "global"
        await tmdb.close()

    asyncio.run(run())


def test_search_all_names_filters_every_page_and_does_not_stop_on_sparse_page(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        if "/genre/" in request.url.path:
            data = {"genres": [{"id": 53, "name": "триллер"}]}
        elif "/search/" in request.url.path:
            page = int(request.url.params["page"])
            data = {
                "total_pages": 4,
                "results": [
                    movie(
                        page,
                        title="",
                        original_title="Interstellar",
                        original_language="en" if page == 2 else "ja",
                    )
                ],
            }
        else:
            data = movie(
                int(request.url.path.split("/")[-1]),
                title="",
                original_title="Interstellar",
                production_countries=[{"iso_3166_1": "US"}],
            )
        return httpx.Response(200, json=data)

    async def run():
        tmdb = api(tmp_path, handle)
        catalogue = Catalogue(tmdb)
        values = {
            "query": "Интерстеллар",
            "kind": "movie",
            "genre": "триллер",
            "country": "US",
            "year": 2025,
            "rating": 7,
            "original_language": "en",
        }
        # Detail values also participate in country/language filtering.
        first = await catalogue.page(CatalogueQuery(**values))
        assert first["next_page"] == 2
        second = await catalogue.page(CatalogueQuery(**values, page=2))
        assert second["items"][0]["title"] == "Interstellar"
        search = next(r for r in calls if "/search/" in r.url.path)
        assert search.url.params["query"] == "Интерстеллар"
        assert second["sort_scope"] == "page"
        original = await catalogue.page(CatalogueQuery(query="Interstellar", kind="movie"))
        assert original["items"][0]["title"] == "Interstellar"
        await tmdb.close()

    asyncio.run(run())


def test_all_curated_shelves_dates_genres_recommendations_and_cold_start(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        if "/genre/" in request.url.path:
            return httpx.Response(200, json={"genres": []})
        tv = "/tv/" in request.url.path or request.url.path.endswith("/tv")
        return httpx.Response(
            200,
            json={
                "total_pages": 2,
                "results": [
                    movie(1, **({"name": "TV", "first_air_date": "2025-10-01"} if tv else {}))
                ],
            },
        )

    async def run():
        tmdb = api(tmp_path, handle)
        catalogue = Catalogue(tmdb, today=lambda: date(2026, 11, 4))
        assert len(FEEDS) == 14
        for feed, _ in FEEDS:
            result = await catalogue.page(CatalogueQuery(feed=feed))
            assert result["feed"] == feed
        new = next(r for r in calls if r.url.params.get("primary_release_date.gte") == "2026-08-06")
        assert new.url.params["primary_release_date.lte"] == "2026-11-04"
        assert any(r.url.params.get("primary_release_date.gte") == "2026-11-05" for r in calls)
        assert any("/trending/movie/week" in r.url.path for r in calls)
        cold = await catalogue.page(CatalogueQuery(feed="recommended"))
        assert "мало истории" in cold["message"]
        personalized = await catalogue.page(
            CatalogueQuery(feed="recommended"), seeds=["tmdb:movie:10"], excluded={"tmdb:movie:1"}
        )
        assert personalized["items"] == [] and personalized["next_page"] == 2
        assert any("/movie/10/recommendations" in r.url.path for r in calls)
        assert any("/movie/10/similar" in r.url.path for r in calls)
        shown = await catalogue.page(CatalogueQuery(feed="recommended"), seeds=["tmdb:movie:10"])
        assert len(shown["items"]) == 1
        await tmdb.close()

    asyncio.run(run())


def test_cache_singleflight_ttl_capacity_and_credential_change(tmp_path):
    calls = []
    tick = [0]

    async def handle(request):
        calls.append(request)
        await asyncio.sleep(0.001)
        return httpx.Response(200, json={"results": []})

    async def run():
        tmdb = api(tmp_path, handle, clock=lambda: tick[0])
        tmdb.CACHE_LIMIT = 3
        await asyncio.gather(*(tmdb.get("discover/movie", page=1) for _ in range(10)))
        assert len(calls) == 1
        await tmdb.get("discover/movie", page=1)
        assert len(calls) == 1
        tick[0] += 901
        await tmdb.get("discover/movie", page=1)
        assert len(calls) == 2
        for page in range(2, 7):
            await tmdb.get("discover/movie", page=page)
        assert len(tmdb.cache) == 3
        tmdb.credentials.save("b" * 32)
        await tmdb.get("discover/movie", page=6)
        assert len(calls) == 8
        tmdb.credentials.save("")
        with pytest.raises(ToolError):
            await tmdb.get("discover/movie", page=6)
        await tmdb.close()

    asyncio.run(run())


def test_rate_limit_respects_retry_after_and_never_caches_error(tmp_path):
    calls = []
    tick = [0]

    def handle(request):
        calls.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "30"})
            if len(calls) == 1
            else httpx.Response(200, json={})
        )

    async def run():
        tmdb = api(tmp_path, handle, clock=lambda: tick[0])
        with pytest.raises(TMDBRateLimited) as error:
            await tmdb.get("movie/1")
        assert error.value.retry_after == 30
        with pytest.raises(TMDBRateLimited):
            await tmdb.get("movie/1")
        assert len(calls) == 1 and len(tmdb.cache) == 0
        tick[0] = 31
        await tmdb.get("movie/1")
        assert len(calls) == 2
        await tmdb.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "values",
    [{"page": 0}, {"page": 501}, {"country": "USA"}, {"sort": "script"}, {"rating": float("nan")}],
)
def test_catalogue_validation(values):
    with pytest.raises(ValidationError):
        CatalogueQuery(**values)


def test_request_start_spacing_and_bounded_concurrency(tmp_path):
    import time

    starts = []
    active = [0, 0]

    async def handle(request):
        starts.append(time.monotonic())
        active[0] += 1
        active[1] = max(active[1], active[0])
        await asyncio.sleep(0.04)
        active[0] -= 1
        return httpx.Response(200, json={})

    async def run():
        tmdb = api(tmp_path, handle)
        tmdb.request_interval = 0.025
        await asyncio.gather(*(tmdb.get(f"movie/{i}") for i in range(5)))
        assert all(b - a >= 0.02 for a, b in zip(starts, starts[1:], strict=False))
        assert active[1] <= 3
        await tmdb.close()

    asyncio.run(run())


def test_paged_hud_keeps_local_statuses_and_recommendations_follow_preferences(tmp_path):
    from types import SimpleNamespace

    from backend.cinema.service import Cinema
    from backend.config import Settings
    from backend.context import DeviceContext
    from backend.hud.bus import HudBus
    from backend.mac_agent import LocalMacAgent
    from backend.memory.store import MemoryStore
    from backend.tools.registry import ToolRegistry

    def handle(request):
        if "/genre/" in request.url.path:
            return httpx.Response(200, json={"genres": []})
        if (
            "/discover/" in request.url.path
            or "/recommendations" in request.url.path
            or "/similar" in request.url.path
        ):
            return httpx.Response(200, json={"total_pages": 2, "results": [movie(1), movie(2)]})
        return httpx.Response(200, json=movie(2))

    async def run():
        tmdb = api(tmp_path, handle)
        memory = MemoryStore(tmp_path / "assistant.sqlite")
        registry = ToolRegistry()
        registry.mac_agent = LocalMacAgent()
        service = Cinema(registry, Settings(data_dir=tmp_path / "data"), memory, tmdb=tmdb)
        bus = HudBus()
        service.bind(bus, SimpleNamespace(device=DeviceContext.local_mac()))
        watched = await service.db(
            "add", {"title": "Seen", "kind": "movie", "catalogue_key": "tmdb:movie:1"}
        )
        await service.db("update_title", watched["id"], {"status": "watched", "user_rating": 9})
        before = await service.db("library")
        await service.db("configure", {"russian_titles": False})
        await service.handle(
            {"op": "catalogue_page", "request_id": "one", "filters": {"feed": "recommended"}}
        )
        event = next(v for v in bus.snapshot() if v["type"] == "cinema")
        assert [i["catalogue_key"] for i in event["catalogue_page"]["items"]] == ["tmdb:movie:2"]
        assert await service.db("library") == before
        await service.db("configure", {"recommendations_show_watched": True})
        await service.handle(
            {"op": "catalogue_page", "request_id": "two", "filters": {"feed": "recommended"}}
        )
        event = next(v for v in bus.snapshot() if v["type"] == "cinema")
        assert len(event["catalogue_page"]["items"]) == 1
        await service.handle(
            {
                "op": "catalogue_add",
                "key": "tmdb:movie:2",
                "values": {"status": "watching", "favorite": True},
            }
        )
        added = next(i for i in await service.db("library") if i["catalogue_key"] == "tmdb:movie:2")
        assert added["status"] == "watching" and added["favorite"]
        await service.handle(
            {"op": "catalogue_page", "request_id": "bad", "filters": {"page": 501}}
        )
        event = next(v for v in bus.snapshot() if v["type"] == "cinema")
        assert event["catalogue_page"]["request_id"] == "bad" and event["catalogue_page"]["error"]
        await service.close()
        from backend.cinema.store import CinemaStore

        reopened = CinemaStore(tmp_path / "assistant.sqlite")
        assert (
            reopened.title(added["id"])["favorite"]
            and reopened.title(watched["id"])["status"] == "watched"
        )
        reopened.close()
        memory.close()

    asyncio.run(run())


def test_watched_manual_titles_are_excluded_without_catalogue_id(tmp_path):
    def handle(request):
        data = (
            {"genres": []}
            if "/genre/" in request.url.path
            else {"total_pages": 1, "results": [movie(1)]}
        )
        return httpx.Response(200, json=data)

    async def run():
        tmdb = api(tmp_path, handle)
        result = await Catalogue(tmdb).page(
            CatalogueQuery(feed="recommended", kind="movie"),
            excluded_titles=[("movie", "film 1", 0)],
        )
        assert result["items"] == []
        await tmdb.close()

    asyncio.run(run())


def test_recommendations_of_another_type_use_explicit_cold_start(tmp_path):
    def handle(request):
        if "/genre/" in request.url.path:
            return httpx.Response(200, json={"genres": []})
        return httpx.Response(
            200,
            json={
                "total_pages": 1,
                "results": [
                    {"id": 9, "name": "Series", "first_air_date": "2025-01-01", "vote_average": 8}
                ],
            },
        )

    async def run():
        tmdb = api(tmp_path, handle)
        result = await Catalogue(tmdb).page(
            CatalogueQuery(feed="recommended", kind="series"), seeds=["tmdb:movie:10"]
        )
        assert len(result["items"]) == 1
        assert "мало истории" in result["message"]
        await tmdb.close()

    asyncio.run(run())


@pytest.mark.parametrize("search", [False, True])
@pytest.mark.parametrize("mode,expected", [("or", [1, 2, 3]), ("and", [3])])
def test_multiple_genres_or_and_in_discover_and_search(tmp_path, search, mode, expected):
    calls = []

    def handle(request):
        calls.append(request)
        if "/genre/" in request.url.path:
            return httpx.Response(
                200,
                json={"genres": [{"id": 53, "name": "Триллер"}, {"id": 9648, "name": "Детектив"}]},
            )
        return httpx.Response(
            200,
            json={
                "total_pages": 2,
                "results": [
                    movie(1, genre_ids=[53]),
                    movie(2, genre_ids=[9648]),
                    movie(3, genre_ids=[53, 9648]),
                    movie(4, genre_ids=[35]),
                ],
            },
        )

    async def run():
        tmdb = api(tmp_path, handle)
        result = await Catalogue(tmdb).page(
            CatalogueQuery(
                kind="movie",
                query="story" if search else "",
                genres=["Триллер", "Детектив"],
                genre_mode=mode,
                year=2025,
                rating=7,
            )
        )
        assert {int(i["catalogue_key"].split(":")[-1]) for i in result["items"]} == set(expected)
        if not search:
            assert calls[-1].url.params["with_genres"] == ("53|9648" if mode == "or" else "53,9648")
        assert result["next_page"] == 2
        await tmdb.close()

    asyncio.run(run())


def test_type_specific_genres_handle_missing_genre_in_or_and_and_configuration(tmp_path):
    def handle(request):
        if "/genre/" in request.url.path:
            genres = (
                [{"id": 53, "name": "Триллер"}]
                if "/movie/" in request.url.path
                else [{"id": 18, "name": "Драма"}]
            )
            return httpx.Response(200, json={"genres": genres})
        if "/configuration/" in request.url.path:
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json={
                "results": [movie(1, genre_ids=[18], name="Series", first_air_date="2025-10-01")]
            },
        )

    async def run():
        tmdb = api(tmp_path, handle)
        catalogue = Catalogue(tmdb)
        config = await catalogue.configuration()
        assert config["genres_by_kind"] == {"movie": ["Триллер"], "series": ["Драма"]}
        values = {"kind": "series", "genres": ["Триллер", "Драма"]}
        assert len((await catalogue.page(CatalogueQuery(**values)))["items"]) == 1
        assert not (await catalogue.page(CatalogueQuery(**values, genre_mode="and")))["items"]
        await tmdb.close()

    asyncio.run(run())
