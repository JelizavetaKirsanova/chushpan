"""Content-free monotonic request traces, shared with async children and audio callbacks."""

from __future__ import annotations

import asyncio
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from backend.brain.privacy import contains_secret

_SAFE = re.compile(r"^[A-Za-z0-9_.:/-]{1,120}$")
PIPELINE_REVISION = "2026-11-06-voice-intelligence-2"
_META = {
    "pipeline_revision",
    "error_component",
    "error_line",
    "fragment_index",
    "stt_chars",
    "stt_result",
    "session_active",
    "continuation_window",
    "audio_duration_s",
    "recording_end",
    "speech_ms",
    "route",
    "request_class",
    "context_chars",
    "context_estimated_tokens",
    "context_messages",
    "tool_schema_chars",
    "tokens_per_second",
    "provider",
    "model",
    "requested_model",
    "tool",
    "attempt",
    "reason",
    "thinking",
    "thinking_level",
    "streaming",
    "route_reason",
    "intent",
    "target",
    "result_status",
    "context_used",
    "queue_depth",
    "timeout_s",
    "eval_count",
    "prompt_eval_count",
    "eval_duration",
    "prompt_eval_duration",
    "load_duration",
    "total_duration",
    "retry",
    "fallback",
    "conversation_id",
    "input_mode",
    "http_status",
    "device",
    "device_id",
    "action",
    "rgb",
    "brightness",
    "authority",
    "arguments_match",
    "input_chars",
    "normalized_chars",
    "delivery_state",
    "physical_confirmed",
    "wake_source",
    "wake_enabled",
    "max_no_speech_prob",
    "min_avg_logprob",
    "max_compression_ratio",
}


def metadata(values):
    return {
        key: value
        for key, value in values.items()
        if key in _META
        and (
            value is None
            or isinstance(value, (bool, int, float))
            or isinstance(value, str)
            and _SAFE.fullmatch(value)
            and not contains_secret(value)
        )
    }


@dataclass
class Profile:
    stages: list[dict] = field(default_factory=list)
    request_id: str = field(default_factory=lambda: uuid4().hex)
    origin: float = field(default_factory=time.perf_counter)
    kind: str = "text"
    status: str = "running"
    result_status: str | None = None
    marks: dict = field(default_factory=dict)
    info: dict = field(default_factory=lambda: {"pipeline_revision": PIPELINE_REVISION})
    errors: list[str] = field(default_factory=list)
    ended: float | None = None
    observer: object = field(default=None, repr=False)
    repository: object = field(default=None, repr=False)
    _next: int = 0
    _wall_time: datetime = field(default_factory=lambda: datetime.now(UTC), repr=False)
    _created_origin: float = field(init=False, repr=False)

    def __post_init__(self):
        self._created_origin = self.origin

    def elapsed(self):
        return max(0, (self.ended if self.ended is not None else time.perf_counter()) - self.origin)

    def update(self, **values):
        if self.ended is not None:
            return
        if "route" in values:
            values.setdefault(
                "route_reason",
                {
                    "llm": "routine_model"
                    if values.get("request_class") == "routine"
                    else "full_model",
                    "local_command": "deterministic_intent",
                    "confirmation": "confirmation_required",
                    "scenario": "registered_scenario",
                }.get(values["route"], values["route"]),
            )
        safe = metadata(values)
        if all(key in self.info and self.info[key] == value for key, value in safe.items()):
            return
        self.info.update(safe)
        self.publish()

    def mark(self, name, at=None, *, allow_before_origin=False):
        if self.ended is not None:
            return
        if name not in self.marks:
            offset = (time.perf_counter() if at is None else at) - self.origin
            self.marks[name] = offset if allow_before_origin else max(0, offset)
            self.publish()

    def begin(self, stage, *, at=None, **values):
        if self.ended is not None:
            return None
        self._next += 1
        if len(self.stages) >= 500:
            return None
        self.stages.append(
            {
                "id": self._next,
                "stage": stage,
                "start_s": self.elapsed() if at is None else max(0, at - self.origin),
                "end_s": None,
                "seconds": 0.0,
                "outcome": "running",
                "metadata": metadata(values),
            }
        )
        self.publish()
        return self._next

    def end(self, identity, outcome="ok", **values):
        row = next((r for r in self.stages if r["id"] == identity), None)
        if row and row["end_s"] is None:
            row["metadata"].update(metadata(values))
            row.update(end_s=self.elapsed(), outcome=outcome)
            row["seconds"] = max(0, row["end_s"] - row["start_s"])
            row["active_s"] = max(0, row["seconds"] - row.get("consumer_wait_s", 0))
            self.publish()

    def end_first(self, identity, **values):
        """First provider event for this attempt, including reasoning and partial streams."""
        row = next((r for r in self.stages if r["id"] == identity), None)
        if row is not None:
            safe = metadata(values)
            changed = any(row["metadata"].get(k) != v for k, v in safe.items())
            row["metadata"].update(safe)
            if "first_result_s" not in row:
                row["first_result_s"] = max(0, self.elapsed() - row["start_s"])
                changed = True
            if changed:
                self.publish()

    def interval(self, stage, start, end, outcome="ok"):
        identity = self.begin(stage)
        row = next((r for r in self.stages if r["id"] == identity), None)
        if row:
            row.update(
                start_s=max(0, start - self.origin),
                end_s=max(0, end - self.origin),
                seconds=max(0, end - start),
                outcome=outcome,
            )
            self.publish()

    def add(self, stage: str, seconds: float, outcome: str = "ok") -> None:
        end = self.elapsed()
        identity = self.begin(stage)
        row = next((r for r in self.stages if r["id"] == identity), None)
        if row:
            row.update(
                start_s=max(0, end - seconds), end_s=end, seconds=max(0, seconds), outcome=outcome
            )
            self.publish()

    def fail(self, code):
        if self.ended is not None:
            return
        code = code if isinstance(code, str) and _SAFE.fullmatch(code) else "error"
        if code not in self.errors:
            self.errors.append(code)
        self.publish()

    def finish(self, status="completed"):
        if self.ended is not None:
            return
        self.ended = time.perf_counter()
        self.status = (
            ("failed" if self.errors else self.result_status or status)
            if status == "completed"
            else status
        )
        for row in self.stages:
            if row["end_s"] is None:
                row.update(end_s=self.elapsed(), outcome=self.status)
                row["seconds"] = max(0, row["end_s"] - row["start_s"])
                row["active_s"] = max(0, row["seconds"] - row.get("consumer_wait_s", 0))
        self.publish()

    def publish(self):
        if self.observer:
            self.observer(self)

    def snapshot(self):
        at = self._wall_time + timedelta(seconds=self.origin - self._created_origin)
        first = self.marks.get(
            "first_content_audio" if self.kind == "voice" else "first_content_text"
        )
        waiting_end = max(
            (
                row["end_s"]
                for row in self.stages
                if row["stage"] == "waiting_playback"
                and row["end_s"] is not None
                and first is not None
                and row["end_s"] <= first
            ),
            default=None,
        )

        def between(start, end):
            a, b = self.marks.get(start), self.marks.get(end)
            return max(0, b - a) if a is not None and b is not None else None

        return {
            "request_id": self.request_id,
            "created_at": at.isoformat(),
            "kind": self.kind,
            "status": self.status,
            "elapsed_s": self.elapsed(),
            "total_s": self.elapsed() if self.ended is not None else None,
            "first_content_s": first,
            "waiting_to_content_gap_s": first - waiting_end if waiting_end is not None else None,
            "first_audio_s": min(
                (
                    self.marks[name]
                    for name in ("waiting_audio", "first_content_audio")
                    if name in self.marks
                ),
                default=None,
            ),
            "marks": dict(self.marks),
            "metrics": {
                **self.summary(),
                "speech_end_to_stt_s": between("user_speech_end", "stt_completed"),
                "stt_to_first_token_s": between("stt_completed", "first_llm_token"),
                "first_token_to_audio_s": between("first_llm_token", "first_content_audio"),
                "speech_end_to_audio_s": between("user_speech_end", "first_content_audio"),
                "speech_end_to_completed_s": max(
                    0, self.elapsed() - self.marks["user_speech_end"]
                )
                if self.ended is not None and "user_speech_end" in self.marks
                else None,
            },
            "metadata": dict(self.info),
            "errors": list(self.errors),
            "stages": [{**r, "metadata": dict(r["metadata"])} for r in self.stages],
        }

    def totals(self) -> dict[str, float]:
        totals = {}
        for row in self.stages:
            seconds = (
                max(0, self.elapsed() - row["start_s"])
                if "end_s" in row and row["end_s"] is None
                else row["seconds"]
            )
            totals[row["stage"]] = totals.get(row["stage"], 0) + seconds
        return totals

    def summary(self) -> dict[str, float | None]:
        totals = self.totals()

        def first(stage):
            return next((row["seconds"] for row in self.stages if row["stage"] == stage), None)

        return {
            "wake_s": totals.get("wake_detection"),
            "vad_onset_s": first("vad_onset"),
            "vad_endpoint_s": totals.get("vad_wait"),
            "routing_s": totals.get("local_routing"),
            "stt_s": totals.get("stt"),
            "llm_first_token_s": first("model_first_chunk"),
            "llm_first_text_s": first("model_first_text"),
            "llm_total_s": sum(
                row.get("active_s", row["seconds"])
                for row in self.stages
                if row["stage"] == "model_total"
            )
            if "model_total" in totals
            else None,
            "tool_execution_s": totals.get("tool_execution"),
            "tts_first_audio_s": first("tts_first_audio"),
            "total_response_s": totals.get("response_total"),
        }


current: ContextVar[Profile | None] = ContextVar("latency_profile", default=None)


@contextmanager
def consumer_wait(identity):
    profile, started = current.get(), time.perf_counter()
    try:
        yield
    finally:
        if profile and profile.ended is None:
            row = next((r for r in profile.stages if r["id"] == identity), None)
            if row is not None:
                row["consumer_wait_s"] = row.get("consumer_wait_s", 0) + max(
                    0, time.perf_counter() - started
                )


@contextmanager
def pause_deadline(deadline, identity=None):
    """Consumer/TTS backpressure is not a provider network stall."""
    loop = asyncio.get_running_loop()
    remaining = max(0, deadline.when() - loop.time()) if deadline.when() is not None else None
    deadline.reschedule(None)
    try:
        with consumer_wait(identity):
            yield
    finally:
        if remaining is not None and not deadline.expired():
            deadline.reschedule(loop.time() + remaining)


def record(stage: str, seconds: float, outcome: str = "ok") -> None:
    if profile := current.get():
        profile.add(stage, seconds, outcome)


@contextmanager
def measure(stage: str, **values):
    profile = current.get()
    identity = profile.begin(stage, **values) if profile else None
    outcome = "ok"
    try:
        yield
    except BaseException as exc:
        outcome = (
            "cancelled" if type(exc).__name__ in {"CancelledError", "GeneratorExit"} else "error"
        )
        raise
    finally:
        if profile:
            profile.end(identity, outcome)
