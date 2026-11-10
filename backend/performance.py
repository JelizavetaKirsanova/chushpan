"""Persistent timing-only diagnostics over the existing SQLite store and local HUD bus."""

import json
import math
import sqlite3
from datetime import UTC

from backend.latency import PIPELINE_REVISION
from backend.statistics import dashboard, period_range

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


def model_statistics(traces):
    """Attribute failures and TTFT to each actual attempt, never to the final fallback."""
    groups = {}
    for trace in traces:
        attempts = [s for s in trace["stages"] if s["stage"] == "llm_attempt"]
        if not attempts:
            attempts = [s for s in trace["stages"] if s["stage"] == "llm_transport"]
        for stage in attempts:
            meta = stage["metadata"]
            key = (meta.get("provider"), meta.get("model"))
            if not all(key):
                continue
            group = groups.setdefault(key, {"rows": [], "requests": set(), "fallbacks": 0})
            group["requests"].add(trace["request_id"])
            group["fallbacks"] += bool(meta.get("fallback"))
            group["rows"].append(
                {
                    "status": "running"
                    if stage["outcome"] == "running"
                    else "completed"
                    if stage["outcome"] == "ok"
                    else "cancelled"
                    if stage["outcome"] == "cancelled"
                    else "failed",
                    "first_content_s": stage.get("first_result_s"),
                    "total_s": stage.get("active_s", stage["seconds"]),
                }
            )
    return [
        {
            "provider": provider,
            "model": model,
            "requests": len(group["requests"]),
            "attempts": len(group["rows"]),
            "fallbacks": group["fallbacks"],
            "first_token": aggregate(group["rows"]),
            "completion": aggregate(group["rows"], "total_s"),
            "status": "observed",  # Past successes cannot prove current availability.
        }
        for (provider, model), group in groups.items()
    ]


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
        if observer := getattr(self, "analytics", None):
            observer.observe_trace(row)
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

    def statistics(self, days, timezone, filters=None):
        filters = filters or {}
        oldest = None
        if days == -2:
            row = self.store.db.execute("SELECT MIN(created_at) FROM request_traces").fetchone()
            oldest = row[0] if row else None
        bounds = period_range(days, timezone, filters, oldest=oldest)
        start, end, before_start, before_end = bounds
        since, until = start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()
        rows = [
            dict(r)
            for r in self.store.db.execute(
                "SELECT request_id,created_at,kind,status,first_content_s,total_s,provider,data "
                "FROM request_traces WHERE created_at>=? AND created_at<? AND kind!='activation' "
                "ORDER BY created_at DESC",
                (since, until),
            )
        ]
        recent, traces = [], []
        for row in rows:
            trace = json.loads(row.pop("data"))
            traces.append(trace)
            row["metadata"] = trace["metadata"]
            if row["status"] == "running" and row["request_id"] not in self.active:
                row["status"] = trace["status"] = "unknown"
        filters = filters or {}

        def matches(row):
            return all(
                not value
                or (row.get("kind") if key == "kind" else row["metadata"].get(key)) == value
                for key, value in filters.items()
                if key in {"kind", "model", "request_class", "input_mode"}
            )

        options = {
            key: sorted({str(r["metadata"][key]) for r in rows if r["metadata"].get(key)})
            for key in ("model", "request_class", "input_mode")
        }
        rows = [row for row in rows if matches(row)]
        selected = {r["request_id"] for r in rows}
        traces = [t for t in traces if t["request_id"] in selected]
        ordered = (
            sorted(
                rows,
                key=lambda row: row["total_s"] if row["total_s"] is not None else -1,
                reverse=True,
            )
            if filters.get("sort") == "slow"
            else rows
        )
        for row in ordered[:100]:
            detail = self.detail(row["request_id"])
            recent.append({**row, "metadata": detail["metadata"] if detail else {}})
        activations = [
            self.detail(row[0])
            for row in self.store.db.execute(
                "SELECT request_id FROM request_traces WHERE kind='activation' "
                "AND created_at>=? AND created_at<? ORDER BY created_at DESC LIMIT 30",
                (since, until),
            )
        ]
        previous = [
            json.loads(row[0])
            for row in self.store.db.execute(
                "SELECT data FROM request_traces WHERE created_at>=? AND created_at<? "
                "AND kind!='activation'",
                (before_start.astimezone(UTC).isoformat(), before_end.astimezone(UTC).isoformat()),
            )
        ]
        previous = [trace for trace in previous if matches(trace)]
        collection_enabled = bool(
            (self.store.preference("analytics", "policy") or {}).get("enabled", False)
        )
        dashboard_data = dashboard(self.store.db, traces, previous, bounds, timezone)
        dashboard_data["collection_enabled"] = collection_enabled
        return {
            "dashboard": dashboard_data,
            "type": "performance_stats",
            "collection_enabled": collection_enabled,
            "days": days,
            "filters": filters,
            "filter_options": options,
            "models": model_statistics(traces),
            "series": [
                {k: row[k] for k in ("created_at", "kind", "first_content_s", "total_s", "status")}
                for row in reversed(rows)
            ],
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
                kind: aggregate([r for r in rows if kind == "all" or r["kind"] == kind], "total_s")
                for kind in ("voice", "text", "all")
            },
            "comparison": {
                kind: {
                    label: aggregate(
                        [
                            r
                            for r in rows
                            if (kind == "all" or r["kind"] == kind)
                            and (r["metadata"].get("pipeline_revision") == PIPELINE_REVISION)
                            == is_current
                        ]
                    )
                    for label, is_current in (("before", False), ("after", True))
                }
                for kind in ("voice", "text", "all")
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
            except (sqlite3.Error, ValueError):
                bus.publish(
                    {
                        "type": "performance_error",
                        "message": "Проверь период. Диагностика недоступна.",
                    }
                )

        def detail(identity):
            try:
                bus.publish({"type": "performance_detail", "trace": self.detail(identity)})
            except sqlite3.Error:
                bus.publish({"type": "performance_error", "message": "Трасса недоступна."})

        bus.handle("performance_stats", stats)

        def query(value):
            if not isinstance(value, dict) or type(value.get("days")) is not int:
                return
            filters = value.get("filters", {})
            if not isinstance(filters, dict) or any(
                k not in {"kind", "model", "request_class", "input_mode", "start", "end", "sort"}
                or not isinstance(v, str)
                or len(v) > 120
                for k, v in filters.items()
            ):
                return
            try:
                bus.publish(self.statistics(value["days"], timezone, filters))
            except (sqlite3.Error, ValueError):
                bus.publish(
                    {
                        "type": "performance_error",
                        "message": "Проверь период. Диагностика недоступна.",
                    }
                )

        bus.handle("performance_query", query)
        bus.handle("performance_detail", detail)
        for profile in self.active.values():
            self.changed(profile)
