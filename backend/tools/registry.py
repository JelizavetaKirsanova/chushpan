"""Tools the LLM can call: schema for Ollama, dispatch and error handling."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from backend.context import (
    request_context,
    voice_addressed,
    voice_music_command,
    voice_read_followup,
)
from backend.latency import record
from backend.mac_agent import MacAgent, authorized_operation
from backend.security import (
    ActionDenied,
    ConfirmationRequired,
    Confirmations,
    Risk,
    approved_action,
    explicit_request,
    fingerprint,
    risk_for,
)
from backend.tools.validation import ArgumentError, validate_arguments

if TYPE_CHECKING:
    from backend.brain.morning import MorningRoutine

log = logging.getLogger(__name__)

# What the user said this turn, set by Conversation.reply: a tool can check that the user
# really asked for what the model is doing (see `remember`). None outside a conversation.
user_text: ContextVar[str | None] = ContextVar("user_text", default=None)

Handler = Callable[..., Awaitable[Any]]


class ToolError(RuntimeError):
    """An expected failure the model should hear about (bad argument, service down)."""


class Tool:
    def __init__(
        self,
        name: str,
        description: str,
        handler: Handler,
        parameters: dict[str, Any] | None = None,
        required: list[str] | None = None,
        capability: dict[str, str] | None = None,
        slow: bool = False,
        quiet: bool = False,
        *,
        risk: Risk | None = None,
        intent: Callable[[dict, str], bool] | None = None,
        prepare: Callable[[dict], dict] | None = None,
        summary: Callable[[dict], str] | None = None,
        reply: Callable[[dict], str] | None = None,
        version: Callable[[dict], str] | None = None,
    ) -> None:
        self.name, self.description, self._handler = name, description, handler
        self.parameters, self.required = parameters or {}, required or []
        self.capability = capability or {}
        self.slow, self.quiet = slow, quiet
        self.risk, self.intent = risk, intent
        self.prepare, self.summary = prepare, summary
        self.reply = reply
        self.version = version
        self.agent: MacAgent | None = None
        self.required_capability: str | None = None

    @property
    def handler(self) -> Handler:
        # Even code holding the registered handler cannot skip validation/authorization.
        return self.invoke

    @handler.setter
    def handler(self, handler: Handler) -> None:
        self._handler = handler

    async def invoke(self, **arguments: Any) -> Any:
        args = validate_arguments(arguments, self.parameters, self.required)
        context = request_context.get()
        risk = self.risk or risk_for(self.name, args)
        local_music = self.name == "music_command" and voice_music_command.get() == args.get(
            "command"
        )
        local_read = risk == Risk.READ and voice_read_followup.get() == (
            self.name,
            json.dumps(args, sort_keys=True),
        )
        if (
            not voice_addressed.get()
            and approved_action.get() is None
            and not local_music
            and not local_read
            and (risk != Risk.READ or self.required_capability)
        ):
            raise ActionDenied("Для системного действия нужно явное обращение к Чушпану.")
        if self.required_capability:
            if (
                context is None
                or not context.device.authenticated
                or self.required_capability not in context.device.capabilities
                or self.agent is None
                or context.device.target_agent_id != self.agent.agent_id
            ):
                raise ActionDenied("Нет разрешённого и доступного Mac Agent для этого действия.")
        if risk != Risk.READ:
            if context is None or not context.device.authenticated:
                raise ActionDenied("Нужен текущий запрос от авторизованной сессии.")
            intent = self.intent or (lambda a, t: explicit_request(self.name, a, t))
            if not intent(args, context.text):
                raise ActionDenied("Действие не выполнено: нужна явная просьба пользовательницы.")
        if self.prepare:
            args = self.prepare(args)
        summary = self.summary(args) if self.summary else f"{self.name}: {_dump(args)}"
        identity = summary + ("\n" + self.version(args) if self.version else "")
        action = fingerprint(self.name, args, identity)
        if risk == Risk.DANGEROUS and approved_action.get() != action:
            raise ConfirmationRequired(summary, action, args)
        # Legacy service intent checks see the original, trusted request on confirmation.
        token = user_text.set(context.text if context else user_text.get())
        try:
            if self.required_capability:
                ticket = authorized_operation.set((self.name, args, context.request_id))
                try:
                    return await self.agent.execute(self.name, args, context)
                finally:
                    authorized_operation.reset(ticket)
            return await self._handler(**args)
        finally:
            user_text.reset(token)

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required,
                    "additionalProperties": False,
                },
            },
        }


class ToolRegistry:
    def __init__(
        self, tools: Iterable[Tool] = (), *, morning: MorningRoutine | None = None
    ) -> None:
        self.morning = morning
        self.memory = None
        self.youtube_video = None
        self.confirmations = Confirmations()
        self._tools: dict[str, Tool] = {}
        self.on_execution: Callable[[int, str, str, Any], None] | None = None
        self._execution_id = 0
        for tool in tools:
            self.add(tool)

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def is_slow(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.slow)

    def is_quiet(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.quiet)

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def direct_reply(self, name: str, result: Any) -> str | None:
        tool = self._tools.get(name)
        if tool is None or tool.reply is None or not isinstance(result, dict):
            return None
        if "error" in result:
            return "Не выполнил: " + str(result["error"])
        return tool.reply(result)

    def capabilities(self, lang: str) -> list[str]:
        out: list[str] = []
        for tool in self._tools.values():
            text = tool.capability.get(lang) or tool.capability.get("en")
            if text and text not in out:
                out.append(text)
        return out

    async def call(self, name: str, arguments: dict[str, Any] | str | None) -> str:
        self._execution_id += 1
        execution_id = self._execution_id

        def notify(status, result=None):
            if self.on_execution:
                try:
                    self.on_execution(execution_id, name, status, result)
                except Exception as exc:
                    log.warning("Task observer failed type=%s", type(exc).__name__)

        notify("running")
        try:
            result = await self._call(name, arguments)
        except asyncio.CancelledError:
            notify("cancelled")
            raise
        notify("result", json.loads(result))
        return result

    async def _call(self, name: str, arguments: dict[str, Any] | str | None) -> str:
        """Run a tool and return JSON for the model. Never raises: errors become data."""
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return _dump({"error": f"unknown tool {name!r}"})
        started = time.perf_counter()
        try:
            args = validate_arguments(arguments, tool.parameters, tool.required)
            result = await tool.handler(**args)
        except ConfirmationRequired as exc:
            context = request_context.get()
            if context is None:
                return _dump({"error": "Нет текущего запроса для подтверждения."})
            return _dump(self.confirmations.request(name, exc.arguments, exc, context))
        except (ToolError, ArgumentError, ActionDenied, PermissionError) as exc:
            return _dump({"error": str(exc)})
        except sqlite3.Error:
            return _dump({"error": "SQLite недоступна. Память не изменена; попробуй позже."})
        except Exception as exc:  # noqa: BLE001
            log.exception("Tool %s failed", name)
            return _dump({"error": f"{type(exc).__name__}: {exc}"})
        finally:
            record("tool_execution", time.perf_counter() - started)
        return _dump(result)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
