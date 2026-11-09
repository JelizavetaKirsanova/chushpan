"""Opt-in, bounded local events. No transcripts, screenshots, URLs or keystrokes."""

import asyncio
import csv
import io
import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from datetime import time as wall_time
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import Field

from backend.brain.privacy import contains_secret
from backend.center import Center
from backend.personality import Strict

CATEGORIES = {
    "assistant": "Чушпан",
    "mac": "Активность приложений",
    "scenarios": "Сценарии",
    "devices": "Устройства",
    "media": "Медиатека",
}
MAX_EVENTS = 100000
_observing: ContextVar[frozenset[str]] = ContextVar("analytics_categories", default=frozenset())
SCHEMA = """CREATE TABLE IF NOT EXISTS analytics_events (
event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, timestamp REAL NOT NULL,
source TEXT NOT NULL, device_id TEXT NOT NULL, session_id TEXT, duration REAL,
metadata TEXT NOT NULL, privacy_level TEXT NOT NULL DEFAULT 'minimal');
CREATE INDEX IF NOT EXISTS analytics_time ON analytics_events(timestamp,device_id);
"""


class CollectionPolicy(Strict):
    enabled: bool = False
    categories: list[str] = Field(default_factory=lambda: ["assistant", "scenarios", "devices"])
    retention_days: int = Field(default=30, ge=1, le=3650)
    excluded_apps: list[str] = Field(default_factory=list, max_length=100)
    development_apps: list[str] = Field(default_factory=list, max_length=100)
    study_apps: list[str] = Field(default_factory=list, max_length=100)


def percentile(values, quantile):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * quantile) - 1)] if values else None


def period_bounds(value, timezone, now=None, oldest=None):
    zone = ZoneInfo(timezone)
    now = now or datetime.now(zone)
    today = now.astimezone(zone).date()
    days = {"today": 1, "week": 7, "month": 30, "90days": 90}
    if value.get("period", "today") in days:
        count = days[value.get("period", "today")]
        start_day, end_day = today - timedelta(days=count - 1), today + timedelta(days=1)
    elif value.get("period") == "all":
        start_day = datetime.fromtimestamp(oldest, zone).date() if oldest is not None else today
        end_day = today + timedelta(days=1)
        count = (end_day - start_day).days
    elif value.get("period") == "custom":
        start_day = datetime.strptime(value.get("start", ""), "%Y-%m-%d").date()
        end_day = datetime.strptime(value.get("end", ""), "%Y-%m-%d").date() + timedelta(days=1)
        count = (end_day - start_day).days
        if not 1 <= count <= 366:
            raise ValueError("Выбери период до 366 дней.")
    else:
        raise ValueError("Неизвестный период.")
    start = datetime.combine(start_day, wall_time(), zone).timestamp()
    end = datetime.combine(end_day, wall_time(), zone).timestamp()
    if value.get("period") == "all":
        return start, end, start
    previous = datetime.combine(start_day - timedelta(days=count), wall_time(), zone).timestamp()
    return start, end, previous


class Analytics(Center):
    channel = "analytics"

    def __init__(self, settings, store, registry, bus):
        self.s, self.store, self.tools = settings, store, registry
        store.db.executescript(SCHEMA)
        policy_error = None
        try:
            self.policy = CollectionPolicy.model_validate(
                store.preference("analytics", "policy") or {}
            )
        except ValueError:
            self.policy = CollectionPolicy()
            policy_error = (
                "Настройки аналитики повреждены. Сбор выключен; сохрани настройки заново."
            )
        self.queue = asyncio.Queue(maxsize=256)
        self.dropped, self.failure, self.query = 0, policy_error, {"period": "today"}
        self.generation = 0
        self.sampling_failure = None
        self.bind(bus)
        registry.analytics = self
        store.performance.analytics = self
        self.worker = asyncio.create_task(self.consume())
        self.sampler = asyncio.create_task(self.sample_apps())
        self.db_path = store.db.execute("PRAGMA database_list").fetchone()[2]

    def record(
        self, kind, *, duration=None, metadata=None, event_id=None, at=None, device="local-mac"
    ):
        category = kind.split(".", 1)[0]
        if (
            not self.store.memory_available
            or not self.policy.enabled
            or category not in self.policy.categories
        ):
            return
        if kind not in {
            "assistant.request",
            "assistant.recognition",
            "mac.activity",
            "mac.switch",
            "scenarios.run",
            "devices.command",
            "media.event",
        }:
            return
        allowed = {
            "bundle",
            "app",
            "provider",
            "model",
            "kind",
            "status",
            "tool",
            "category",
            "first_content_s",
            "stages",
            "scenario_id",
        }
        clean = {}
        for key, value in (metadata or {}).items():
            if key not in allowed:
                continue
            if isinstance(value, str) and len(value) <= 120 and not contains_secret(value):
                clean[key] = value
            elif type(value) in (int, float) and math.isfinite(value) and value >= 0:
                clean[key] = value
            elif key == "stages" and isinstance(value, dict):
                clean[key] = {
                    k: v
                    for k, v in value.items()
                    if re.fullmatch(r"[a-z_]{1,40}", k)
                    and type(v) in (int, float)
                    and math.isfinite(v)
                    and 0 <= v <= 86400
                }
        if duration is not None and (not math.isfinite(duration) or not 0 <= duration <= 86400):
            return
        event = (
            event_id or uuid4().hex,
            kind,
            at if at is not None else time.time(),
            category,
            device,
            None,
            duration,
            json.dumps(clean),
            "minimal",
        )
        try:
            self.queue.put_nowait((self.generation, event))
        except asyncio.QueueFull:
            self.dropped += 1

    def observe_trace(self, row):
        if row["status"] == "running":
            return
        created = datetime.fromisoformat(row["created_at"])
        timestamp = (created if created.tzinfo else created.replace(tzinfo=UTC)).timestamp()
        if row["kind"] == "activation":
            self.record(
                "assistant.recognition",
                event_id="trace:" + row["request_id"],
                at=timestamp,
                metadata={"status": row["status"]},
                device=row["metadata"].get("device_id", "local-mac"),
            )
            return
        self.record(
            "assistant.request",
            event_id="trace:" + row["request_id"],
            at=timestamp,
            duration=row.get("total_s"),
            device=row["metadata"].get("device_id", "local-mac"),
            metadata={
                "kind": row["kind"],
                "status": row["status"],
                "first_content_s": row.get("first_content_s"),
                "provider": row["metadata"].get("provider", "local"),
                "model": row["metadata"].get("model", ""),
                "stages": {s["stage"]: s["seconds"] for s in row["stages"]},
            },
        )

    @staticmethod
    def tool_category(name):
        if name == "run_scenario":
            return "scenarios.run"
        if name in {"led_control", "device_command", "set_brightness", "set_external_brightness"}:
            return "devices.command"
        if name.startswith("media_") or name in {"play_music", "music_command"}:
            return "media.event"
        return None

    @contextmanager
    def scope(self, name):
        """Count a device command once even when it delegates to another device tool."""
        category, parents = self.tool_category(name), _observing.get()
        token = _observing.set(parents | {category} if category else parents)
        try:
            yield category not in parents
        finally:
            _observing.reset(token)

    def observe_tool(self, name, result):
        kind = self.tool_category(name)
        if kind is None:
            return
        from backend.context import request_context

        context = request_context.get()
        self.record(
            kind,
            metadata={"tool": name, "status": result.get("result_status", "unverified")},
            device=context.device.device_id if context else "local-mac",
        )

    def write(self, batch, generation):
        if generation != self.generation:
            return
        db = sqlite3.connect(self.db_path, timeout=0.5) if self.db_path else self.store.db
        try:
            with db:
                db.executemany(
                    "INSERT OR REPLACE INTO analytics_events VALUES(?,?,?,?,?,?,?,?,?)", batch
                )
                db.execute(
                    "DELETE FROM analytics_events WHERE timestamp<?",
                    (time.time() - self.policy.retention_days * 86400,),
                )
        finally:
            if self.db_path:
                db.close()

    async def consume(self):
        while True:
            first = await self.queue.get()
            batch = [first]
            while not self.queue.empty() and len(batch) < 64:
                batch.append(self.queue.get_nowait())
            generation = self.generation
            try:
                events = [row for gen, row in batch if gen == generation]
                if events:
                    if self.db_path:
                        await asyncio.to_thread(self.write, events, generation)
                    else:
                        self.write(events, generation)
                    if (
                        self.failure
                        == "Часть событий не сохранена: локальная база занята или недоступна."
                    ):
                        self.failure = None
            except sqlite3.Error:
                self.failure = "Часть событий не сохранена: локальная база занята или недоступна."
                self.dropped += len(batch)
            finally:
                for _ in batch:
                    self.queue.task_done()

    async def sample_apps(self):
        previous, sampled = None, time.monotonic()
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            if not self.policy.enabled or "mac" not in self.policy.categories:
                self.sampling_failure = None
                previous, sampled = None, now
                continue
            try:
                status = await self.tools.desktop.bridge.request("desktop_activity_status")
                active = (await self.tools.desktop.apps("active")).get("apps", [])
                app = active[0] if len(active) == 1 and status.get("idle_s", 999) < 60 else None
                excluded = {a.casefold() for a in self.policy.excluded_apps}
                if app and {app["bundle"].casefold(), app["name"].casefold()} & excluded:
                    app = None
                if (
                    app
                    and previous
                    and previous["bundle"] == app["bundle"]
                    and 0 < now - sampled < 10
                ):
                    category = (
                        "development"
                        if app["bundle"] in self.policy.development_apps
                        else "study"
                        if app["bundle"] in self.policy.study_apps
                        else "other"
                    )
                    self.record(
                        "mac.activity",
                        duration=now - sampled,
                        metadata={
                            "app": app["name"],
                            "bundle": app["bundle"],
                            "category": category,
                        },
                    )
                elif app and previous:
                    self.record("mac.switch", metadata={"bundle": app["bundle"]})
                previous = app
                self.sampling_failure = None
            except Exception:
                previous = None
                self.sampling_failure = (
                    "Учёт приложений недоступен. Разрешения не запрашивались автоматически."
                )
            sampled = now

    async def flush(self):
        async with asyncio.timeout(5):
            await self.queue.join()

    def events(self, start, end, device=""):
        result = [
            {**dict(r), "metadata": json.loads(r["metadata"])}
            for r in self.store.db.execute(
                "SELECT * FROM analytics_events WHERE timestamp>=? AND timestamp<? "
                "AND (?='' OR device_id=?) ORDER BY timestamp LIMIT ?",
                (start, end, device, device, MAX_EVENTS + 1),
            )
        ]
        if len(result) > MAX_EVENTS:
            raise ValueError(
                "В периоде слишком много событий для отчёта. Сократи период или выбери устройство."
            )
        return result

    def oldest_event(self):
        if self.query.get("period") != "all":
            return None
        row = self.store.db.execute("SELECT MIN(timestamp) FROM analytics_events").fetchone()
        return row[0] if row else None

    def aggregate(self, events):
        requests = [r for r in events if r["event_type"] == "assistant.request"]
        latencies = [
            r["metadata"]["first_content_s"]
            for r in requests
            if r["metadata"].get("first_content_s") is not None
            and r["metadata"].get("status") == "completed"
        ]
        activity = [r for r in events if r["event_type"] == "mac.activity"]
        apps, categories, daily, providers, commands, stages = {}, {}, {}, {}, {}, {}
        for row in events:
            meta = row["metadata"]
            day = (
                datetime.fromtimestamp(row["timestamp"], ZoneInfo(self.s.location.tz_name))
                .date()
                .isoformat()
            )
            group = daily.setdefault(day, {"day": day, "requests": 0, "active_s": 0})
            if row["event_type"] == "assistant.request":
                group["requests"] += 1
                provider = meta.get("provider", "unknown")
                providers[provider] = providers.get(provider, 0) + 1
                for name, duration in meta.get("stages", {}).items():
                    stages.setdefault(name, []).append(duration)
            if row["event_type"] == "mac.activity":
                duration, bundle = row["duration"] or 0, meta.get("bundle", "unknown")
                app = apps.setdefault(
                    bundle, {"bundle": bundle, "name": meta.get("app", bundle), "seconds": 0}
                )
                app["seconds"] += duration
                group["active_s"] += duration
                category = meta.get("category", "other")
                categories[category] = categories.get(category, 0) + duration
            if meta.get("tool"):
                key = (row["event_type"], meta["tool"])
                command = commands.setdefault(
                    key,
                    {
                        "name": meta["tool"],
                        "category": row["event_type"],
                        "count": 0,
                        "failed": 0,
                        "confirmed": 0,
                    },
                )
                command["count"] += 1
                command["failed"] += meta.get("status") == "failed"
                command["confirmed"] += meta.get("status") == "success"
        return {
            "count": len(requests),
            "voice": sum(r["metadata"].get("kind") == "voice" for r in requests),
            "text": sum(r["metadata"].get("kind") == "text" for r in requests),
            "mean_s": sum(latencies) / len(latencies) if latencies else None,
            "p50_s": percentile(latencies, 0.5),
            "p95_s": percentile(latencies, 0.95),
            "failed": sum(r["metadata"].get("status") == "failed" for r in requests),
            "active_s": sum(r["duration"] or 0 for r in activity),
            "development_s": categories.get("development", 0),
            "study_s": categories.get("study", 0),
            "switches": sum(r["event_type"] == "mac.switch" for r in events),
            "apps": sorted(apps.values(), key=lambda r: -r["seconds"]),
            "categories": categories,
            "daily": list(daily.values()),
            "providers": providers,
            "commands": list(commands.values()),
            "stages": [
                {"name": name, "mean_s": sum(v) / len(v), "p95_s": percentile(v, 0.95)}
                for name, v in stages.items()
            ],
            "latencies": [
                {"at": r["timestamp"], "seconds": r["metadata"].get("first_content_s")}
                for r in requests[-100:]
            ],
            "events": len(events),
            "recognition_errors": sum(
                r["event_type"] == "assistant.recognition"
                and r["metadata"].get("status") == "failed"
                for r in events
            ),
        }

    def snapshot(self):
        start, end, previous = period_bounds(
            self.query, self.s.location.tz_name, oldest=self.oldest_event()
        )
        device = self.query.get("device", "")
        result = self.aggregate(self.events(start, end, device))
        before = self.aggregate(self.events(previous, start, device))
        insights = []
        if result["count"] and before["count"]:
            insights.append(
                f"Обращения: {result['count']} за выбранный период, "
                f"{before['count']} за предыдущий."
            )
        if result["mean_s"] is not None and before["mean_s"] is not None:
            insights.append(
                f"Средний первый ответ: {result['mean_s']:.2f} с; ранее {before['mean_s']:.2f} с."
            )
        if result["active_s"]:
            insights.append(
                "Активность измерена выборками по 5 секунд без ввода и содержимого"
                " окон. Категории задаёшь ты."
            )
        return {
            "summary": result,
            "previous": before,
            "policy": self.policy.model_dump(),
            "insights": insights,
            "dropped": self.dropped,
            "error": self.failure or self.sampling_failure,
            "query": self.query,
            "timezone": self.s.location.tz_name,
            "devices": [
                r[0]
                for r in self.store.db.execute(
                    "SELECT DISTINCT device_id FROM analytics_events LIMIT 100"
                )
            ],
            "categories": CATEGORIES,
        }

    async def handle(self, value):
        op = value.get("op", "query")
        await self.flush()
        if op == "policy":
            policy = CollectionPolicy.model_validate(value.get("value"))
            if set(policy.categories) - CATEGORIES.keys():
                raise ValueError("Неизвестная категория сбора.")
            for name in policy.excluded_apps + policy.development_apps + policy.study_apps:
                if not isinstance(name, str) or len(name) > 120 or contains_secret(name):
                    raise ValueError("Проверь список приложений.")
            self.store.set_preference("analytics", "policy", policy.model_dump())
            self.policy = policy
            self.failure = None
            if not policy.enabled or "mac" not in policy.categories:
                self.sampling_failure = None
            with self.store.memory_transaction():
                self.store.db.execute(
                    "DELETE FROM analytics_events WHERE timestamp<?",
                    (time.time() - policy.retention_days * 86400,),
                )
        elif op == "query":
            query = {k: value[k] for k in ("period", "start", "end", "device") if k in value}
            oldest = (
                self.store.db.execute("SELECT MIN(timestamp) FROM analytics_events").fetchone()[0]
                if query.get("period") == "all"
                else None
            )
            period_bounds(query, self.s.location.tz_name, oldest=oldest)
            self.query = query
        elif op == "clear":
            self.store._require_memory()
            if value.get("confirmed") is not True:
                raise ValueError("Подтверди удаление статистики.")
            self.generation += 1
            with self.store.memory_transaction():
                self.store.db.execute("DELETE FROM analytics_events")
        elif op == "export":
            if value.get("format", "json") not in {"csv", "json"}:
                raise ValueError("Доступен экспорт CSV или JSON.")
            start, end, _ = period_bounds(
                self.query, self.s.location.tz_name, oldest=self.oldest_event()
            )
            events = self.events(start, end, self.query.get("device", ""))
            if value.get("format") == "csv":
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(
                    ["event_id", "event_type", "timestamp_utc", "device", "duration_s", "metadata"]
                )
                for r in events:
                    writer.writerow(
                        [
                            r["event_id"],
                            r["event_type"],
                            datetime.fromtimestamp(r["timestamp"], UTC).isoformat(),
                            r["device_id"],
                            r["duration"],
                            json.dumps(r["metadata"], ensure_ascii=False),
                        ]
                    )
                download = {
                    "name": "chushpan-analytics.csv",
                    "mime": "text/csv",
                    "content": output.getvalue(),
                }
            else:
                download = {
                    "name": "chushpan-analytics.json",
                    "mime": "application/json",
                    "content": json.dumps(
                        {"schema": 1, "timezone": self.s.location.tz_name, "events": events},
                        ensure_ascii=False,
                        indent=2,
                    ),
                }
            return {"data": self.snapshot(), "download": download}
        else:
            raise ValueError("Неизвестное действие аналитики.")
        return {"data": self.snapshot()}

    async def close(self):
        await super().close()
        self.sampler.cancel()
        await asyncio.gather(self.sampler, return_exceptions=True)
        try:
            await self.flush()
        finally:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.tools.analytics = None
            self.store.performance.analytics = None
