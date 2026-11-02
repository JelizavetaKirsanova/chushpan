"""Persistent timing-only diagnostics over the existing SQLite store and local HUD bus."""

import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from backend.latency import PIPELINE_REVISION

SCHEMA = """CREATE TABLE IF NOT EXISTS request_traces (
request_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, conversation_id INTEGER,
kind TEXT NOT NULL, status TEXT NOT NULL, first_content_s REAL, total_s REAL,
provider TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_trace_created ON request_traces(created_at);
"""


def aggregate(rows, metric="first_content_s"):
    samples = sorted(
        r[metric] for r in rows if r["status"] == "completed" and r.get(metric) is not None
    )
    n = len(samples)
    return {
        "count": len(rows),
        "measured": n,
        "failed": sum(r["status"] == "failed" for r in rows),
        "cancelled": sum(r["status"] in {"cancelled", "interrupted"} for r in rows),
        "incomplete": sum(r["status"] in {"unknown", "running"} for r in rows),
        "rejected": sum(r["status"] == "rejected" for r in rows),
        "awaiting_confirmation": sum(r["status"] == "awaiting_confirmation" for r in rows),
        "unmeasured": sum(r["status"] == "completed" and r.get(metric) is None for r in rows),
        "mean_s": sum(samples) / n if n else None,
        "median_s": (samples[(n - 1) // 2] + samples[n // 2]) / 2 if n else None,
        "p95_s": samples[math.ceil(n * 0.95) - 1] if n else None,
        "max_s": samples[-1] if n else None,
        "over": {str(t): 100 * sum(v > t for v in samples) / n if n else None for t in (5, 10, 15)},
    }


class PerformanceHistory:
    def __init__(self, store):
        self.store, self.bus = store, None
        self.active = {}
        store.db.executescript(SCHEMA)

    def close(self):
        for profile in list(self.active.values()):
            profile.finish("interrupted")

    def attach(self, profile, conversation_id=None):
        if profile.repository is not None:
            if conversation_id is not None:
                profile.update(conversation_id=conversation_id)
            return
        profile.repository = self
        if conversation_id is not None:
            profile.info["conversation_id"] = conversation_id
        self.active[profile.request_id] = profile
        profile.observer = self.changed
        self.save(profile)
        self.changed(profile)

    def changed(self, profile):
        if profile.ended is not None:
            self.save(profile)
            self.active.pop(profile.request_id, None)
        if self.bus:
            self.bus.publish({"type": "performance_live", "trace": profile.snapshot()})

    def save(self, profile):
        row = profile.snapshot()
        try:
            with self.store.memory_transaction():
                self.store.db.execute(
                    "INSERT OR REPLACE INTO request_traces VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        row["request_id"],
                        row["created_at"],
                        row["metadata"].get("conversation_id"),
                        row["kind"],
                        row["status"],
                        row["first_content_s"],
                        row["total_s"],
                        row["metadata"].get("provider"),
                        json.dumps(row, ensure_ascii=False),
                    ),
                )
        except sqlite3.Error:
            # Diagnostics must never prevent speech or overwrite conversation history.
            pass

    def detail(self, request_id):
        if request_id in self.active:
            return self.active[request_id].snapshot()
        row = self.store.db.execute(
            "SELECT status,data FROM request_traces WHERE request_id=?", (request_id,)
        ).fetchone()
        if not row:
            return None
        return {
            **json.loads(row["data"]),
            "status": "unknown" if row["status"] == "running" else row["status"],
        }

    def statistics(self, days, timezone):
        if days not in {1, 7, 30}:
            days = 1
        since = (
            (
                datetime.now(ZoneInfo(timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
                - timedelta(days=days - 1)
            )
            .astimezone(UTC)
            .isoformat()
        )
        rows = [
            dict(r)
            for r in self.store.db.execute(
                "SELECT request_id,created_at,kind,status,first_content_s,total_s,provider,data "
                "FROM request_traces WHERE created_at>=? AND kind!='activation' "
                "ORDER BY created_at DESC",
                (since,),
            )
        ]
        recent = []
        for row in rows:
            row["metadata"] = json.loads(row.pop("data"))["metadata"]
            if row["status"] == "running" and row["request_id"] not in self.active:
                row["status"] = "unknown"
        for row in rows[:100]:
            detail = self.detail(row["request_id"])
            recent.append({**row, "metadata": detail["metadata"] if detail else {}})
        activations = [
            self.detail(row[0])
            for row in self.store.db.execute(
                "SELECT request_id FROM request_traces WHERE kind='activation' "
                "AND created_at>=? ORDER BY created_at DESC LIMIT 30",
                (since,),
            )
        ]
        return {
            "type": "performance_stats",
            "days": days,
            "summary": aggregate(rows),
            "providers": {
                p: aggregate([r for r in rows if r["provider"] == p])
                for p in ("gemini", "ollama", None)
            },
            "kinds": {
                kind: aggregate([r for r in rows if r["kind"] == kind])
                for kind in ("voice", "text")
            },
            "completion": {
                kind: aggregate([r for r in rows if r["kind"] == kind], "total_s")
                for kind in ("voice", "text")
            },
            "comparison": {
                kind: {
                    label: aggregate(
                        [
                            r
                            for r in rows
                            if r["kind"] == kind
                            and (r["metadata"].get("pipeline_revision") == PIPELINE_REVISION)
                            == is_current
                        ]
                    )
                    for label, is_current in (("before", False), ("after", True))
                }
                for kind in ("voice", "text")
            },
            "benchmarks": self.benchmarks(),
            "requests": recent,
            "activations": activations,
            "active": max(self.active.values(), key=lambda p: p.origin).snapshot()
            if self.active
            else None,
        }

    def benchmarks(self):
        from pathlib import Path

        from backend.benchmark_reports import comparison

        path = self.store.db.execute("PRAGMA database_list").fetchone()[2]
        return comparison(Path(path).parent / "latency-benchmarks") if path else None

    def bind(self, bus, timezone):
        self.bus = bus

        def stats(days):
            try:
                bus.publish(self.statistics(days, timezone))
            except sqlite3.Error:
                bus.publish({"type": "performance_error", "message": "Диагностика недоступна."})

        def detail(identity):
            try:
                bus.publish({"type": "performance_detail", "trace": self.detail(identity)})
            except sqlite3.Error:
                bus.publish({"type": "performance_error", "message": "Трасса недоступна."})

        bus.handle("performance_stats", stats)
        bus.handle("performance_detail", detail)
        for profile in self.active.values():
            self.changed(profile)
