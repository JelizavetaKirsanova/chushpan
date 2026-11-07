"""Regression coverage for ownership, multi-signal verification and safe URL deduplication."""

import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from backend.cinema.browser_search import BrowserSearch
from backend.cinema.evidence import result_url
from backend.cinema.internet import PageVerifier, candidate_urls
from backend.tools.registry import ToolError


class Safari:
    """Minimal test double for only the app's marked tab, plus an existing user tab."""

    def __init__(self, result=None):
        self.result = result or {"empty": True}
        self.tabs = {"user": "https://example.org/user-tab"}
        self.open_count = 0
        self.opened = asyncio.Event()
        self.complete_open = asyncio.Event()
        self.complete_open.set()

    async def _run(self, script):
        marker = re.search(r"chushpan_session=([a-f0-9]{32})", script)[1]
        if "make new tab" in script:
            assert marker not in self.tabs
            self.tabs[marker] = "owned"
            self.open_count += 1
            self.opened.set()
            await self.complete_open.wait()
            return "73"
        if marker not in self.tabs:
            return "unowned"
        if "close ownedTab" in script:
            assert "front window" not in script and "current tab" not in script
            self.tabs.pop(marker)
            return "closed"
        if "return ownerID" in script:
            return "73"
        if "do JavaScript" in script:
            return json.dumps(self.result)
        return "sent"


def test_ten_empty_searches_reuse_one_window_per_operation_and_leave_user_tab():
    async def run():
        safari = Safari()
        search = BrowserSearch(safari, poll_seconds=0, polls=2)
        for _ in range(10):
            assert await search.discover(["Запрос", "Original query"], "EE", "ru") == []
            assert safari.tabs == {"user": "https://example.org/user-tab"}
            assert not search.sessions
        assert safari.open_count == 10

    asyncio.run(run())


def test_search_timeout_and_cancellation_during_safari_open_close_only_owned_tabs():
    async def run():
        safari = Safari(result={"loading": True})
        search = BrowserSearch(safari, poll_seconds=0, polls=1)
        with pytest.raises(ToolError, match="истекло время"):
            await search.search("Timeout", "EE", "en")
        assert list(safari.tabs) == ["user"]
        safari.opened.clear()
        safari.complete_open.clear()
        task = asyncio.create_task(search.search("Cancel", "EE", "en"))
        await safari.opened.wait()
        task.cancel()
        safari.complete_open.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await search.close()
        assert list(safari.tabs) == ["user"] and not search.sessions

    asyncio.run(run())


def test_series_coordinates_use_two_independent_signals_and_reject_wrong_episode():
    query = {
        "title": "Последнее королевство",
        "kind": "series",
        "year": 2015,
        "season": 1,
        "episode": 1,
        "metadata": {"original_title": "The Last Kingdom"},
    }
    page = "<h1>The Last Kingdom (2015), Season 1 Episode 1</h1><video></video>"
    url = "https://publisher.test/the-last-kingdom/season/1/episode/1"
    row = PageVerifier.inspect(page, url, query)
    assert row["scope"] == "episode" and row["episode_status"] == "confirmed"
    assert row["player_detected"] and not row["content_verified"]
    assert set(row["identity_signals"]) == {"заголовок", "адрес страницы"}
    single = PageVerifier.inspect(page, "https://publisher.test/the-last-kingdom", query)
    assert single["episode_status"] == "single_signal" and single["manual_candidate"]
    with pytest.raises(ToolError, match="другой сезон или серию"):
        PageVerifier.inspect(page, url[:-1] + "2", query)
    with pytest.raises(ToolError, match="главную"):
        PageVerifier.inspect(page, "https://publisher.test/", query)


def test_candidate_selection_keeps_distinct_domains_and_does_not_merge_episode_parameters():
    uploads = [f"https://uploads.test/video/{i}" for i in range(10)]
    publisher = "https://tv.apple.com/ee/episode/episode-1"
    result = candidate_urls([*uploads, "https://www.imdb.com/title/ref", publisher], limit=3)
    assert result == [publisher, *uploads[:2]]
    one = result_url("https://watch.test/page/?episode=1&utm_source=ads#top")
    assert one == result_url("https://watch.test/page?episode=1")
    assert one != result_url("https://watch.test/page?episode=2")


def test_new_hud_search_waits_for_previous_cancellation():
    from backend.cinema.service import Cinema

    async def run():
        context = SimpleNamespace(hud_search=None, cancel_search_task=Cinema.cancel_search_task)
        ready = asyncio.Event()

        async def work():
            ready.set()
            await asyncio.Event().wait()

        first = asyncio.create_task(Cinema.run_hud_search(context, "first", work))
        await ready.wait()
        second = asyncio.create_task(
            Cinema.run_hud_search(context, "second", lambda: asyncio.sleep(0))
        )
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        assert context.hud_search is None

    asyncio.run(run())
