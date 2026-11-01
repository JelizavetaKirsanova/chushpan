"""Content-free per-turn timings, shared with async child tasks."""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class Profile:
    stages: list[dict] = field(default_factory=list)

    def add(self, stage: str, seconds: float, outcome: str = "ok") -> None:
        self.stages.append({"stage": stage, "seconds": max(0, seconds), "outcome": outcome})

    def totals(self) -> dict[str, float]:
        totals = {}
        for row in self.stages:
            totals[row["stage"]] = totals.get(row["stage"], 0) + row["seconds"]
        return totals


current: ContextVar[Profile | None] = ContextVar("latency_profile", default=None)


def record(stage: str, seconds: float, outcome: str = "ok") -> None:
    if profile := current.get():
        profile.add(stage, seconds, outcome)


@contextmanager
def measure(stage: str):
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        if profile := current.get():
            profile.add(stage, time.perf_counter() - started, outcome)
