"""Assistant state machine: sleeping → listening → thinking → speaking."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextvars import ContextVar
from enum import StrEnum

request_context: ContextVar[int | None] = ContextVar("voice_request", default=None)


class State(StrEnum):
    SLEEPING = "sleeping"  # waiting for the wake word
    LISTENING = "listening"  # waiting for, or recording, a request
    THINKING = "thinking"  # transcribing and waiting for the model
    SPEAKING = "speaking"


TRANSITIONS: dict[State, set[State]] = {
    # Straight to speaking for unprompted announcements (a timer going off).
    State.SLEEPING: {State.LISTENING, State.SPEAKING},
    State.LISTENING: {State.SLEEPING, State.THINKING, State.SPEAKING},
    # Back to listening when the turn ends, fails or is interrupted.
    State.THINKING: {State.SPEAKING, State.LISTENING},
    State.SPEAKING: {State.LISTENING},
}

Listener = Callable[[State, State], None]


class InvalidTransition(RuntimeError):
    pass


class StateMachine:
    def __init__(self, initial: State, clock: Callable[[], float] = time.monotonic) -> None:
        self.state = initial
        self._clock = clock
        self.since = clock()
        self._listeners: list[Listener] = []
        self.request_id = 0
        self.phase = "idle" if initial is State.SLEEPING else "listening"

    def begin_request(self) -> int:
        self.request_id += 1
        return self.request_id

    def set_phase(self, phase: str, request_id: int) -> bool:
        if request_id != self.request_id:
            return False
        self.phase = phase
        return True

    def add_listener(self, listener: Listener) -> None:
        """Called with (old, new) on every change; the HUD will subscribe here."""
        self._listeners.append(listener)

    def to(self, new: State) -> bool:
        """Move to `new`. Returns False if already there."""
        old = self.state
        if new == old:
            return False
        if new not in TRANSITIONS[old]:
            raise InvalidTransition(f"{old} → {new}")
        self.state, self.since = new, self._clock()
        self.phase = {
            State.SLEEPING: "idle",
            State.LISTENING: "listening",
            State.THINKING: "processing",
            State.SPEAKING: "speaking",
        }[new]
        for listener in self._listeners:
            listener(old, new)
        return True

    @property
    def elapsed(self) -> float:
        return self._clock() - self.since
