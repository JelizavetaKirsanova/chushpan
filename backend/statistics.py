"""Read-only dashboard projections over saved timing traces and scenario receipts."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def period_range(days, timezone, filters=None, now=None, oldest=None):
    zone = ZoneInfo(timezone)
    now = (now or datetime.now(UTC)).astimezone(zone)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    filters = filters or {}
    if days == -1:
        start = datetime.strptime(filters.get("start", ""), "%Y-%m-%d").replace(tzinfo=zone)
        end = datetime.strptime(filters.get("end", ""), "%Y-%m-%d").replace(tzinfo=zone)
        end += timedelta(days=1)
        if not 1 <= (end.date() - start.date()).days <= 366:
            raise ValueError("Выбери от 1 до 366 дней.")
    elif days == 0:
        start, end = now - timedelta(hours=1), now
    elif days in {1, 7, 30, 90, 365}:
        start, end = midnight - timedelta(days=days - 1), now
    elif days == -2:
        if oldest:
            start = (
                datetime.fromisoformat(oldest)
                .astimezone(zone)
                .replace(hour=0, minute=0, second=0, microsecond=0)
            )
        else:
            start = midnight
        end = now
    else:
        raise ValueError("Неизвестный период.")
    # Compare equal local-calendar windows (including DST), up to the same time of day.
    shift = (
        timedelta(hours=1)
        if days == 0
        else timedelta(0)
        if days == -2
        else timedelta(days=((end.date() - start.date()).days if days == -1 else days))
    )
    if days == -2:
        return start, end, end, end
    return start, end, start - shift, end - shift


def stage_times(trace):
    """Missing instrumentation stays null; overlapping spans use interval union."""
    stages = trace.get("stages", [])

    def duration(names):
        spans = sorted(
            (s["start_s"], s["end_s"])
            for s in stages
            if s["stage"] in names and s.get("end_s") is not None
        )
        if not spans:
            return None
        total, left, right = 0, *spans[0]
        for start, end in spans[1:]:
            if start > right:
                total += right - left
                left, right = start, end
            else:
                right = max(right, end)
        return total + right - left

    model = next((s for s in stages if s["stage"] == "model_total"), None)
    token = trace.get("marks", {}).get("first_llm_token")
    return {
        "stt": duration({"stt"}),
        "llm": max(0, token - model["start_s"]) if token is not None and model else None,
        "tools": duration({"tool_execution", "local_execution"}),
        "tts": duration({"tts_generation"}),
        "total": trace.get("total_s"),
    }


def operation_category(trace):
    meta = trace.get("metadata", {})
    names = {str(s.get("metadata", {}).get("tool", "")) for s in trace.get("stages", [])}
    names.add(str(meta.get("tool", "")))
    if meta.get("request_class") == "scenario" or "run_scenario" in names:
        return "scenarios"
    if meta.get("intent") == "device_control" or names & {
        "device_command",
        "led_control",
        "set_brightness",
        "set_external_brightness",
        "set_volume",
    }:
        return "devices"
    if any(n.startswith(("search", "web_")) or n in {"weather", "news"} for n in names):
        return "search"
    if meta.get("request_class") in {"routine", "dialogue"} or (
        not any(names)
        and not any(
            s["stage"] in {"tool_execution", "local_execution"} for s in trace.get("stages", [])
        )
        and meta.get("request_class") not in {"command", "local_control", "confirmation"}
    ):
        return "conversations"
    return "other"


def dashboard(db, traces, previous, bounds, timezone):
    start, end, before_start, before_end = bounds
    zone = ZoneInfo(timezone)

    def runs(left, right):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='scenario_runs'").fetchone():
            return []
        return [
            json.loads(r[0])
            for r in db.execute(
                "SELECT data FROM scenario_runs WHERE started_at>=? AND started_at<? "
                "ORDER BY started_at DESC",
                (left.astimezone(UTC).isoformat(), right.astimezone(UTC).isoformat()),
            )
        ]

    current_runs, previous_runs = runs(start, end), runs(before_start, before_end)

    def metrics(rows, scenarios):
        commands = [r for r in rows if operation_category(r) in {"devices", "other", "search"}]
        outcomes = [r.get("metadata", {}).get("result_status") for r in commands]
        outcomes += [r["status"] for r in scenarios]
        known = [s for s in outcomes if s in {"success", "completed", "failed", "partial_success"}]
        success = sum(s in {"success", "completed"} for s in known)
        latency = [
            r["first_content_s"]
            for r in rows
            if r["status"] == "completed" and r.get("first_content_s") is not None
        ]
        return {
            "requests": len(rows),
            "response_s": sum(latency) / len(latency) if latency else None,
            "executed": success,
            "success_pct": 100 * success / len(known) if known else None,
            "known_operations": len(known),
            "unconfirmed_operations": len(outcomes) - len(known),
        }

    span = ((end - timedelta(microseconds=1)).date() - start.date()).days + 1
    unit = "hour" if span <= 1 else "month" if span > 90 else "day"

    def key(at):
        return at.strftime({"hour": "%Y-%m-%dT%H", "day": "%Y-%m-%d", "month": "%Y-%m"}[unit])

    buckets = {}
    cursor = start.replace(minute=0, second=0, microsecond=0)
    while cursor < end:
        bucket_key = key(cursor)
        buckets.setdefault(
            bucket_key,
            {
                "key": bucket_key,
                "label": cursor.strftime({"hour": "%H:00", "day": "%d.%m", "month": "%m.%Y"}[unit]),
                "requests": 0,
                "values": {name: [] for name in stage_times({})},
            },
        )
        cursor += timedelta(hours=1) if unit == "hour" else timedelta(days=1)
    distribution = dict.fromkeys(["conversations", "devices", "scenarios", "search", "other"], 0)
    diagnostics = []
    for trace in traces:
        times = stage_times(trace)
        category = operation_category(trace)
        distribution[category] += 1
        bucket = buckets.get(key(datetime.fromisoformat(trace["created_at"]).astimezone(zone)))
        if bucket:
            bucket["requests"] += 1
            if trace["status"] == "completed":
                for name, value in times.items():
                    if value is not None:
                        bucket["values"][name].append(value)
        diagnostics.append(
            {
                "request_id": trace["request_id"],
                "created_at": trace["created_at"],
                "kind": trace["kind"],
                "category": category,
                "status": trace["status"],
                "model": trace.get("metadata", {}).get("model"),
                "times": times,
                "errors": trace.get("errors", []),
            }
        )
    for bucket in buckets.values():
        bucket["latency"] = {
            k: sum(v) / len(v) if v else None for k, v in bucket.pop("values").items()
        }
    apps = []
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='analytics_events'").fetchone():
        # Respect collection policy: read already collected events, never start sampling.
        totals = {}
        for duration, metadata in db.execute(
            "SELECT duration,metadata FROM analytics_events WHERE event_type='mac.activity' "
            "AND timestamp>=? AND timestamp<?",
            (start.timestamp(), end.timestamp()),
        ):
            meta = json.loads(metadata)
            name = meta.get("app", meta.get("bundle", "Приложение"))
            totals[name] = totals.get(name, 0) + (duration or 0)
        apps = [
            {"name": name, "seconds": seconds}
            for name, seconds in sorted(totals.items(), key=lambda row: -row[1])
        ]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timezone": timezone,
        "unit": unit,
        "metrics": metrics(traces, current_runs),
        "previous": metrics(previous, previous_runs),
        "buckets": list(buckets.values()),
        "distribution": distribution,
        "requests": diagnostics,
        "scenarios": current_runs,
        "apps": apps,
    }
