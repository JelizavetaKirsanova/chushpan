"""Server-issued request provenance; client device IDs are descriptive, never grants."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

MAC_CAPABILITIES = frozenset(
    {
        "mac.system",
        "mac.apps",
        "mac.media",
        "mac.personal",
        "mac.windows",
        "mac.screen",
        "mac.files",
        "mac.shortcuts",
    }
)

# A contextual continuation alone grants no system action; matched READ proposals are scoped below.
voice_addressed: ContextVar[bool] = ContextVar("voice_addressed", default=True)
voice_music_command: ContextVar[str | None] = ContextVar("voice_music_command", default=None)
voice_read_followup: ContextVar[tuple[str, str] | None] = ContextVar(
    "voice_read_followup", default=None
)


@dataclass(frozen=True)
class DeviceContext:
    device_id: str
    device_type: Literal["mac", "iphone", "ipad", "speaker", "server"]
    session_id: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    authenticated: bool = False
    target_agent_id: str | None = None

    @classmethod
    def local_mac(cls, session_id: str | None = None) -> DeviceContext:
        """Only the trusted desktop entry point issues this identity, never a payload."""
        return cls(
            "local-mac", "mac", session_id or uuid4().hex, MAC_CAPABILITIES, True, "local-mac"
        )


@dataclass(frozen=True)
class RequestContext:
    device: DeviceContext
    text: str
    request_id: str = field(default_factory=lambda: uuid4().hex)

    @property
    def binding(self) -> tuple[str, str]:
        return self.device.device_id, self.device.session_id


request_context: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)


@contextmanager
def request_scope(context: RequestContext):
    token = request_context.set(context)
    try:
        yield
    finally:
        request_context.reset(token)
