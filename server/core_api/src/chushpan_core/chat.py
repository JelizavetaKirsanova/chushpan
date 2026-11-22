"""Bounded OpenAI chat transport with request deduplication support."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from chushpan_core.secrets import load_openai_key, openai_key_configured

MAX_PENDING_REQUESTS = 8
MAX_CHAT_MESSAGES = 32
MAX_MESSAGE_CHARS = 20_000
MAX_TOTAL_CHARS = 60_000


class ChatError(RuntimeError):
    """A sanitized provider or queue error suitable for a client response."""


class ChatBusy(ChatError):
    """The bounded FIFO chat queue is full."""


class ChatProvider(Protocol):
    async def stream(
        self, messages: list[dict], request_id: str, tools: list[dict] | None = None
    ) -> AsyncIterator[dict | str]: ...


class OpenAIChatProvider:
    provider_name = "openai"

    def __init__(
        self,
        *,
        key_loader=load_openai_key,
        client: httpx.AsyncClient | None = None,
        model: str | None = None,
    ) -> None:
        self._key_loader = key_loader
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=10),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            trust_env=False,
        )
        self._owns_client = client is None
        self.model = model or os.environ.get("CHUSHPAN_CORE_OPENAI_MODEL", "gpt-4.1-mini")

    @property
    def available(self) -> bool:
        return openai_key_configured()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def stream(
        self, messages: list[dict], request_id: str, tools: list[dict] | None = None
    ) -> AsyncIterator[dict | str]:
        try:
            key = self._key_loader()
        except (OSError, RuntimeError, ValueError):
            raise ChatError(
                "OpenAI credential is unavailable; provision it over SSH first."
            ) from None
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0.6,
            "max_completion_tokens": 2048,
        }
        allowed_tools = set()
        calls: dict[int, dict] = {}
        if tools:
            allowed_tools = {item["function"]["name"] for item in tools}
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = False
        saw_done = False
        try:
            async with (
                asyncio.timeout(120),
                self._client.stream(
                    "POST",
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "X-Request-Id": request_id},
                    json=payload,
                ) as response,
            ):
                if response.status_code >= 400:
                    await response.aread()
                    raise ChatError(f"OpenAI returned HTTP {response.status_code}.")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        saw_done = True
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        raise ChatError("OpenAI returned an invalid streaming event.") from None
                    if error := event.get("error"):
                        status_code = error.get("status") if isinstance(error, dict) else None
                        suffix = f" HTTP {status_code}" if isinstance(status_code, int) else ""
                        raise ChatError(f"OpenAI stream failed{suffix}.")
                    choices = event.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        text = delta.get("content")
                        if isinstance(text, str) and text:
                            yield {"type": "token", "text": text}
                        for call in delta.get("tool_calls") or []:
                            index = call.get("index")
                            if type(index) is not int or not 0 <= index < 64:
                                raise ChatError("OpenAI returned an invalid tool call.")
                            item = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                            if call.get("id"):
                                item["id"] = call["id"]
                            function = call.get("function") or {}
                            item["name"] += function.get("name") or ""
                            item["arguments"] += function.get("arguments") or ""
                            if len(item["name"]) > 100 or len(item["arguments"]) > 20_000:
                                raise ChatError("OpenAI returned an oversized tool call.")
                        if choices[0].get("finish_reason") == "length":
                            raise ChatError("OpenAI response reached its token limit.")
        except TimeoutError:
            raise ChatError("OpenAI request timed out.") from None
        except httpx.HTTPError:
            raise ChatError("OpenAI could not be reached.") from None
        if not saw_done:
            raise ChatError("OpenAI stream ended before completion.")
        if calls:
            result = []
            for index in sorted(calls):
                call = calls[index]
                if call["name"] not in allowed_tools or not call["id"]:
                    raise ChatError("OpenAI requested a tool outside the Mac Agent allowlist.")
                try:
                    arguments = json.loads(call["arguments"] or "{}")
                except json.JSONDecodeError:
                    raise ChatError("OpenAI returned invalid tool arguments.") from None
                if not isinstance(arguments, dict):
                    raise ChatError("OpenAI returned invalid tool arguments.")
                result.append(
                    {
                        "id": call["id"],
                        "function": {"name": call["name"], "arguments": arguments},
                    }
                )
            yield {"type": "tool_calls", "tool_calls": result}


class OllamaChatProvider:
    """Optional loopback-only Ollama transport; never probes or installs a model."""

    provider_name = "ollama"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        model: str | None = None,
        endpoint: str | None = None,
    ) -> None:
        self.model = model or os.environ.get("CHUSHPAN_CORE_OLLAMA_MODEL", "qwen3:1.7b")
        address = endpoint or os.environ.get("CHUSHPAN_CORE_OLLAMA_URL", "http://127.0.0.1:11434")
        parsed = urlsplit(address)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port != 11434
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Huawei Ollama may only use its loopback port 11434.")
        self._client = client or httpx.AsyncClient(
            base_url=address.rstrip("/"),
            timeout=httpx.Timeout(120, connect=3),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            trust_env=False,
        )
        self._owns_client = client is None

    @property
    def available(self) -> bool:
        return bool(self.model)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _messages(messages: list[dict]) -> list[dict]:
        result = []
        for item in messages:
            row = dict(item)
            if row.get("tool_calls"):
                calls = []
                for call in row["tool_calls"]:
                    function = dict(call.get("function") or {})
                    arguments = function.get("arguments", "{}")
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            raise ChatError("Mac Agent supplied invalid tool arguments.") from None
                    if not isinstance(arguments, dict):
                        raise ChatError("Mac Agent supplied invalid tool arguments.")
                    function["arguments"] = arguments
                    calls.append({**call, "function": function})
                row["tool_calls"] = calls
            result.append(row)
        return result

    async def stream(
        self, messages: list[dict], request_id: str, tools: list[dict] | None = None
    ) -> AsyncIterator[dict | str]:
        payload = {
            "model": self.model,
            "messages": self._messages(messages),
            "stream": True,
            "options": {"temperature": 0.6, "num_predict": 2048},
        }
        allowed_tools = set()
        if tools:
            allowed_tools = {item["function"]["name"] for item in tools}
            payload["tools"] = tools
        calls = []
        saw_done = False
        try:
            async with (
                asyncio.timeout(120),
                self._client.stream(
                    "POST",
                    "/api/chat",
                    headers={"X-Request-Id": request_id},
                    json=payload,
                ) as response,
            ):
                if response.status_code >= 400:
                    raise ChatError(f"Ollama returned HTTP {response.status_code}.")
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        raise ChatError("Ollama returned an invalid streaming event.") from None
                    if event.get("error"):
                        raise ChatError("Ollama reported a model error.")
                    message = event.get("message") or {}
                    text = message.get("content")
                    if isinstance(text, str) and text:
                        yield {"type": "token", "text": text}
                    for call in message.get("tool_calls") or []:
                        function = call.get("function") or {}
                        name = function.get("name")
                        arguments = function.get("arguments")
                        if (
                            not isinstance(name, str)
                            or name not in allowed_tools
                            or not isinstance(arguments, dict)
                        ):
                            raise ChatError(
                                "Ollama requested a tool outside the Mac Agent allowlist."
                            )
                        calls.append(
                            {
                                "id": "ollama_" + uuid4().hex,
                                "function": {"name": name, "arguments": arguments},
                            }
                        )
                        if len(calls) > 32:
                            raise ChatError("Ollama returned too many tool calls.")
                    if event.get("done") is True:
                        saw_done = True
        except TimeoutError:
            raise ChatError("Ollama request timed out.") from None
        except httpx.HTTPError:
            raise ChatError("Ollama could not be reached on Huawei loopback.") from None
        if not saw_done:
            raise ChatError("Ollama stream ended before completion.")
        if calls:
            yield {"type": "tool_calls", "tool_calls": calls}


class _Reservation(AbstractAsyncContextManager):
    def __init__(self, service: ChatService) -> None:
        self.service = service
        self.acquired = False
        self.closed = False

    async def __aenter__(self):
        try:
            await self.service._serial.acquire()
            self.acquired = True
            return self
        except BaseException:
            await self.service._release_pending()
            raise

    async def __aexit__(self, exc_type, exc, traceback):
        if self.closed:
            return False
        self.closed = True
        if self.acquired:
            self.service._serial.release()
            self.acquired = False
        await self.service._release_pending()
        return False

    async def close(self) -> None:
        await self.__aexit__(None, None, None)


class ChatService:
    """One active LLM stream at a time, with a bounded FIFO of waiting clients."""

    def __init__(self, provider: ChatProvider | None = None) -> None:
        configured_provider = os.environ.get("CHUSHPAN_CORE_LLM_PROVIDER", "openai").casefold()
        if provider is None and configured_provider == "ollama":
            provider = OllamaChatProvider()
        elif provider is None and configured_provider == "openai":
            provider = OpenAIChatProvider()
        elif provider is None:
            raise ValueError("CHUSHPAN_CORE_LLM_PROVIDER must be openai or ollama.")
        self.provider = provider
        self._serial = asyncio.Lock()
        self._pending_lock = asyncio.Lock()
        self._pending = 0

    @property
    def available(self) -> bool:
        return bool(getattr(self.provider, "available", True))

    @property
    def pending(self) -> int:
        return self._pending

    async def reserve(self) -> _Reservation:
        async with self._pending_lock:
            if self._pending >= MAX_PENDING_REQUESTS:
                raise ChatBusy("Core chat queue is full.")
            self._pending += 1
        return _Reservation(self)

    async def _release_pending(self) -> None:
        async with self._pending_lock:
            self._pending = max(0, self._pending - 1)

    async def stream(
        self, messages: list[dict], request_id: str, tools: list[dict] | None = None
    ) -> AsyncIterator[dict | str]:
        async for chunk in self.provider.stream(messages, request_id, tools):
            yield chunk
