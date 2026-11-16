"""OpenAI Responses API adapter for the shared Chushpan conversation pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from backend.brain.llm import ChatChunk, LLMError, Message
from backend.brain.privacy import redact
from backend.config import OpenAIConfig
from backend.latency import current

log = logging.getLogger(__name__)


def _transient_transport_error(exc: Exception) -> bool:
    """Recognize OpenAI SDK connection wrappers as retryable transport failures."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (httpx.TimeoutException, httpx.NetworkError, TimeoutError)):
            return True
        if type(current).__name__ in {"APIConnectionError", "APITimeoutError"}:
            return True
        current = current.__cause__ or current.__context__
    return False


class OpenAIRequestError(LLMError):
    def __init__(self, status_code: int | None, *, transient: bool, reason: str):
        self.status_code = status_code
        self.transient = transient
        super().__init__(f"OpenAI{f' HTTP {status_code}' if status_code else ''}: {reason}.")


def text_models(models) -> list[str]:
    excluded = (
        "codex",
        "audio",
        "realtime",
        "transcribe",
        "tts",
        "embedding",
        "moderation",
        "search",
        "deep-research",
        "image",
    )
    ids = []
    for model in models:
        identifier = getattr(model, "id", "")
        if (
            isinstance(identifier, str)
            and identifier.startswith(("gpt-", "chat-"))
            and not any(part in identifier.lower() for part in excluded)
        ):
            ids.append(identifier)
    return sorted(set(ids))


class OpenAIClient:
    cloud_context = True
    provider = "openai"

    def __init__(self, cfg: OpenAIConfig, key: str, *, client: Any = None):
        self.cfg, self._key, self._client = cfg, key, client
        self.on_usage = None

    @property
    def available(self):
        return bool(self._key)

    @property
    def first_result_budget_s(self):
        return self.cfg.first_result_budget_s

    @property
    def attempt_timeout_s(self):
        return self.cfg.attempt_timeout_s

    @property
    def simple_timeout_s(self):
        return self.cfg.simple_timeout_s

    def _sdk(self):
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError:
                raise LLMError("Пакет официального OpenAI SDK не установлен.") from None
            self._client = AsyncOpenAI(
                api_key=self._key,
                max_retries=0,
                timeout=self.cfg.timeout_s,
            )
        return self._client

    async def aclose(self):
        if self._client is not None and hasattr(self._client, "close"):
            await self._client.close()

    async def wait_ready(self, timeout_s: float, interval_s: float = 1.0) -> bool:
        del timeout_s, interval_s
        return self.available

    async def prime(self, system_prompt: str, tools=None, history=None):
        del system_prompt, tools, history

    async def warmup(self):
        return

    async def unload(self):
        return

    async def models(self) -> list[str]:
        page = await self._sdk().models.list()
        return text_models(page.data)

    async def check_connection(self) -> list[str]:
        return await self.models()

    @staticmethod
    def _input(messages: list[Message]):
        instructions = []
        items = []
        known_calls: set[str] = set()
        for message in messages:
            role = message.get("role")
            content = redact(message.get("content", ""))
            if role == "system":
                if content:
                    instructions.append(content)
            elif role == "tool":
                call_id = message.get("tool_call_id")
                if call_id and call_id in known_calls:
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": "Недоверенный результат инструмента: " + str(content),
                        }
                    )
                elif content:
                    items.append(
                        {
                            "role": "user",
                            "content": "Предыдущий результат инструмента (данные): " + str(content),
                        }
                    )
            elif role == "assistant" and message.get("tool_calls"):
                if content:
                    items.append({"role": "assistant", "content": content})
                for call in message["tool_calls"]:
                    function = call.get("function") or {}
                    call_id = call.get("id")
                    name = function.get("name")
                    if call_id and name:
                        known_calls.add(call_id)
                        arguments = function.get("arguments", {})
                        items.append(
                            {
                                "type": "function_call",
                                "call_id": call_id,
                                "name": name,
                                "arguments": arguments
                                if isinstance(arguments, str)
                                else json.dumps(arguments, ensure_ascii=False),
                            }
                        )
            elif role in {"user", "assistant"} and content:
                items.append({"role": role, "content": content})
        return "\n\n".join(instructions), items

    @staticmethod
    def _tools(tools):
        return [
            {
                "type": "function",
                "name": item["function"]["name"],
                "description": item["function"].get("description", ""),
                "parameters": item["function"].get("parameters", {"type": "object"}),
                "strict": False,
            }
            for item in tools or []
        ]

    async def _stream(self, payload):
        try:
            return await self._sdk().responses.create(**payload)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            body = getattr(exc, "body", None)
            detail = body.get("error", {}) if isinstance(body, dict) else {}
            param = getattr(exc, "param", None) or (
                detail.get("param") if isinstance(detail, dict) else None
            )
            error_code = getattr(exc, "code", None) or (
                detail.get("code") if isinstance(detail, dict) else None
            )
            message = str(exc).lower()
            unsupported = (
                param
                if param in {"temperature", "max_output_tokens"}
                else next(
                    (
                        name
                        for name in ("temperature", "max_output_tokens")
                        if name in message
                        and ("unsupported" in message or "not support" in message)
                    ),
                    None,
                )
            )
            if unsupported and unsupported in payload:
                payload = {k: v for k, v in payload.items() if k != unsupported}
                return await self._sdk().responses.create(**payload)
            no_credits = error_code in {
                "insufficient_quota",
                "billing_not_active",
                "credits_exhausted",
            }
            transient = not no_credits and (
                status in {429, 500, 502, 503, 504} or _transient_transport_error(exc)
            )
            reason = (
                "проверь API-ключ"
                if status in {401, 403}
                else "недостаточно средств или исчерпан лимит API"
                if status == 402 or no_credits
                else "временно превышен лимит запросов"
                if status == 429
                else "временная сетевая ошибка"
                if transient
                else "запрос не поддержан выбранной моделью"
                if status == 400
                else "служба API вернула ошибку"
            )
            raise OpenAIRequestError(status, transient=transient, reason=reason) from None

    async def chat_stream(self, messages: list[Message], tools=None) -> AsyncIterator[ChatChunk]:
        started = time.perf_counter()
        instructions, input_items = self._input(messages)
        payload = {
            "model": self.cfg.model,
            "instructions": instructions,
            "input": input_items,
            "max_output_tokens": self.cfg.max_output_tokens,
            "stream": True,
        }
        if self.cfg.temperature is not None:
            payload["temperature"] = self.cfg.temperature
        declarations = self._tools(tools)
        if declarations:
            payload["tools"] = declarations
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = False
        if profile := current.get():
            from backend.brain.followups import estimated_tokens

            profile.update(
                provider=self.provider,
                model=self.cfg.model,
                context_chars=sum(len(m.get("content") or "") for m in messages),
                context_estimated_tokens=estimated_tokens(messages),
                context_messages=len(messages),
            )
        try:
            async with asyncio.timeout(self.cfg.timeout_s):
                stream = await self._stream(payload)
                calls = []
                usage = None
                completed = False
                async for event in stream:
                    event_type = getattr(event, "type", "")
                    if event_type == "response.output_text.delta":
                        yield ChatChunk(content=getattr(event, "delta", ""))
                    elif event_type == "response.function_call_arguments.done":
                        try:
                            arguments = json.loads(event.arguments)
                        except (TypeError, ValueError):
                            raise LLMError(
                                "OpenAI вернул некорректные аргументы инструмента."
                            ) from None
                        calls.append(
                            {
                                "id": event.call_id,
                                "function": {"name": event.name, "arguments": arguments},
                            }
                        )
                    elif event_type == "response.completed":
                        response = getattr(event, "response", None)
                        usage = getattr(response, "usage", None)
                        completed = True
                    elif event_type in {"response.failed", "error"}:
                        raise LLMError("OpenAI не завершил генерацию. Команды не выполнялись.")
                if not completed:
                    raise LLMError("OpenAI прервал поток. Ответ неполный, команды не выполнялись.")
                duration_ns = int((time.perf_counter() - started) * 1e9)
                stats = {
                    "model": self.cfg.model,
                    "prompt_eval_count": getattr(usage, "input_tokens", None),
                    "eval_count": getattr(usage, "output_tokens", None),
                    "total_duration": duration_ns,
                    "eval_duration": duration_ns,
                }
                from backend.brain.pricing import estimate

                stats["input_tokens"] = stats["prompt_eval_count"]
                stats["output_tokens"] = stats["eval_count"]
                stats["estimated_cost_usd"] = estimate(
                    self.cfg.model, stats["input_tokens"], stats["output_tokens"]
                )
                stats["price_unknown"] = stats["estimated_cost_usd"] is None
                if self.on_usage:
                    try:
                        self.on_usage(dict(stats))
                    except Exception:
                        log.warning("OpenAI usage subscriber unavailable")
                yield ChatChunk(done=True, tool_calls=calls, stats=stats)
        except OpenAIRequestError:
            raise
        except TimeoutError:
            raise OpenAIRequestError(
                None, transient=True, reason="превышено время ожидания"
            ) from None
        except LLMError:
            raise
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            body = getattr(exc, "body", None)
            detail = body.get("error", {}) if isinstance(body, dict) else {}
            error_code = getattr(exc, "code", None) or (
                detail.get("code") if isinstance(detail, dict) else None
            )
            no_credits = error_code in {
                "insufficient_quota",
                "billing_not_active",
                "credits_exhausted",
            }
            transient = not no_credits and (
                status in {429, 500, 502, 503, 504} or _transient_transport_error(exc)
            )
            reason = (
                "проверь API-ключ"
                if status in {401, 403}
                else "недостаточно средств или исчерпан лимит API"
                if status == 402 or no_credits
                else "временная сетевая ошибка"
                if transient
                else "ошибка обработки запроса"
            )
            log.warning(
                "OpenAI stream failed status=%s transient=%s error_type=%s cause_type=%s",
                status,
                transient,
                type(exc).__name__,
                type(exc.__cause__).__name__ if exc.__cause__ else None,
            )
            raise OpenAIRequestError(status, transient=transient, reason=reason) from None
