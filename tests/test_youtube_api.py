"""Official HTTP endpoints are mocked. No network, browser, audio or real memory writes."""

import asyncio
import random
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.config import YouTubeVideoConfig
from backend.media.youtube_api import (
    APIError,
    VideoLibrary,
    YouTubeAPI,
    duration_seconds,
    playlist_id,
    select_video,
    watch_url,
)
from backend.memory.store import MemoryStore
from backend.tools.registry import ToolError

PID = "PLfixture1234567890"
A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"


def video(vid=A, *, duration=7200, position=0, available=True, channel="Канал"):
    return dict(
        id=vid,
        title="Видео " + vid,
        duration=duration,
        position=position,
        available=available,
        channel=channel,
        channel_id="UCfixture",
        url=watch_url(vid),
    )


def dataset(videos=None):
    return dict(
        id=PID,
        title="Сон",
        videos=videos if videos is not None else [video()],
        fetched_at=time.time(),
        requests=3,
    )


@pytest.mark.parametrize(
    "value",
    [
        PID,
        f"https://www.youtube.com/playlist?list={PID}",
        f"https://youtube.com/watch?v={A}&list={PID}&index=3",
        f"https://m.youtube.com/playlist?list={PID}",
        f"https://youtu.be/{A}?list={PID}",
    ],
)
def test_playlist_parsing(value):
    assert playlist_id(value) == PID


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.test/playlist?list=" + PID,
        "https://youtube.com.evil.test/playlist?list=" + PID,
        "https://youtube.com@evil.test/playlist?list=" + PID,
        "https://music.youtube.com/playlist?list=" + PID,
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://youtube.com/playlist?list=" + PID,
        "https://youtube.com:443/playlist?list=" + PID,
        "https://youtube.com/playlist?list=" + PID + "&list=PLsecond12345",
        "../../somefile",
        "short",
    ],
)
def test_playlist_rejects_untrusted_input(value):
    with pytest.raises(ToolError):
        playlist_id(value)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("PT2H3M4S", 7384),
        ("PT30M", 1800),
        ("P1DT1H", 90000),
        ("PT0S", 0),
        ("P0D", 0),
        ("P", None),
        ("live", None),
    ],
)
def test_duration(value, expected):
    assert duration_seconds(value) == expected


def fixture_transport(calls, *, total=51, fail=None):
    def handler(request):
        calls.append(request)
        assert request.headers["x-goog-api-key"] == "fixture-only-key"
        assert "key=" not in str(request.url)
        assert request.url.host == "www.googleapis.com"
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if fail:
            return httpx.Response(fail, json={"error": {"errors": [{"reason": "forbidden"}]}})
        if endpoint == "playlists":
            return httpx.Response(200, json={"items": [{"snippet": {"title": "Для сна"}}]})
        if endpoint == "playlistItems":
            start = 50 if "pageToken" in request.url.params else 0
            items = [
                {
                    "snippet": {"title": "Video", "position": n},
                    "contentDetails": {"videoId": f"v{n:010d}"},
                }
                for n in range(start, min(start + 50, total))
            ]
            return httpx.Response(
                200,
                json={
                    "items": items,
                    **({"nextPageToken": "page2"} if total > 50 and start == 0 else {}),
                },
            )
        assert endpoint == "videos"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": vid,
                        "snippet": {"title": vid, "channelTitle": "Канал"},
                        "contentDetails": {"duration": "PT2H"},
                        "status": {"privacyStatus": "public", "uploadStatus": "processed"},
                    }
                    for vid in request.url.params["id"].split(",")
                    if vid != "v0000000003"
                ]
            },
        )

    return httpx.MockTransport(handler)


def test_all_pages_batched_metadata_and_unavailable_without_search(caplog):
    calls = []
    api = YouTubeAPI(YouTubeVideoConfig(api_key="fixture-only-key"), fixture_transport(calls))
    data = asyncio.run(api.fetch(PID))
    assert len(data["videos"]) == 51 and len(calls) == 5
    assert data["title"] == "Для сна" and data["videos"][50]["position"] == 50
    assert data["videos"][0]["duration"] == 7200
    assert not data["videos"][3]["available"]
    assert "search" not in {c.url.path.rsplit("/", 1)[-1] for c in calls}
    assert "fixture-only-key" not in caplog.text


def test_empty_playlist_uses_two_requests():
    calls = []
    api = YouTubeAPI(
        YouTubeVideoConfig(api_key="fixture-only-key"), fixture_transport(calls, total=0)
    )
    assert asyncio.run(api.fetch(PID))["videos"] == [] and len(calls) == 2


@pytest.mark.parametrize(
    "status,attempts",
    [(400, 1), (401, 1), (403, 1), (404, 1), (429, 1), (500, 2), (502, 2), (503, 2), (504, 2)],
)
def test_api_errors_are_bounded_and_sanitized(status, attempts):
    calls = []
    api = YouTubeAPI(
        YouTubeVideoConfig(api_key="fixture-only-key"), fixture_transport(calls, fail=status)
    )
    with pytest.raises(APIError) as exc:
        asyncio.run(api.fetch(PID))
    assert len(calls) == attempts
    assert "fixture-only-key" not in str(exc.value) and "https://" not in str(exc.value)
    assert exc.value.temporary == (status in {429, 500, 502, 503, 504})


def test_quota_exhaustion_cooldown_and_no_retry():
    calls = []

    def fail(request):
        calls.append(request)
        return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})

    api = YouTubeAPI(YouTubeVideoConfig(api_key="fixture-only-key"), httpx.MockTransport(fail))

    async def run():
        for _ in range(2):
            with pytest.raises(APIError, match="[Кк]вот"):
                await api.fetch(PID)

    asyncio.run(run())
    assert len(calls) == 1


def test_network_timeout_retries_once():
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("private request details must not escape", request=request)

    api = YouTubeAPI(YouTubeVideoConfig(api_key="fixture-only-key"), httpx.MockTransport(fail))
    with pytest.raises(APIError) as exc:
        asyncio.run(api.fetch(PID))
    assert len(calls) == 2 and "private" not in str(exc.value)


def test_missing_key_never_requests_gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-fixture-not-for-youtube")
    calls = []
    api = YouTubeAPI(YouTubeVideoConfig(), fixture_transport(calls))
    with pytest.raises(APIError, match="YOUTUBE_API_KEY"):
        asyncio.run(api.fetch(PID))
    assert not calls


def test_cache_ttl_force_stale_fatal_errors_and_restart(tmp_path):
    path = tmp_path / "cache.db"
    store = MemoryStore(path)
    api = AsyncMock()
    api.fetch.return_value = dataset()
    lib = VideoLibrary(store, YouTubeVideoConfig(cache_ttl=60), api)

    async def run():
        assert (await lib.load(PID))["cache"] == "updated"
        assert (await lib.load(PID))["cache"] == "fresh"
        assert api.fetch.await_count == 1
        await lib.load(PID, force=True)
        assert api.fetch.await_count == 2
        api.fetch.side_effect = APIError("temporary", temporary=True)
        assert (await lib.load(PID, force=True))["cache"] == "stale"
        api.fetch.side_effect = APIError("authorization")
        with pytest.raises(APIError, match="authorization"):
            await lib.load(PID, force=True)

    asyncio.run(run())
    store.close()
    other = VideoLibrary(MemoryStore(path), YouTubeVideoConfig(cache_ttl=60), AsyncMock())
    assert asyncio.run(other.load(PID))["title"] == "Сон"
    other.api.fetch.assert_not_awaited()


def test_request_budget_and_repeated_page_token():
    calls = []
    api = YouTubeAPI(
        YouTubeVideoConfig(api_key="fixture-only-key", max_requests=3), fixture_transport(calls)
    )
    with pytest.raises(APIError, match="лимит"):
        asyncio.run(api.fetch(PID))
    assert len(calls) == 3

    def repeat(request):
        if request.url.path.endswith("playlists"):
            return httpx.Response(200, json={"items": [{"snippet": {"title": "a"}}]})
        return httpx.Response(200, json={"items": [], "nextPageToken": "same"})

    api = YouTubeAPI(YouTubeVideoConfig(api_key="fixture-only-key"), httpx.MockTransport(repeat))
    with pytest.raises(APIError, match="пагинации"):
        asyncio.run(api.fetch(PID))
    assert api.requests == 3


def test_history_updates_without_false_completion_and_preserves_user_data(tmp_path):
    store = MemoryStore(tmp_path / "test.db")
    store.add_fact("старый факт")
    store.set_preference("youtube_music", "default", {"id": "music"})
    api = AsyncMock()
    api.fetch.return_value = dataset([video(), video(B, available=False)])
    lib = VideoLibrary(store, YouTubeVideoConfig(), api)
    asyncio.run(lib.load(PID))
    assert lib.history(PID)[A]["status"] == "unwatched"
    assert lib.history(PID)[B]["status"] == "unavailable"
    lib.mark(PID, A, "started", launched=True, source="test")
    lib.mark(PID, A, "partially_watched", progress=300)
    assert lib.history(PID)[A]["starts"] == 1
    lib.mark(PID, A, "started", launched=True)
    assert lib.history(PID)[A]["starts"] == 2
    lib.mark(PID, A, "watched")
    assert lib.history(PID)[A]["status"] == "watched"
    lib.mark(PID, A, "skipped")
    assert lib.history(PID)[A]["excluded"] == 1
    lib.mark(PID, A, "unwatched")
    assert lib.history(PID)[A]["excluded"] == 0
    lib.reset(PID)
    assert lib.history(PID)[A]["starts"] == 0
    assert lib.history(PID)[B]["status"] == "unavailable"
    assert store.facts()[0][1] == "старый факт"
    assert store.preference("youtube_music", "default") == {"id": "music"}


def test_selection_filters_order_random_and_exhaustion():
    data = dataset(
        [video(), video(B, duration=3600, position=1), video(C, duration=10800, position=2)]
    )
    history = {A: {"status": "watched"}}
    assert select_video(data, history, mode="longest")["id"] == C
    assert select_video(data, history, mode="shortest")["id"] == B
    assert select_video(data, history, mode="order")["id"] == B
    assert select_video(data, history, min_seconds=7000, max_seconds=11000)["id"] == C
    rng = random.Random(1)
    assert {select_video(data, history, rng=rng)["id"] for _ in range(30)} == {B, C}
    for vid in [B, C]:
        history[vid] = {"status": "started"}
    with pytest.raises(ToolError, match="повтор"):
        select_video(data, history)
    assert select_video(data, history, allow_repeat=True)["id"] in {A, B, C}
    with pytest.raises(ToolError, match="Фильтры"):
        select_video(data, history, channel="Другой")
    assert select_video(data, {}, mode="order", after_position=0)["id"] == B
    with pytest.raises(ToolError):
        select_video(dataset([]), {})
    with pytest.raises(ToolError):
        select_video(dataset([video(available=False), video(B, duration=None)]), {})
    with pytest.raises(ToolError):
        select_video(dataset(), {A: {"last_started": time.time()}})


def test_expired_cache_and_missing_api_key_are_not_masked(tmp_path):
    store = MemoryStore(tmp_path / "t.db")
    api = AsyncMock()
    api.fetch.return_value = dataset()
    lib = VideoLibrary(store, YouTubeVideoConfig(cache_ttl=60), api)

    async def run():
        await lib.load(PID)
        with store.db:
            store.db.execute("UPDATE youtube_playlist_cache SET fetched_at=?", (time.time() - 61,))
        await lib.load(PID)
        assert api.fetch.await_count == 2
        with store.db:
            store.db.execute(
                "UPDATE youtube_playlist_cache SET fetched_at=?", (time.time() - 31 * 86400,)
            )
        api.fetch.side_effect = APIError("temporary", temporary=True)
        with pytest.raises(APIError):
            await lib.load(PID)
        api.fetch.side_effect = APIError("missing key")
        with pytest.raises(APIError, match="missing key"):
            await lib.load(PID, force=True)

    asyncio.run(run())


def test_cache_refresh_never_erases_history_or_returns_partial_result(tmp_path):
    store = MemoryStore(tmp_path / "t.db")
    api = AsyncMock()
    api.fetch.return_value = dataset()
    lib = VideoLibrary(store, YouTubeVideoConfig(), api)

    async def run():
        await lib.load(PID)
        lib.mark(PID, A, "watched", launched=True)
        await lib.load(PID, force=True)
        assert lib.history(PID)[A]["status"] == "watched"
        api.fetch.side_effect = APIError("page two failed", temporary=True)
        out = await lib.load(PID, force=True)
        assert out["videos"] == dataset()["videos"]
        assert lib.history(PID)[A]["starts"] == 1

    asyncio.run(run())


def test_api_log_contains_only_safe_counters(caplog):
    import logging

    calls = []
    caplog.set_level(logging.INFO, logger="backend.media.youtube_api")
    api = YouTubeAPI(
        YouTubeVideoConfig(api_key="fixture-only-key"), fixture_transport(calls, total=0)
    )
    asyncio.run(api.fetch(PID))
    assert "estimated_quota_units=2" in caplog.text
    assert PID not in caplog.text and "fixture-only-key" not in caplog.text


def test_flat_env_settings_keep_key_secret(monkeypatch):
    from backend.config import Settings

    monkeypatch.setenv("YOUTUBE_API_KEY", "fixture-key-from-env")
    monkeypatch.setenv("YOUTUBE_SLEEP_PLAYLIST_URL", PID)
    monkeypatch.setenv("YOUTUBE_CACHE_TTL", "600")
    s = Settings()
    assert s.youtube_video.cache_ttl == 600 and s.youtube_video.playlist_url == PID
    assert s.youtube_video.api_key.get_secret_value() == "fixture-key-from-env"
    assert "fixture-key-from-env" not in repr(s.youtube_video)
    assert "fixture-key-from-env" not in s.model_dump_json()


def test_temporary_unavailability_preserves_manual_watched_state(tmp_path):
    api = AsyncMock()
    api.fetch.return_value = dataset()
    lib = VideoLibrary(MemoryStore(tmp_path / "t.db"), YouTubeVideoConfig(), api)

    async def run():
        await lib.load(PID)
        lib.mark(PID, A, "watched")
        api.fetch.return_value = dataset([video(available=False)])
        await lib.load(PID, force=True)
        assert lib.history(PID)[A]["status"] == "unavailable"
        api.fetch.return_value = dataset()
        await lib.load(PID, force=True)
        assert lib.history(PID)[A]["status"] == "watched"

    asyncio.run(run())
