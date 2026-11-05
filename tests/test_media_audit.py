"""Regressions for optional years, app relocation, and AX playback verification.

All applications, HTTP responses and accessibility observations are test doubles.
"""

import asyncio
import plistlib
from unittest.mock import AsyncMock

import pytest

from backend.cinema.adapters import HTML5Adapter
from backend.cinema.classification import classify
from backend.cinema.internet import verified_entity
from backend.cinema.models import CatalogueQuery
from backend.config import Settings
from backend.media.safari import ElementChanged, MusicAppNotFound, SafariMusic, playback_seconds
from backend.tools.media import MediaService
from backend.tools.registry import ToolError
from tests.test_cinema_catalogue import Catalogue, api, movie
from tests.test_cinema_classification import MEDIA, QUERY, evidence, work


def test_seek_waits_until_safari_finishes_seeking_even_if_old_position_is_close(monkeypatch):
    from types import SimpleNamespace

    bridge = SimpleNamespace(
        evaluate=AsyncMock(
            side_effect=[
                {"position": 7.3, "duration": 14, "seeking": True},
                {"position": 7.3, "duration": 14, "seeking": True},
                {"position": 9, "duration": 14, "seeking": False},
            ]
        )
    )
    adapter = HTML5Adapter(bridge)
    monkeypatch.setattr("backend.cinema.adapters.asyncio.sleep", AsyncMock())
    result = asyncio.run(adapter.control("seek", 9))
    assert result["position"] == 9 and bridge.evaluate.await_count == 3


@pytest.mark.parametrize(
    "label,kind",
    [
        ("Обзор фильма", "review"),
        ("Клип из фильма", "clip"),
        ("Official trailer", "trailer"),
        ("Тизер", "teaser"),
    ],
)
def test_promotional_media_kinds_remain_distinct_and_never_verify_as_a_full_movie(label, kind):
    result = classify(QUERY, evidence(work(associatedMedia={"name": label, "contentUrl": MEDIA})))
    assert result["video_type"] == kind and not result["content_verified"]


def test_sitemap_search_with_unknown_year_returns_a_card_without_claiming_playback():
    from backend.cinema.public_search import PublicCatalogue
    from tests.test_cinema_watch import BASE, QUERY, CatalogueHTTP

    async def check():
        rows = await PublicCatalogue({"base_url": BASE}, CatalogueHTTP()).search(
            {**QUERY, "year": 0}
        )
        assert len(rows) == 1 and rows[0]["scope"] == "series"
        assert not rows[0]["content_verified"]

    asyncio.run(check())


def test_archive_search_with_unknown_year_keeps_manual_candidates():
    from backend.cinema.sources import ArchiveAdapter
    from tests.test_cinema_sources import HTTP

    http = HTTP()
    rows = asyncio.run(
        ArchiveAdapter({"base_url": "https://archive.org"}, http).search(
            {
                "title": "Film",
                "kind": "movie",
                "year": 0,
            }
        )
    )
    assert rows and all(row["kind"] == "page" for row in rows)
    assert not any(row.get("content_verified") for row in rows)
    assert not any("restricted" in row["url"] or "trailer" in row["url"] for row in rows)
    assert "year:0" not in http.calls[0][1]["params"]["q"]


@pytest.mark.parametrize("year", [None, 0])
@pytest.mark.parametrize("published", [None, "2025-01-01"])
def test_unknown_year_allows_a_card_but_full_playback_needs_an_exact_identity(year, published):
    query = {**QUERY, "year": year}
    entity = work(datePublished=published)
    assert verified_entity(entity, query)
    assert not classify(query, evidence(entity))["content_verified"]
    identified = {**query, "metadata": {**query["metadata"], "imdb_id": "tt12345678"}}
    entity["sameAs"] = "https://www.imdb.com/title/tt12345678/"
    assert classify(identified, evidence(entity))["content_verified"]
    promo = work(datePublished=published, trailer={"contentUrl": MEDIA}, associatedMedia={})
    result = classify(query, evidence(promo))
    assert result["video_type"] == "trailer" and not result["content_verified"]


def test_known_year_mismatch_cannot_be_overridden_by_a_matching_imdb_id():
    entity = work(datePublished="2024", sameAs="https://www.imdb.com/title/tt12345678/")
    query = {**QUERY, "metadata": {"imdb_id": "tt12345678"}}
    assert verified_entity(entity, query) is None
    assert not classify(query, evidence(entity))["content_verified"]


def app_bundle(root, bundle="com.apple.Safari.WebApp.test", *, native=False):
    path = root / "Music.app"
    (path / "Contents").mkdir(parents=True)
    info = {"CFBundleIdentifier": bundle}
    if not native:
        info["Manifest"] = {"start_url": "https://music.youtube.com/"}
    (path / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    return path


def music(monkeypatch, *paths):
    settings = Settings()
    settings.media.provider = "youtube_music"
    settings.media.youtube_music.transport = "auto"
    settings.media.youtube_music.app_path = None
    settings.media.youtube_music.bundle_id = None
    monkeypatch.setattr(SafariMusic, "application_paths", lambda _: list(paths))
    return settings, SafariMusic(settings)


def test_ambiguous_app_never_chooses_the_first_or_falls_back_to_safari(tmp_path, monkeypatch):
    one = app_bundle(tmp_path / "one")
    two = app_bundle(tmp_path / "two")
    settings, app = music(monkeypatch, one, two)
    with pytest.raises(ToolError, match="несколько"):
        app.discover()
    settings.media.youtube_music.allow_browser = True
    service = MediaService(settings)
    assert isinstance(service.youtube.app, SafariMusic)
    assert service.music_application["kind"] == "ambiguous"
    assert "несколько" in service.music_application["error"]
    settings.media.youtube_music.app_path = two
    assert app.discover()[0] == two


def test_config_changes_and_reinstall_are_seen_on_the_same_adapter(tmp_path, monkeypatch):
    one = app_bundle(tmp_path / "one")
    two = app_bundle(tmp_path / "two", "com.apple.Safari.WebApp.two")
    settings, app = music(monkeypatch, one, two)
    settings.media.youtube_music.app_path = one
    assert app.discover()[0] == one
    settings.media.youtube_music = settings.media.youtube_music.model_copy(
        update={"app_path": two, "bundle_id": "com.apple.Safari.WebApp.two"}
    )
    assert app.discover()[0] == two
    info = two / "Contents/Info.plist"
    value = plistlib.loads(info.read_bytes())
    value["CFBundleIdentifier"] = "com.apple.Safari.WebApp.reinstalled"
    info.write_bytes(plistlib.dumps(value))
    assert app.discover() == (two, value["CFBundleIdentifier"])


def test_moved_native_selection_follows_its_bundle_id(tmp_path, monkeypatch):
    path = app_bundle(tmp_path / "original", "example.selected.music", native=True)
    settings, app = music(monkeypatch)
    settings.media.youtube_music.app_path = path
    settings.media.youtube_music.bundle_id = "example.selected.music"
    assert app.discover()[0] == path
    destination = tmp_path / "moved.app"
    path.rename(destination)
    monkeypatch.setattr(SafariMusic, "application_paths", lambda _: [destination])
    assert app.discover() == (destination, "example.selected.music")


def test_spotlight_finds_moved_bundle_without_opening_any_application(tmp_path, monkeypatch):
    import backend.media.safari as module

    selected = app_bundle(tmp_path, "com.apple.Safari.WebApp.moved")
    settings = Settings()
    settings.media.youtube_music.bundle_id = "com.apple.Safari.WebApp.moved"
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        from types import SimpleNamespace

        return SimpleNamespace(returncode=0, stdout=str(selected) + "\n")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.sys, "platform", "darwin")
    assert selected in SafariMusic(settings).application_paths()
    assert calls[0][0] == [
        "/usr/bin/mdfind",
        "kMDItemCFBundleIdentifier == 'com.apple.Safari.WebApp.moved'",
    ]
    assert calls[0][1]["timeout"] == 3


def test_missing_app_and_invalid_plist_are_controlled_errors(tmp_path, monkeypatch):
    _, app = music(monkeypatch)
    with pytest.raises(MusicAppNotFound):
        app.discover()
    bad = app_bundle(tmp_path)
    (bad / "Contents/Info.plist").write_bytes(plistlib.dumps(["invalid"]))
    with pytest.raises(ToolError, match="идентификатор"):
        app.application(bad)


def test_music_waits_for_async_library_links_before_navigating(monkeypatch):
    _, app = music(monkeypatch)
    app.discover = lambda: (None, "test.music")
    app.request = AsyncMock(return_value={"running": True})
    app.act = AsyncMock()
    url = "https://music.youtube.com/playlist?list=LM"
    library = {"role": "AXWebArea", "url": "https://music.youtube.com/library"}
    link = {"role": "AXLink", "url": url, "title": "Понравившиеся", "path": [0, 1]}
    app.snapshot = AsyncMock(
        side_effect=[
            [library],
            [library, link],
            [{"role": "AXWebArea", "url": url}],
        ]
    )
    monkeypatch.setattr("backend.media.safari.asyncio.sleep", AsyncMock())
    asyncio.run(app.navigate(url))
    app.act.assert_awaited_once_with(link)
    assert app.snapshot.await_count == 3


def test_playlist_navigation_selects_the_library_playlist_filter_once(monkeypatch):
    _, app = music(monkeypatch)
    app.discover = lambda: (None, "test.music")
    app.request = AsyncMock(return_value={"running": True})
    app.act = AsyncMock()
    url = "https://music.youtube.com/playlist?list=LM"
    library = {"role": "AXWebArea", "url": "https://music.youtube.com/library"}
    selector = {"role": "AXRadioButton", "title": "Плейлисты", "value": "0", "path": [1]}
    link = {"role": "AXLink", "url": url, "title": "Понравившиеся", "path": [2]}
    app.snapshot = AsyncMock(
        side_effect=[
            [library, selector],
            [library, selector],
            [library, link],
            [{"role": "AXWebArea", "url": url}],
        ]
    )
    monkeypatch.setattr("backend.media.safari.asyncio.sleep", AsyncMock())
    asyncio.run(app.navigate(url))
    assert [call.args[0] for call in app.act.await_args_list] == [selector, link]


def test_ax_path_refresh_retries_only_a_rejected_precondition(monkeypatch):
    _, app = music(monkeypatch)
    old = {"role": "AXRadioButton", "title": "Плейлисты", "path": [1]}
    fresh = {**old, "path": [2]}
    app.request = AsyncMock(side_effect=[ElementChanged("changed before press"), {}])
    app.snapshot = AsyncMock(return_value=[fresh])
    asyncio.run(app.act(old))
    assert [call.kwargs["path"] for call in app.request.await_args_list] == [[1], [2]]
    app.request = AsyncMock(side_effect=ToolError("AX action not confirmed"))
    with pytest.raises(ToolError):
        asyncio.run(app.act(old))
    assert app.request.await_count == 1


@pytest.mark.parametrize(
    "value,seconds",
    [
        ("9:59 / 12:00", 599),
        ("10:00 / 12:00", 600),
        ("1:00:01", 3601),
        ("live", None),
        ("0:99", None),
        (None, None),
        (True, None),
    ],
)
def test_ax_clock_is_parsed_as_seconds(value, seconds):
    assert playback_seconds(value) == seconds


def test_track_clock_crosses_minutes_and_resets_after_pausing(monkeypatch):
    _, app = music(monkeypatch)
    monkeypatch.setattr("backend.media.safari.asyncio.sleep", AsyncMock())
    states = [
        {"state": state, "track": "Song", "position": position}
        for state, position in [
            ("playing", "9:58 / 12:00"),
            ("paused", "9:58 / 12:00"),
            ("playing", "9:59 / 12:00"),
            ("playing", "10:00 / 12:00"),
        ]
    ]
    app.now_playing = AsyncMock(side_effect=states)
    result = asyncio.run(app.wait_playing())
    assert app.now_playing.await_count == 4
    assert result["confirmation"]["position_after"] == "10:00 / 12:00"


def test_duration_change_alone_never_proves_playback(monkeypatch):
    _, app = music(monkeypatch)
    app.config.timeout_s = 0.01
    monkeypatch.setattr("backend.media.safari.asyncio.sleep", AsyncMock())
    states = iter(["0:01 / 3:20", "0:01 / 3:21"])
    app.now_playing = AsyncMock(
        side_effect=lambda: {
            "state": "playing",
            "track": "Song",
            "position": next(states, "0:01 / 3:21"),
        }
    )
    with pytest.raises(ToolError, match="не подтверждено"):
        asyncio.run(app.wait_playing())


@pytest.mark.parametrize(
    "values",
    [
        {"genres": ["мультфильм"], "hide_animation": True},
        {"genres": ["мультфильм", "драма"], "genre_mode": "and", "hide_animation": True},
        {"feed": "animation", "hide_animation": True},
        {"feed": "new", "year_max": 1900},
        {"year": 2025, "year_min": 2026},
        {"feed": "popular_movies", "kind": "series"},
    ],
)
def test_conflicting_filters_explain_empty_result_without_discover_requests(tmp_path, values):
    import httpx

    calls = []

    def response(request):
        calls.append(request.url.path)
        assert "/genre/" in request.url.path
        return httpx.Response(
            200, json={"genres": [{"id": 16, "name": "мультфильм"}, {"id": 18, "name": "драма"}]}
        )

    async def check():
        tmdb = api(tmp_path, response)
        try:
            result = await Catalogue(tmdb).page(CatalogueQuery(**{"kind": "movie", **values}))
            assert not result["items"] and result["next_page"] is None
            assert result["empty_reason"] == "incompatible_filters"
            assert result["filter_conflicts"]
        finally:
            await tmdb.close()

    asyncio.run(check())


def test_or_can_still_return_an_unhidden_genre(tmp_path):
    import httpx

    def response(request):
        return httpx.Response(
            200,
            json={"genres": [{"id": 16, "name": "мультфильм"}, {"id": 18, "name": "драма"}]}
            if "/genre/" in request.url.path
            else {"results": [movie(1, genre_ids=[18])], "total_pages": 1},
        )

    async def check():
        tmdb = api(tmp_path, response)
        try:
            result = await Catalogue(tmdb).page(
                CatalogueQuery(
                    kind="movie",
                    genres=["мультфильм", "драма"],
                    genre_mode="or",
                    hide_animation=True,
                )
            )
            assert len(result["items"]) == 1 and not result["filter_conflicts"]
        finally:
            await tmdb.close()

    asyncio.run(check())


def test_relaxed_search_can_explicitly_include_a_normally_hidden_genre(tmp_path):
    import httpx

    def response(request):
        return httpx.Response(
            200,
            json={"genres": [{"id": 16, "name": "мультфильм"}]}
            if "/genre/" in request.url.path
            else {"results": [movie(1, genre_ids=[16])], "total_pages": 1},
        )

    async def check():
        tmdb = api(tmp_path, response)
        try:
            result = await Catalogue(tmdb).page(
                CatalogueQuery(
                    kind="movie",
                    query="Film",
                    genres=["мультфильм"],
                    hide_animation=True,
                    relax_restrictions=True,
                )
            )
            assert len(result["items"]) == 1 and not result["filter_conflicts"]
        finally:
            await tmdb.close()

    asyncio.run(check())
