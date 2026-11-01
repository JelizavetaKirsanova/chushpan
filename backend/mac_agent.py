"""A narrow dispatch port for a future authenticated Mac Agent transport. No shell API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any, Protocol

from backend.context import RequestContext

authorized_operation: ContextVar[tuple[str, dict, str] | None] = ContextVar(
    "authorized_operation", default=None
)


class MacAgent(Protocol):
    agent_id: str

    async def execute(self, operation: str, arguments: dict, context: RequestContext) -> Any: ...


class LocalMacAgent:
    agent_id = "local-mac"

    def __init__(self) -> None:
        self._operations: dict[str, Callable[..., Awaitable[Any]]] = {}

    def register(self, name: str, handler: Callable[..., Awaitable[Any]]) -> None:
        self._operations[name] = handler

    async def execute(self, operation: str, arguments: dict, context: RequestContext) -> Any:
        if not context.device.authenticated or context.device.target_agent_id != self.agent_id:
            raise PermissionError("Нет разрешения на выбранный Mac Agent.")
        if authorized_operation.get() != (operation, arguments, context.request_id):
            raise PermissionError("Mac Agent принимает только проверенные вызовы инструментов.")
        handler = self._operations.get(operation)
        if handler is None:
            raise PermissionError("Операция недоступна на этом Mac Agent.")
        return await handler(**arguments)
