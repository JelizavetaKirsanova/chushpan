"""No trailer substitution: real orchestration with isolated data and HTTP doubles."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.cinema.adapters import HTML5Adapter
from backend.cinema.classification import classify
from backend.cinema.commands import command
from backend.cinema.service import Cinema
from backend.cinema.tmdb import TMDB, TMDBCredentials
from backend.config import Settings
from backend.context import DeviceContext
from backend.hud.bus import HudBus
from backend.mac_agent import LocalMacAgent
from backend.memory.store import MemoryStore
from backend.tools.registry import ToolRegistry

QUERY = {
    "title": "Полная история",
    "kind": "movie",
    "year": 2025,
    "metadata": {"runtime": 2, "original_title": "Full Story"},
}
URL = "https://legal.example/film"
MEDIA = URL + "/full.mp4"


def work(**changes):
    return {
        "@type": "Movie",
        "name": "Полная история",
        "datePublished": "2025",
        "associatedMedia": {"@type": "VideoObject", "contentUrl": MEDIA},
        **changes,
    }


def evidence(entity=None, **changes):
    return {
        "live": True,
        "page_url": URL,
        "frame_url": URL,
        "media_url": MEDIA,
        "duration": 120,
        "structured": [work() if entity is None else entity],
        **changes,
    }


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Official trailer", "trailer"),
        ("Официальный тизер", "teaser"),
        ("Сцена из фильма", "clip"),
        ("Review", "review"),
    ],
)
def test_explicit_promo_labels_do_not_depend_on_duration(label, expected):
    result = classify(
        QUERY, evidence(work(associatedMedia={"name": label, "contentUrl": MEDIA}), duration=9000)
    )
    assert result["video_type"] == expected and not result["content_verified"]


def test_short_film_is_valid_but_matching_title_or_runtime_alone_is_not():
    assert classify(QUERY, evidence())["content_verified"]
    assert not classify({**QUERY, "year": 0}, evidence())["content_verified"]
    assert not classify(QUERY, evidence(work(associatedMedia={}), duration=120))["content_verified"]
    assert not classify(QUERY, evidence(work(name="Другая история")))["content_verified"]
    assert not classify(QUERY, evidence(work(datePublished="2024")))["content_verified"]
    assert not classify(QUERY, evidence(work(), media_url=URL + "/different.mp4"))[
        "content_verified"
    ]
    result = classify({**QUERY, "metadata": {"runtime": 110}}, evidence())
    assert result["video_type"] == "unknown" and not result["content_verified"]


def test_page_has_full_film_but_active_player_is_trailer():
    trailer = URL + "/preview.mp4"
    data = work(trailer={"@type": "VideoObject", "contentUrl": trailer})
    assert classify(QUERY, evidence(data))["content_verified"]
    result = classify(QUERY, evidence(data, media_url=trailer))
    assert result["video_type"] == "trailer" and not result["content_verified"]


def test_episode_needs_matching_series_season_episode_and_media():
    query = {**QUERY, "kind": "series", "season": 2, "episode": 3}
    data = {
        "@type": "TVEpisode",
        "name": "Серия 3",
        "partOfSeries": {"name": QUERY["title"], "datePublished": "2025"},
        "partOfSeason": {"seasonNumber": 2},
        "episodeNumber": 3,
        "encoding": {"contentUrl": MEDIA},
    }
    assert classify(query, evidence(data))["video_type"] == "episode"
    assert not classify({**query, "episode": 4}, evidence(data))["content_verified"]
    assert not classify({**query, "season": 1}, evidence(data))["content_verified"]


def test_trailer_command_is_separate_from_full_watch():
    assert command("смотреть трейлер")["action"] == "trailer"
    assert command("покажи трейлер фильма Интерстеллар")["title"] == "интерстеллар"
    assert command("не показывай трейлер") is None


def test_tmdb_trailer_without_a_full_source_never_opens_during_watch(tmp_path):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/videos"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "type": "Trailer",
                            "site": "YouTube",
                            "official": True,
                            "key": "abcdefghijk",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "id": 42,
                "title": QUERY["title"],
                "release_date": "2025-01-01",
                "overview": "Фильм",
                "runtime": 2,
            },
        )

    class Unavailable:
        async def request(self, *args, **kwargs):
            return httpx.Response(404)

    async def run():
        credentials = TMDBCredentials(tmp_path / "integrations")
        credentials.save("a" * 32)
        tmdb = TMDB(credentials, transport=httpx.MockTransport(handler), request_interval=0)
        assert (await tmdb.official_trailer("tmdb:movie:42"))["url"].endswith("abcdefghijk")
        requests.clear()
        memory = MemoryStore(tmp_path / "memory.sqlite")
        tools = ToolRegistry()
        tools.mac_agent = LocalMacAgent()
        opened = AsyncMock()
        player = SimpleNamespace(open=AsyncMock())
        cinema = Cinema(
            tools,
            Settings(data_dir=tmp_path / "data"),
            memory,
            tmdb=tmdb,
            opener=opened,
            adapter_factory=lambda _: player,
        )
        cinema.bind(HudBus(), SimpleNamespace(device=DeviceContext.local_mac()))
        cinema.sources.http = Unavailable()
        cinema.sources.web = None
        try:
            item = await cinema.db("add", await tmdb.detail("tmdb:movie:42"))
            episode = item["current"]
            await cinema.db("progress", episode["id"], 60, 120, "manual")
            result = await cinema.resume(item["id"])
            assert "Источник для просмотра не найден" in result["message"]
            assert not result["playback_confirmed"]
            result = await cinema.execute("смотреть фильм " + QUERY["title"])
            assert "Источник для просмотра не найден" in result["message"]
            opened.assert_not_awaited()
            player.open.assert_not_awaited()
            assert not any(path.endswith("/videos") for path in requests)
            assert (await cinema.db("detail", item["id"]))["current"]["position"] == 60
            history = await cinema.db("history")
            result = await cinema.trailer(title_id=item["id"])
            assert result["video_type"] == "trailer" and not result["playback_confirmed"]
            opened.assert_awaited_once_with("https://www.youtube.com/watch?v=abcdefghijk")
            assert await cinema.db("history") == history
        finally:
            await cinema.close()
            memory.close()
            await tmdb.close()

    asyncio.run(run())


def test_html5_trailer_is_rejected_before_seek_play_and_fullscreen():
    class Bridge:
        def __init__(self):
            self.actions = []

        async def ensure_page(self, *args):
            pass

        async def evaluate(self, url, request):
            self.actions.append(request["action"])
            return {
                "duration": 120,
                "position": 0,
                "paused": True,
                "binding": "fixture",
                "media_evidence": evidence(work(trailer={"contentUrl": MEDIA}, associatedMedia={})),
            }

    async def run():
        bridge = Bridge()
        adapter = HTML5Adapter(bridge)
        result = await adapter.open(
            {
                "item": QUERY,
                "link": {"url": URL},
                "episode": {"position": 90, "season": 0, "episode": 0},
            }
        )
        assert result["video_type"] == "trailer" and not result["playback_confirmed"]
        assert not {"play", "seek", "fullscreen", "offer_gesture"} & set(bridge.actions)

    asyncio.run(run())


def test_replacing_url_invalidates_verification_but_preserves_progress(tmp_path):
    from backend.cinema.store import CinemaStore

    store = CinemaStore(tmp_path / "library.sqlite")
    source = store.save_source({"name": "Источник", "base_url": "https://legal.example"})
    values = {
        **{k: QUERY[k] for k in ("title", "kind", "year")},
        "source_id": source["id"],
        "url": URL,
    }
    item = store.add(values)
    episode = item["current"]
    link = episode["links"][0]
    store.verify_link(link["id"], {"video_type": "trailer", "content_verified": False})
    store.progress(episode["id"], 20, 120, "manual", link["id"])
    store.playback_state(episode["id"], link["id"], 20, 120, True)
    history = store.history()
    updated = store.add({**values, "title_id": item["id"], "url": URL + "/corrected"})
    assert store.verify_link(link["id"]) == {}
    assert updated["current"]["position"] == 20
    assert not updated["current"]["playback_state"]["confirmed"]
    assert store.history() == history
    store.close()


@pytest.mark.parametrize(
    "text,kind",
    [
        ("Смотреть фильм Полная история", "movie"),
        ("Включи сериал Тестовый сериал", "series"),
        ("Открой фильм Full Story", "movie"),
    ],
)
def test_named_watch_is_routed_to_full_source_search(text, kind):
    parsed = command(text)
    assert parsed["action"] == "find_sources" and parsed["watch"]
    assert parsed["kind"] == kind


def test_generic_embedded_video_and_image_are_not_full_encoding():
    assert not classify(QUERY, evidence(work(associatedMedia={}, video={"contentUrl": MEDIA})))[
        "content_verified"
    ]
    assert not classify(
        QUERY, evidence(work(associatedMedia={"@type": "ImageObject", "url": MEDIA}))
    )["content_verified"]
