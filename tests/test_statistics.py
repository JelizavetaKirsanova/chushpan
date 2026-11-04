"""Dashboard integration against saved data; no microphone, devices or external APIs."""

import json
from datetime import UTC, datetime

import pytest

from backend.memory.store import MemoryStore
from backend.statistics import period_range, stage_times


def save_trace(
    store,
    identity,
    at,
    *,
    status="completed",
    latency=2,
    category="routine",
    result=None,
    tool=None,
    total=4,
):
    stages = [{"stage": "stt", "start_s": 0, "end_s": 0.5}]
    if tool:
        stages.append(
            {"stage": "tool_execution", "start_s": 1, "end_s": 2, "metadata": {"tool": tool}}
        )
    meta = {"request_class": category, "model": "local-test"}
    if result:
        meta["result_status"] = result
    trace = {
        "request_id": identity,
        "created_at": at,
        "kind": "text",
        "status": status,
        "metadata": meta,
        "first_content_s": latency,
        "total_s": total,
        "marks": {},
        "stages": stages,
        "errors": [],
    }
    store.db.execute(
        "INSERT INTO request_traces VALUES(?,?,NULL,?,?,?,?,?,?)",
        (identity, at, "text", status, latency, total, None, json.dumps(trace)),
    )


def test_calendar_ranges_validate_custom_and_respect_dst():
    start, end, previous_start, previous_end = period_range(
        -1, "Europe/Tallinn", {"start": "2026-03-29", "end": "2026-03-29"}
    )
    assert (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() == 23 * 3600
    assert previous_start.date().isoformat() == "2026-03-28"
    assert previous_end == start
    with pytest.raises(ValueError):
        period_range(-1, "Europe/Tallinn", {"start": "2026-02-03", "end": "2026-02-01"})
    with pytest.raises(ValueError):
        period_range(-1, "UTC", {"start": "2020-01-01", "end": "2026-01-01"})
    today = period_range(1, "Europe/Tallinn", now=datetime(2026, 11, 5, 10, tzinfo=UTC))
    assert today[0].hour == 0 and today[1].hour == 12
    assert today[2].hour == 0 and today[3].hour == 12
    assert (period_range(365, "UTC")[1] - period_range(365, "UTC")[0]).days == 364


def test_dashboard_uses_complete_selected_history_previous_period_and_receipts(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    try:
        save_trace(store, "before", "2026-10-31T21:59:59+00:00", latency=8)
        save_trace(store, "talk", "2026-11-01T00:00:00+00:00", latency=2)
        save_trace(
            store,
            "failed",
            "2026-11-01T01:00:00+00:00",
            latency=0.01,
            category="command",
            status="failed",
            result="failed",
            tool="set_volume",
        )
        save_trace(
            store,
            "sent",
            "2026-11-01T02:00:00+00:00",
            category="command",
            result="unverified",
            tool="led_control",
        )
        save_trace(
            store,
            "confirmed",
            "2026-11-01T03:00:00+00:00",
            category="command",
            result="success",
            tool="set_volume",
        )
        save_trace(store, "after", "2026-11-01T22:00:00+00:00")
        store.db.executescript("CREATE TABLE scenario_runs(started_at TEXT, data TEXT);")
        receipt = {
            "id": "run",
            "name": "Вечер",
            "status": "success",
            "steps": [],
            "started_at": "2026-11-01T04:00:00+00:00",
        }
        store.db.execute(
            "INSERT INTO scenario_runs VALUES(?,?)", (receipt["started_at"], json.dumps(receipt))
        )
        changes = store.db.total_changes
        event = store.performance.statistics(
            -1, "Europe/Tallinn", {"start": "2026-11-01", "end": "2026-11-01"}
        )
        data = event["dashboard"]
        assert data["unit"] == "hour" and len(data["buckets"]) == 24
        assert data["metrics"]["requests"] == 4
        assert data["previous"]["requests"] == 1
        assert data["metrics"]["response_s"] == 2  # Failed response isn't a fast success.
        assert data["metrics"]["executed"] == 2  # Command and scenario receipt.
        assert data["metrics"]["known_operations"] == 3
        assert data["metrics"]["unconfirmed_operations"] == 1
        assert data["metrics"]["success_pct"] == pytest.approx(200 / 3)
        assert sum(data["distribution"].values()) == 4
        assert data["distribution"]["devices"] == 3
        assert data["scenarios"] == [receipt]
        assert data["buckets"][0]["requests"] == 0
        assert sum(b["requests"] for b in data["buckets"]) == 4
        assert store.db.total_changes == changes
        empty = store.performance.statistics(
            -1, "UTC", {"start": "2020-01-01", "end": "2020-01-01"}
        )["dashboard"]
        assert empty["metrics"]["response_s"] is None
        assert empty["metrics"]["success_pct"] is None
        assert empty["metrics"]["requests"] == 0
    finally:
        store.close()


def test_stage_series_preserves_missing_values_and_parallel_spans():
    assert stage_times({}) == dict.fromkeys(["stt", "llm", "tools", "tts", "total"])
    times = stage_times(
        {
            "stages": [
                {"stage": "model_total", "start_s": 1, "end_s": 7},
                {"stage": "tts_generation", "start_s": 2, "end_s": 4},
                {"stage": "tts_generation", "start_s": 3, "end_s": 5},
            ],
            "marks": {"first_llm_token": 1.25},
            "total_s": 8,
        }
    )
    assert times == {"stt": None, "llm": 0.25, "tools": None, "tts": 3, "total": 8}


def test_month_and_year_bucket_resolution_and_all_requests_available(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    try:
        for i in range(120):
            save_trace(store, str(i), "2026-11-01T12:00:00+00:00")
        save_trace(store, "old-slow", "2026-11-01T01:00:00+00:00", total=99)
        filters = {"start": "2026-11-01", "end": "2026-11-30"}
        month = store.performance.statistics(-1, "UTC", filters)["dashboard"]
        assert month["unit"] == "day" and len(month["buckets"]) == 30
        assert len(month["requests"]) == 121
        slow = store.performance.statistics(-1, "UTC", {**filters, "sort": "slow"})
        assert slow["requests"][0]["request_id"] == "old-slow"
        year = store.performance.statistics(
            -1, "UTC", {"start": "2026-01-01", "end": "2026-12-31"}
        )["dashboard"]
        assert year["unit"] == "month" and len(year["buckets"]) == 12
        filtered = store.performance.statistics(-1, "UTC", {**filters, "model": "different"})
        assert filtered["dashboard"]["metrics"]["requests"] == 0
    finally:
        store.close()
