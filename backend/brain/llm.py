"""Async Ollama client with streaming."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from backend.brain.followups import fit_local_context
from backend.brain.privacy import redact
from backend.brain.response_mode import simple_request
from backend.config import LLMConfig
from backend.context import request_context
from backend.latency import current

Message = dict[str, Any]


class LLMError(RuntimeError):
    pass


@dataclass
class ChatChunk:
    content: str = ""
    done: bool = False
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    thinking: str = field(default="", repr=False)  # transient tool-round context, never spoken
    native_parts: list[Any] = field(default_factory=list, repr=False)

    @property
    def tokens_per_second(self) -> float | None:
        count, ns = self.stats.get("eval_count"), self.stats.get("eval_duration")
        return count / (ns / 1e9) if count and ns else None


class LLMClient(Protocol):
    def chat_stream(
        self, messages: list[Message], tools: list | None = None
    ) -> AsyncIterator[ChatChunk]: ...
    async def aclose(self) -> None: ...
    async def prime(self, system_prompt: str, tools=None, history=None) -> None: ...
    async def warmup(self) -> None: ...
    async def unload(self) -> None: ...
    async def wait_ready(self, timeout_s: float, interval_s: float = 1.0) -> bool: ...


class OllamaClient:
    def __init__(self, cfg: LLMConfig, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self._client = client or httpx.AsyncClient(
            base_url=cfg.host, timeout=httpx.Timeout(300, connect=5)
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _payload(self, messages: list[Message], stream: bool, tools: list | None) -> dict:
        messages = fit_local_context(messages, tools, self.cfg.num_ctx)
        messages = [dict(m) for m in messages]
        for message in messages:
            if message.get("images"):
                message["images"] = [
                    base64.b64encode(i.data).decode("ascii") for i in message["images"]
                ]
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": redact(messages),
            "stream": stream,
            "think": self.cfg.think,
            "keep_alive": self.cfg.keep_alive,
            "options": {"temperature": self.cfg.temperature, "num_ctx": self.cfg.num_ctx},
        }
        user = next((m.get("content", "") for m in reversed(messages) if m["role"] == "user"), "")
        if context := request_context.get():
            user = context.text
        if isinstance(user, str) and simple_request(user) and not self.cfg.think_for_simple:
            payload["think"] = False
        if profile := current.get():
            profile.update(provider="ollama", model=self.cfg.model, thinking=payload["think"])
        if tools:
            payload["tools"] = tools
        return payload

    async def wait_ready(self, timeout_s: float, interval_s: float = 1.0) -> bool:
        """Wait for the server to answer: at login, Ollama may still be starting."""
        import asyncio
        import time

        deadline = time.monotonic() + timeout_s
        while True:
            try:
                resp = await self._client.get("/api/version")
                if resp.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(interval_s)

    async def warmup(self) -> None:
        """Load the model into memory (an empty message list only loads it)."""
        resp = await self._client.post(
            "/api/chat",
            json=self._payload([], stream=False, tools=None),
        )
        self._raise_for(resp)

    async def unload(self) -> None:
        """Free the model's memory in Ollama now, instead of after `keep_alive`."""
        resp = await self._client.post(
            "/api/chat", json={"model": self.cfg.model, "messages": [], "keep_alive": 0}
        )
        self._raise_for(resp)

    async def prime(
        self, system_prompt: str, tools: list | None = None, history: list[Message] | None = None
    ) -> None:
        """Load the model and pre-process the prompt prefix so the next reply is fast.

        Pass the same tools as the chat: they are rendered into the prompt prefix. With
        `history`, the conversation so far is processed too (Ollama keeps it cached).
        """
        messages = [{"role": "system", "content": system_prompt}, *(history or [])]
        payload = self._payload(messages, False, tools)
        if history:
            # qwen3's template appends "/no_think" to the last user message only; here that
            # message stands in for the next one, which would no longer match the cache.
            del payload["think"]
        payload["options"]["num_predict"] = 1
        self._raise_for(await self._client.post("/api/chat", json=payload))

    async def chat_stream(
        self, messages: list[Message], tools: list | None = None
    ) -> AsyncIterator[ChatChunk]:
        if any(m.get("images") for m in messages):
            tags = await self._client.get("/api/tags")
            self._raise_for(tags)
            names = {m.get("name") for m in tags.json().get("models", [])}
            if self.cfg.model not in names:
                raise LLMError(
                    "Локальная модель не установлена; автоматическая загрузка запрещена."
                )
            info = await self._client.post("/api/show", json={"model": self.cfg.model})
            self._raise_for(info)
            if "vision" not in info.json().get("capabilities", []):
                raise LLMError("Установленная Ollama-модель не поддерживает изображения.")
        payload = self._payload(messages, stream=True, tools=tools)
        profile = current.get()
        interval = (
            profile.begin(
                "llm_transport", provider="ollama", model=self.cfg.model, thinking=payload["think"]
            )
            if profile
            else None
        )
        thinking_interval = None
        outcome = "error"
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    self._raise_for(resp)
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if "error" in data:
                        raise LLMError(data["error"])
                    if data.get("done") and data.get("done_reason") == "length":
                        raise LLMError("Ollama достигла лимита длины; ответ не завершён.")
                    msg = data.get("message") or {}
                    chunk = ChatChunk(
                        content=msg.get("content", ""),
                        thinking=msg.get("thinking", ""),
                        done=bool(data.get("done")),
                        tool_calls=msg.get("tool_calls") or [],
                    )
                    if profile and (chunk.thinking or chunk.content or chunk.tool_calls):
                        profile.mark("first_llm_token")
                        if chunk.thinking and thinking_interval is None:
                            thinking_interval = profile.begin("model_thinking")
                        if chunk.content or chunk.tool_calls:
                            profile.end(thinking_interval)
                            if chunk.content:
                                profile.mark("first_llm_text")
                    if chunk.done:
                        chunk.stats = {
                            k: v for k, v in data.items() if k.endswith(("count", "duration"))
                        }
                    yield chunk
            outcome = "ok"
        finally:
            if profile:
                outcome = "cancelled" if asyncio.current_task().cancelling() else outcome
                profile.end(thinking_interval, outcome)
                profile.end(
                    interval,
                    outcome,
                )

    @staticmethod
    def _raise_for(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        try:
            detail = resp.json().get("error", resp.text)
        except ValueError:
            detail = resp.text
        raise LLMError(f"Ollama {resp.status_code}: {detail}")
