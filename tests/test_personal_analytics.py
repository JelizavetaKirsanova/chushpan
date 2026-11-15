import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from backend.analytics import Analytics, CollectionPolicy, period_bounds
from backend.config import Settings
from backend.hud.bus import HudBus
from backend.memory.store import MemoryStore


def setup(tmp_path):
    store = MemoryStore(tmp_path / "analytics.sqlite")
    tools = SimpleNamespace(desktop=SimpleNamespace(bridge=SimpleNamespace(request=AsyncMock())))
    service = Analytics(Settings(location={"timezone": "Europe/Tallinn"}), store, tools, HudBus())
    # Feed deterministic NSWorkspace-shaped transitions instead of observing the test desktop.
    service.start_activity_observer = lambda: None
    return store, service


def test_disabled_collection_is_empty_and_never_probes_native(tmp_path):
    async def go():
        store, service = setup(tmp_path)
        try:
            service.record("assistant.request", metadata={"kind": "voice"})
            await service.flush()
            assert service.snapshot()["summary"]["count"] == 0
            assert service.snapshot()["summary"]["mean_s"] is None
            service.tools.desktop.bridge.request.assert_not_awaited()
        finally:
            await service.close()
            store.close()

    asyncio.run(go())


def test_events_metadata_percentiles_export_filters_retention_and_clear(tmp_path):
    async def go():
        store, service = setup(tmp_path)
        try:
            await service.handle(
                {"op": "policy", "value": {"enabled": True, "categories": ["assistant", "mac"]}}
            )
            for index, latency in enumerate((0, 1, 3, 8)):
                service.record(
                    "assistant.request",
                    event_id=str(index),
                    duration=10,
                    metadata={
                        "kind": "text",
                        "status": "completed",
                        "first_content_s": latency,
                        "content": "Private conversation",
                        "password": "forbidden",
                        "provider": "local",
                    },
                )
            service.record(
                "mac.activity",
                duration=5,
                device="other",
                metadata={"app": "Editor", "bundle": "example.editor", "category": "other"},
            )
            service.record(
                "assistant.request", at=time.time() - 86400 * 100, metadata={"kind": "text"}
            )
            await service.flush()
            assert store.db.execute("SELECT COUNT(*) FROM analytics_events").fetchone()[0] == 5
            data = service.snapshot()["summary"]
            assert data["count"] == 4 and data["mean_s"] == 3
            assert data["p50_s"] == 1 and data["p95_s"] == 8
            assert data["active_s"] == 5 and data["development_s"] == 0
            raw = (await service.handle({"op": "export"}))["download"]["content"]
            assert "Private conversation" not in raw and "password" not in raw
            assert len(json.loads(raw)["events"]) == 5
            filtered = await service.handle({"op": "query", "device": "other"})
            assert filtered["data"]["summary"]["count"] == 0
            assert (
                "timestamp_utc"
                in (await service.handle({"op": "export", "format": "csv"}))["download"]["content"]
            )
            with pytest.raises(ValueError, match="Подтверди"):
                await service.handle({"op": "clear"})
            await service.handle({"op": "clear", "confirmed": True})
            assert service.snapshot()["summary"]["events"] == 0
            assert store.db.execute("SELECT COUNT(*) FROM analytics_events").fetchone()[0] == 0
        finally:
            await service.close()
            store.close()

    asyncio.run(go())


def test_tallinn_midnight_and_dst_are_calendar_based():
    start, end, previous = period_bounds(
        {"period": "custom", "start": "2026-10-25", "end": "2026-10-25"}, "Europe/Tallinn"
    )
    assert end - start == 25 * 3600
    assert start - previous == 24 * 3600
    start, end, _ = period_bounds(
        {}, "Europe/Tallinn", datetime.fromisoformat("2026-11-05T00:30:00+02:00")
    )
    assert datetime.fromtimestamp(start, __import__("zoneinfo").ZoneInfo("Europe/Tallinn")).day == 5
    assert end - start == 86400
    start, end, previous = period_bounds(
        {"period": "90days"}, "Europe/Tallinn", datetime.fromisoformat("2026-11-09T12:00:00+02:00")
    )
    zone = __import__("zoneinfo").ZoneInfo("Europe/Tallinn")
    assert (
        datetime.fromtimestamp(end, zone).date() - datetime.fromtimestamp(start, zone).date()
    ).days == 90
    assert start - previous == 90 * 86400


def test_bounded_queue_drops_instead_of_blocking_voice(tmp_path):
    async def go():
        store, service = setup(tmp_path)
        try:
            service.policy = CollectionPolicy(enabled=True)
            for _ in range(300):
                service.record("assistant.request", metadata={"kind": "voice"})
            assert service.queue.qsize() == 256 and service.dropped == 44
            await service.flush()
        finally:
            await service.close()
            store.close()

    asyncio.run(go())


def test_foreground_activity_uses_switch_and_session_events_without_polling(tmp_path):
    async def go():
        store, service = setup(tmp_path)
        try:
            await service.handle(
                {
                    "op": "policy",
                    "value": {
                        "enabled": True,
                        "categories": ["mac"],
                        "development_apps": ["com.example.editor"],
                        "excluded_apps": ["com.example.private"],
                    },
                }
            )
            start = time.time() - 60
            service.activity_transition(
                {
                    "type": "activated",
                    "timestamp": start,
                    "bundle": "com.example.editor",
                    "app": "Editor",
                }
            )
            service.activity_transition(
                {
                    "type": "activated",
                    "timestamp": start + 12,
                    "bundle": "com.example.browser",
                    "app": "Browser",
                }
            )
            service.activity_transition(
                {
                    "type": "paused",
                    "timestamp": start + 32,
                }
            )
            service.activity_transition(
                {
                    "type": "activated",
                    "timestamp": start + 40,
                    "bundle": "com.example.private",
                    "app": "Private app",
                }
            )
            service.activity_transition(
                {
                    "type": "paused",
                    "timestamp": start + 52,
                }
            )
            await service.flush()
            rows = store.db.execute(
                "SELECT event_type,duration,metadata FROM analytics_events ORDER BY timestamp"
            ).fetchall()
            activity = [(row[1], json.loads(row[2])) for row in rows if row[0] == "mac.activity"]
            assert [(duration, metadata["category"]) for duration, metadata in activity] == [
                (12, "development"),
                (20, "other"),
            ]
            assert all("private" not in row[2] for row in rows)
            assert service.snapshot()["summary"]["switches"] == 1
            assert not hasattr(service, "sample_apps")
            service.tools.desktop.bridge.request.assert_not_awaited()
        finally:
            await service.close()
            store.close()

    asyncio.run(go())


def test_foreground_activity_is_split_at_local_midnight(tmp_path):
    async def go():
        store, service = setup(tmp_path)
        try:
            await service.handle(
                {"op": "policy", "value": {"enabled": True, "categories": ["mac"]}}
            )
            started = datetime.fromisoformat("2026-11-04T23:59:30+02:00").timestamp()
            service.activity_app = {"bundle": "com.example.editor", "name": "Editor"}
            service.activity_started = started
            service.finish_activity(started + 90)
            await service.flush()
            rows = store.db.execute(
                "SELECT timestamp,duration FROM analytics_events "
                "WHERE event_type='mac.activity' ORDER BY timestamp"
            ).fetchall()
            zone = ZoneInfo("Europe/Tallinn")
            assert [(datetime.fromtimestamp(row[0], zone).date(), row[1]) for row in rows] == [
                (datetime.fromtimestamp(started, zone).date(), 30),
                (datetime.fromtimestamp(started + 30, zone).date(), 60),
            ]
        finally:
            await service.close()
            store.close()

    asyncio.run(go())


def test_remote_trace_keeps_device_attribution_and_activation_is_not_a_request(tmp_path):
    async def go():
        store, service = setup(tmp_path)
        try:
            service.policy = CollectionPolicy(enabled=True)
            base = {
                "request_id": "remote",
                "created_at": datetime.now().astimezone().isoformat(),
                "kind": "text",
                "status": "completed",
                "total_s": 1,
                "first_content_s": 0.5,
                "metadata": {"device_id": "test-phone"},
                "stages": [],
            }
            service.observe_trace(base)
            service.observe_trace(
                {**base, "request_id": "wake", "kind": "activation", "status": "failed"}
            )
            await service.flush()
            service.query = {"period": "today", "device": "test-phone"}
            summary = service.snapshot()["summary"]
            assert summary["count"] == 1 and summary["recognition_errors"] == 1
        finally:
            await service.close()
            store.close()

    asyncio.run(go())
