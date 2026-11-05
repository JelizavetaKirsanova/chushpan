"""Regression boundaries only; real network/player results are documented separately."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.cinema.models import Preferences
from backend.cinema.native_player import AVPlayerAdapter
from backend.cinema.playback import SourceValidator, duration_matches
from backend.cinema.service import Cinema
from backend.cinema.sources import ArchiveAdapter, capabilities
from backend.config import Settings
from backend.context import DeviceContext
from backend.hud.bus import HudBus
from backend.mac_agent import LocalMacAgent
from backend.memory.store import MemoryStore
from backend.tools.registry import ToolError, ToolRegistry


@pytest.mark.parametrize("observed", [0, 30, 90, 500, float("nan"), float("inf")])
def test_full_work_duration_rejects_previews_and_invalid_samples(observed):
    assert not duration_matches(888.06, observed)


def test_provider_proof_requires_the_live_duration_even_with_matching_url():
    url = "https://example.org/full.mp4"
    proof = {"content_verified": True, "video_type": "full_movie", "duration": 888.06}
    for duration in (30, 90):
        result = SourceValidator.player(
            {"title": "Sintel", "kind": "movie", "year": 2010},
            {"live": True, "media_url": url, "duration": duration},
            url,
            proof,
        )
        assert not result["content_verified"] and "превью" in result["classification_reason"]
    assert duration_matches(888.06, 888.02)


def test_native_preview_is_rejected_before_seek_or_play(monkeypatch):
    async def run():
        adapter = AVPlayerAdapter(Settings())
        adapter.helper = AsyncMock(return_value=Path("/test/cinema-player"))
        adapter.request = AsyncMock()
        adapter.sample = AsyncMock(return_value={"ready": 4, "position": 0, "duration": 30})
        adapter.control = AsyncMock()
        monkeypatch.setattr(
            "backend.cinema.native_player.asyncio.create_subprocess_exec", AsyncMock()
        )
        with pytest.raises(ToolError) as failure:
            await adapter.open(
                {
                    "item": {"title": "Sintel"},
                    "episode": {"position": 0},
                    "link": {"url": "https://example.org/full.mp4"},
                    "direct_verification": {"duration": 888.06},
                }
            )
        assert failure.value.code == "duration_mismatch"
        adapter.control.assert_not_awaited()

    asyncio.run(run())


def test_http_200_does_not_mean_a_working_player_and_capabilities_are_declared():
    source = {"name": "Archive", "base_url": "https://archive.org", "search_mode": "archive"}
    assert capabilities(source)["verification_status"] == "not_verified"
    result = capabilities({**source, "health": {"status": "available", "successes": 0}})
    assert result["verification_status"] == "partial"
    assert result["capabilities_verified"] is False
    assert (
        capabilities({**source, "health": {"reason": "HTTP 401: auth"}})["verification_status"]
        == "requires_authorization"
    )
    assert not Preferences().open_in_safari
    assert Preferences(open_in_safari=True).open_in_safari  # Preserve an explicit choice.


def test_episode_inventory_does_not_fabricate_arbitrary_series_links():
    adapter = ArchiveAdapter({}, None)
    rows = asyncio.run(adapter.getEpisodes({"title": "Каминандес", "year": 2013}))
    assert [r["episode"] for r in rows] == [1, 2, 3]
    assert all(r["status"] == "not_verified" for r in rows)
    assert asyncio.run(adapter.getEpisodes({"title": "Other series", "year": 2013})) == []


def test_explicit_embedded_request_cannot_silently_open_a_website(tmp_path):
    async def run():
        memory = MemoryStore(tmp_path / "cinema.sqlite")
        tools = ToolRegistry()
        tools.mac_agent = LocalMacAgent()
        opener = AsyncMock()
        cinema = Cinema(tools, Settings(data_dir=tmp_path), memory, opener=opener)
        cinema.bind(HudBus(), SimpleNamespace(device=DeviceContext.local_mac()))
        try:
            source = await cinema.db(
                "save_source", {"name": "Website", "base_url": "https://example.org"}
            )
            item = await cinema.db(
                "add",
                {
                    "title": "Film",
                    "kind": "movie",
                    "source_id": source["id"],
                    "url": "https://example.org/film",
                },
            )
            with pytest.raises(ToolError) as failure:
                await cinema.open(item["id"], player="embedded")
            assert failure.value.code == "unsupported_source"
            opener.assert_not_awaited()
        finally:
            await cinema.close()
            memory.close()

    asyncio.run(run())
