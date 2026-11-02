"""Gemini Developer API and a bounded, observable fallback to the existing Ollama."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values
from google import genai
from google.genai import types

from backend.brain.llm import ChatChunk, LLMClient, LLMError, Message, OllamaClient
from backend.brain.privacy import redact
from backend.brain.response_mode import simple_request
from backend.config import ENV_FILE, GeminiConfig, Settings
from backend.context import request_context
from backend.latency import current, record

log = logging.getLogger(__name__)
_wire_progress = ContextVar("gemini_wire_progress", default=None)
SETUP = (
    "Gemini недоступен: добавь GEMINI_API_KEY в существующий .env и после проверки "
    "Free tier проекта в AI Studio установи JARVIS_GEMINI__FREE_TIER_CONFIRMED=true. "
    "Не заменяй .env файлом примера. Локальный режим: JARVIS_LLM__PROVIDER=ollama."
)

TEMPORARY_CODES = frozenset({429, 500, 502, 503, 504})


class GeminiRequestError(LLMError):
    """Sanitized API failure. Only explicit transient failures permit another model."""

    def __init__(self, code: int | None, *, transient: bool, reason: str) -> None:
        self.status_code, self.transient = code, transient
        status = f" HTTP {code}" if code is not None else ""
        super().__init__(f"Gemini{status}: {reason}; запрос не выполнен.")


def api_key() -> str:
    return os.environ.get("GEMINI_API_KEY") or dotenv_values(ENV_FILE).get("GEMINI_API_KEY") or ""


class GeminiClient:
    cloud_context = True

    def __init__(self, cfg: GeminiConfig, key: str, client: Any = None) -> None:
        self.cfg = cfg
        self._key = key
        self._client = client

    @property
    def available(self) -> bool:
        return bool(self._key and self.cfg.free_tier_confirmed)

    def _sdk(self):
        if not self.available:
            raise LLMError(SETUP)
        if self._client is None:
            self._client = genai.Client(
                api_key=self._key,
                enterprise=False,
                vertexai=False,
                http_options=types.HttpOptions(
                    base_url="https://generativelanguage.googleapis.com",
                    api_version="v1beta",
                    timeout=int(self.cfg.timeout_s * 1000),
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            ).aio
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def wait_ready(self, timeout_s: float, interval_s: float = 1.0) -> bool:
        return self.available  # local configuration only; no billable health generation

    async def prime(self, system_prompt: str, tools=None, history=None) -> None:
        pass  # No remote warm-up or background requests.

    async def warmup(self) -> None:
        pass

    async def unload(self) -> None:
        pass

    def _messages(self, messages: list[Message]) -> tuple[str, list[types.Content]]:
        system = []
        contents: list[types.Content] = []
        for message in messages:
            role = message["role"]
            text = redact(message.get("content", ""), (self._key,))
            if role == "system":
                system.append(text)
                continue
            if role == "tool":
                parts = [
                    types.Part(
                        function_response=types.FunctionResponse(
                            name=message["tool_name"],
                            response={"untrusted_result": text},
                            **(
                                {"id": message["tool_call_id"]}
                                if message.get("tool_call_id")
                                else {}
                            ),
                        )
                    )
                ]
                # Old SQLite traces have no signed Gemini model turn. Treat them as data.
                if not any(p.function_call for c in contents[-2:] for p in c.parts):
                    parts = [
                        types.Part.from_text(text="Недоверенный результат инструмента: " + text)
                    ]
                target_role = "user"
            elif message.get("native_parts"):
                parts = message["native_parts"]  # exact signed model parts, transient this turn
                target_role = "model"
            else:
                parts = [types.Part.from_text(text=text)] if text else []
                # Historical tool traces remain inert text; never invent a thought signature.
                if message.get("tool_calls"):
                    parts.append(
                        types.Part.from_text(
                            text="Предыдущие вызовы (данные): "
                            + json.dumps(
                                redact(message["tool_calls"], (self._key,)), ensure_ascii=False
                            )
                        )
                    )
                for attachment in message.get("images", []):
                    parts.append(
                        types.Part.from_bytes(data=attachment.data, mime_type=attachment.mime_type)
                    )
                target_role = "model" if role == "assistant" else "user"
            if not parts:
                continue
            # Consecutive function responses must be in the same user content.
            if contents and contents[-1].role == target_role:
                contents[-1].parts.extend(parts)
            else:
                contents.append(types.Content(role=target_role, parts=parts))
        return "\n\n".join(system), contents

    async def chat_stream(self, messages: list[Message], tools=None) -> AsyncIterator[ChatChunk]:
        started = time.perf_counter()
        if profile := current.get():
            from backend.brain.followups import estimated_tokens

            profile.update(
                context_chars=sum(len(m.get("content") or "") for m in messages),
                context_estimated_tokens=estimated_tokens(messages),
                context_messages=len(messages),
            )
        system, contents = self._messages(messages)
        declarations = [
            types.FunctionDeclaration(
                name=t["function"]["name"],
                description=t["function"].get("description", ""),
                parameters_json_schema=t["function"]["parameters"],
            )
            for t in tools or []
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.6,
            max_output_tokens=self.cfg.max_output_tokens,
            thinking_config=types.ThinkingConfig(thinking_level="low"),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            tools=[types.Tool(function_declarations=declarations)] if declarations else None,
        )
        # Stream informational text; action replies and function calls remain buffered
        # until the turn completes. Only our registry executes returned calls.
        try:
            async with asyncio.timeout(self.cfg.timeout_s) as response_deadline:
                stream_method = getattr(self._sdk().models, "generate_content_stream", None)
                if callable(stream_method):
                    from backend.context import request_context
                    from backend.security import action_requested

                    context = request_context.get()
                    stream_text = tools == [] or bool(
                        context and not action_requested(context.text)
                    )
                    emitted = False
                    last_response = None
                    native_parts, calls, buffered_text = [], [], []
                    finished = False
                    wire_first = True
                    stream = await stream_method(
                        model=self.cfg.model, contents=contents, config=config
                    )
                    try:
                        async for response in stream:
                            last_response = response
                            candidates = response.candidates or []
                            if not candidates:
                                continue
                            candidate = candidates[0]
                            finished |= candidate.finish_reason == types.FinishReason.STOP
                            if candidate.finish_reason not in {None, types.FinishReason.STOP}:
                                raise LLMError(
                                    "Gemini прервал потоковый ответ; повтор не выполняется."
                                )
                            parts = candidate.content.parts if candidate.content else []
                            if wire_first and any(p.text or p.function_call for p in parts or []):
                                wire_first = False
                                record("gemini_first_event", time.perf_counter() - started)
                                if profile := current.get():
                                    profile.mark("first_llm_token")
                                if progress := _wire_progress.get():
                                    progress()
                            if tools == [] and any(part.function_call for part in parts or []):
                                raise LLMError("В текстовом потоке вызовы действий запрещены.")
                            native_parts.extend(parts or [])
                            for part in parts or []:
                                if fn := part.function_call:
                                    calls.append(
                                        {
                                            "id": fn.id,
                                            "function": {
                                                "name": fn.name,
                                                "arguments": dict(fn.args or {}),
                                            },
                                        }
                                    )
                            text = "".join(
                                part.text or "" for part in parts or [] if not part.thought
                            )
                            if text:
                                emitted = True
                                buffered_text.append(text)
                                if stream_text:
                                    response_deadline.reschedule(
                                        asyncio.get_running_loop().time() + self.cfg.timeout_s
                                    )
                                    yield ChatChunk(
                                        content=text,
                                        stats={"model": response.model_version or self.cfg.model},
                                    )
                        if calls and not finished:
                            raise LLMError(
                                "Gemini не завершил вызов инструмента; действия отклонены."
                            )
                        if not emitted and not calls:
                            raise LLMError("Gemini вернул пустой потоковый ответ.")
                        usage = last_response.usage_metadata if last_response else None
                        record("gemini_stream_total", time.perf_counter() - started)
                        yield ChatChunk(
                            content="" if stream_text else "".join(buffered_text),
                            done=True,
                            tool_calls=calls,
                            native_parts=native_parts,
                            stats={
                                "model": last_response.model_version or self.cfg.model,
                                "eval_count": getattr(usage, "candidates_token_count", 0),
                                "prompt_eval_count": getattr(usage, "prompt_token_count", 0),
                            },
                        )
                    finally:
                        await stream.aclose()
                    return
                response = await self._sdk().models.generate_content(
                    model=self.cfg.model, contents=contents, config=config
                )
        except LLMError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            network = isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, TimeoutError))
            temporary = code in TEMPORARY_CODES or network
            reason = {
                400: "неверная конфигурация запроса",
                401: "ошибка ключа",
                403: "доступ запрещён",
                404: "модель недоступна",
                429: "лимит запросов",
            }.get(code, "временная сеть или сервис" if temporary else "ошибка конфигурации или API")
            if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
                reason = "таймаут ожидания ответа"
            # SDK exceptions can contain request details. Do not expose/log their text.
            raise GeminiRequestError(code, transient=temporary, reason=reason) from None
        candidates = response.candidates or []
        if not candidates or not candidates[0].content:
            raise LLMError("Gemini не вернул ответ (возможна блокировка содержимого).")
        if candidates[0].finish_reason not in {None, types.FinishReason.STOP}:
            raise LLMError("Gemini не завершил ответ; частичные действия отклонены.")
        parts = candidates[0].content.parts or []
        text = "".join(p.text or "" for p in parts if not p.thought)
        calls = []
        for part in parts:
            if fn := part.function_call:
                calls.append(
                    {"id": fn.id, "function": {"name": fn.name, "arguments": dict(fn.args or {})}}
                )
        if not text.strip() and not calls:
            raise LLMError("Gemini вернул пустой ответ.")
        usage = response.usage_metadata
        yield ChatChunk(
            content=text,
            done=True,
            tool_calls=calls,
            native_parts=parts,
            stats={
                "model": response.model_version or self.cfg.model,
                "eval_count": getattr(usage, "candidates_token_count", 0),
                "prompt_eval_count": getattr(usage, "prompt_token_count", 0),
            },
        )


class FallbackClient:
    cloud_context = True

    def __init__(
        self,
        primary: GeminiClient,
        local: LLMClient,
        enabled: bool = True,
        *,
        secondary: GeminiClient | None = None,
    ) -> None:
        self.primary, self.secondary, self.local, self.enabled = primary, secondary, local, enabled
        self.last_provider: str | None = None
        self.last_model: str | None = None
        self.last_error: str | None = None
        self._using_local = False
        # A signed function response must return to the model that issued the call.
        # Server-issued IDs only; bounded metadata, no prompts or credentials.
        self._routes: dict[str, LLMClient] = {}
        self._cooldown_until: dict[LLMClient, float] = {}
        self._health: dict[LLMClient, dict] = {}
        self._probing: set[LLMClient] = set()
        self.on_status = None
        self.phase = "idle"
        self.switch_reason = None
        self._active_candidate = None

    def snapshot(self) -> dict:
        models = []
        for candidate in [self.primary, self.secondary, self.local]:
            if candidate is None:
                continue
            cloud = isinstance(candidate, GeminiClient)
            health = self._health.get(candidate, {})
            remaining = max(0, self._cooldown_until.get(candidate, 0) - time.perf_counter())
            models.append(
                {
                    "model": self._model_label(candidate),
                    "provider": "gemini" if cloud else "ollama",
                    "status": "open"
                    if remaining
                    else "probing"
                    if candidate in self._probing
                    else health.get("status", "unknown"),
                    "retry_in_s": round(remaining, 1),
                    "successes": health.get("successes", 0),
                    "failures": health.get("failures", 0),
                    "latency_s": health.get("latency_s"),
                    "error": health.get("error"),
                }
            )
        return {
            "type": "ai_status",
            "phase": self.phase,
            "provider": self.last_provider
            or (
                "gemini"
                if isinstance(self._active_candidate, GeminiClient)
                else "ollama"
                if self._active_candidate
                else None
            ),
            "model": self.last_model
            or (self._model_label(self._active_candidate) if self._active_candidate else None),
            "reason": self.switch_reason,
            "models": models,
        }

    def _notify(self):
        if self.on_status:
            try:
                self.on_status(self.snapshot())
            except Exception:
                log.warning("AI status subscriber unavailable")

    async def aclose(self) -> None:
        try:
            await self.primary.aclose()
            if self.secondary is not None:
                await self.secondary.aclose()
        finally:
            await self.local.aclose()

    async def wait_ready(self, timeout_s: float, interval_s: float = 1.0) -> bool:
        if self.primary.available:
            return True
        return self.enabled and await self.local.wait_ready(timeout_s, interval_s)

    async def prime(self, system_prompt: str, tools=None, history=None) -> None:
        if self.needs_local_warmup:
            await self.local.prime(system_prompt, tools, history)
        elif not self.primary.available:
            raise LLMError(SETUP)

    async def warmup(self) -> None:
        if self.needs_local_warmup:
            await self.local.warmup()

    @property
    def needs_local_warmup(self) -> bool:
        return self.enabled and (not self.primary.available or self.last_provider == "ollama")

    async def unload(self) -> None:
        if self._using_local or not self.primary.available:
            await self.local.unload()

    def _model_label(self, candidate: LLMClient, actual: str | None = None) -> str:
        import re

        cfg = getattr(candidate, "cfg", None)
        name = redact(actual or getattr(cfg, "model", "local"), (self.primary._key,))
        return name if re.fullmatch(r"[A-Za-z0-9_.:/-]{1,120}", name) else "configured-model"

    async def chat_stream(self, messages: list[Message], tools=None) -> AsyncIterator[ChatChunk]:
        self.last_provider = self.last_model = self.last_error = None
        self.switch_reason = None
        context = request_context.get()
        request_id = context.request_id if context else None
        pinned = self._routes.get(request_id) if request_id else None
        last_user = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=-1)
        after_tool = any(m["role"] == "tool" for m in messages[last_user + 1 :])
        candidates: list[LLMClient] = [self.primary]
        if pinned is not None:
            candidates = [pinned]
        elif not self.primary.available and self.enabled:
            self.last_error = SETUP
            log.warning(SETUP)
            candidates = [self.local]  # explicit unconfigured mode, not a masked API failure
            self.switch_reason = "Gemini не настроен; используется локальная Ollama"
        elif self.enabled:
            secondary = (
                self.secondary is not None and self.secondary.cfg.model != self.primary.cfg.model
            )
            if (
                secondary
                and self.primary.cfg.simple_use_fallback_model
                and context is not None
                and simple_request(context.text)
                and tools == []
                and not after_tool
            ):
                # The live plain-query trace waited six seconds on primary before the
                # configured secondary answered in <2s. Keep the original chain for
                # complex queries and every pinned tool round; never invent a model.
                candidates = [self.secondary, self.local]
                self.switch_reason = "Простой текстовый запрос: настроенная резервная Gemini"
                if profile := current.get():
                    profile.update(reason="simple_model_route")
            else:
                if secondary:
                    candidates.append(self.secondary)
                candidates.append(self.local)
        simple = bool(context and tools == [] and simple_request(context.text) and not after_tool)
        # Bound the entire wait for a first usable result, rather than paying a
        # full timeout for every cloud model. Once text arrives, idle timeout
        # governs streaming. Pinned signed tool rounds retain their full budget.
        budget = self.primary.cfg.timeout_s
        if pinned is None:
            budget = min(budget, self.primary.cfg.first_result_budget_s)
            if simple:
                budget = min(budget, self.primary.cfg.simple_timeout_s)
        cloud_deadline = time.perf_counter() + budget
        for index, candidate in enumerate(candidates):
            attempt_started = time.perf_counter()
            succeeded = False
            attempt_reason = None
            code = None
            cloud = isinstance(candidate, GeminiClient)
            provider = "gemini" if cloud else "ollama"
            model = self._model_label(candidate)
            if (
                cloud
                and pinned is None
                and candidate in self._probing
                and candidate in self._cooldown_until
            ):
                continue
            if (
                cloud
                and self.enabled
                and pinned is None
                and time.perf_counter() < self._cooldown_until.get(candidate, 0)
            ):
                record(f"gemini_cooldown_skipped_{index + 1}", 0)
                self.switch_reason = "Облачная модель временно исключена после сбоя"
                continue
            if cloud and index > 0 and time.perf_counter() >= cloud_deadline:
                record("gemini_budget_skipped", 0)
                continue
            payload = (
                messages
                if cloud
                else [{k: v for k, v in m.items() if k != "native_parts"} for m in messages]
            )
            emitted = False
            self._probing.add(candidate)
            self._active_candidate = candidate
            profile = current.get()
            interval = (
                profile.begin(
                    "llm_attempt",
                    provider=provider,
                    model=model,
                    attempt=index + 1,
                    fallback=index > 0,
                )
                if profile
                else None
            )
            if profile:
                profile.update(provider=provider, model=model, fallback=index > 0)
            self.phase = "switching" if index else "generating"
            self._notify()

            async def attempt(candidate=candidate, cloud=cloud, payload=payload):
                if not cloud:
                    async for chunk in candidate.chat_stream(payload, tools):
                        yield chunk
                    return
                try:
                    first_chunk = True
                    async with asyncio.timeout(
                        max(
                            0.001,
                            min(
                                candidate.cfg.attempt_timeout_s,
                                cloud_deadline - time.perf_counter(),
                            ),
                        )
                    ) as deadline:

                        def progress():
                            deadline.reschedule(
                                asyncio.get_running_loop().time()
                                + max(0.001, cloud_deadline - time.perf_counter())
                            )

                        progress_token = _wire_progress.set(progress)
                        try:
                            async for chunk in candidate.chat_stream(payload, tools):
                                if chunk.content:
                                    deadline.reschedule(
                                        asyncio.get_running_loop().time()
                                        + candidate.cfg.stream_idle_timeout_s
                                    )
                                    first_chunk = False
                                elif first_chunk and chunk.tool_calls:
                                    progress()
                                    first_chunk = False
                                yield chunk
                        finally:
                            _wire_progress.reset(progress_token)
                except TimeoutError:
                    raise GeminiRequestError(
                        None, transient=True, reason="общий таймаут облачной цепочки"
                    ) from None

            try:
                async for chunk in attempt():
                    usable = bool(chunk.content.strip() or chunk.tool_calls)
                    if usable and not emitted:
                        record("provider_first_chunk", time.perf_counter() - attempt_started)
                        self.last_provider = provider
                        self.last_model = self._model_label(candidate, chunk.stats.get("model"))
                        self._using_local |= not cloud
                        self.phase = "generating"
                        self._notify()
                        log.info(
                            "LLM selected provider=%s model=%s requested_model=%s",
                            provider,
                            self.last_model,
                            model,
                        )
                    if request_id and chunk.tool_calls:
                        self._routes[request_id] = candidate
                        if len(self._routes) > 32:
                            del self._routes[next(iter(self._routes))]
                    emitted |= usable
                    yield chunk
                if not emitted:
                    raise LLMError("Модель вернула пустой ответ.")
                succeeded = True
                self._cooldown_until.pop(candidate, None)
                return
            except Exception as exc:
                self.last_provider = self.last_model = None
                if request_id:
                    self._routes.pop(request_id, None)
                transient = isinstance(exc, GeminiRequestError) and exc.transient
                code = exc.status_code if isinstance(exc, GeminiRequestError) else None
                attempt_reason = (
                    "rate_limit"
                    if code == 429
                    else "timeout"
                    if isinstance(exc, TimeoutError)
                    or isinstance(exc, GeminiRequestError)
                    and "таймаут" in str(exc)
                    else "api_error"
                )
                if profile:
                    profile.update(
                        reason="rate_limit"
                        if code == 429
                        else "timeout"
                        if isinstance(exc, TimeoutError)
                        else type(exc).__name__
                    )
                self.switch_reason = {
                    429: "Достигнут лимит запросов Gemini",
                    503: "Gemini временно недоступен",
                    504: "Gemini не ответил вовремя",
                    401: "Нужно проверить API-ключ",
                    403: "Доступ к Gemini запрещён",
                    404: "Настроенная модель недоступна",
                }.get(
                    code,
                    "Сетевая ошибка или таймаут" if transient else "Ошибка модели или конфигурации",
                )
                log.warning(
                    "LLM failed provider=%s model=%s status=%s transient=%s",
                    provider,
                    model,
                    code,
                    transient,
                )
                if cloud:
                    self.last_error = str(exc) if isinstance(exc, LLMError) else "Ошибка Gemini."
                    if not transient or emitted:
                        raise
                    self._cooldown_until[candidate] = time.perf_counter() + 30
                    if code == 429:
                        self._cooldown_until[candidate] = time.perf_counter() + 60
                    if after_tool:
                        raise LLMError(
                            "Gemini недоступен после вызова инструмента. Часть запроса могла "
                            "выполниться; резервную цепочку автоматически не повторяю."
                        ) from None
                    if index + 1 < len(candidates):
                        continue
                    raise
                detail = ""
                if isinstance(exc, LLMError):
                    if "не поддерживает изображения" in str(exc):
                        detail = " Установленная локальная модель не поддерживает изображения."
                    elif "не установлена" in str(exc):
                        detail = " Локальная модель не установлена. Автозагрузки нет."
                    elif "лимита длины" in str(exc):
                        detail = " Локальная модель достигла лимита длины; ответ не завершён."
                raise LLMError(
                    "Gemini недоступен; резервная Ollama тоже не выполнила запрос." + detail
                ) from None
            finally:
                elapsed = time.perf_counter() - attempt_started
                self._probing.discard(candidate)
                if self._active_candidate is candidate:
                    self._active_candidate = None
                health = self._health.setdefault(candidate, {"successes": 0, "failures": 0})
                cancelled = bool(asyncio.current_task().cancelling()) or isinstance(
                    sys.exception(), (GeneratorExit, asyncio.CancelledError)
                )
                if profile:
                    profile.end(
                        interval,
                        "cancelled" if cancelled else "ok" if succeeded else "error",
                        reason=attempt_reason,
                        http_status=code,
                    )
                if not cancelled:
                    health["successes" if succeeded else "failures"] += 1
                health["latency_s"] = round(elapsed, 3)
                health["status"] = "cancelled" if cancelled else "ready" if succeeded else "failed"
                health["error"] = None if succeeded or cancelled else self.switch_reason
                self.phase = (
                    "idle"
                    if succeeded or cancelled
                    else "error"
                    if index == len(candidates) - 1
                    else "switching"
                )
                self._notify()
                record(f"{provider}_attempt_{index + 1}", elapsed, "ok" if succeeded else "error")
                log.info(
                    "LLM_TIMING provider=%s model=%s attempt=%d seconds=%.3f",
                    provider,
                    model,
                    index + 1,
                    elapsed,
                )

        self.phase = "error"
        self._notify()
        raise LLMError("Сейчас нет доступной модели. Попробуй ещё раз немного позже.")


def create_llm(s: Settings) -> LLMClient:
    # A cloud fallback must actually be local. Existing explicit Ollama mode stays compatible.
    if s.llm.provider == "gemini" and urlparse(s.llm.host).hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise LLMError("Резервная Ollama должна использовать loopback-адрес.")
    local = OllamaClient(s.llm)
    if s.llm.provider == "ollama":
        return local
    cloud = GeminiClient(s.gemini, api_key())
    if not cloud.available:
        log.warning(SETUP)
    secondary = (
        GeminiClient(s.gemini.model_copy(update={"model": s.gemini.fallback_model}), cloud._key)
        if s.gemini.fallback_model and s.gemini.fallback_model != s.gemini.model
        else None
    )
    return FallbackClient(cloud, local, s.llm.fallback, secondary=secondary)


def configured_label(s: Settings, client: LLMClient) -> str:
    if s.llm.provider == "ollama":
        return f"Ollama {s.llm.model}"
    if isinstance(client, FallbackClient) and not client.primary.available:
        return f"Ollama {s.llm.model} (Gemini не настроен)"
    chain = f" → {s.gemini.fallback_model}" if s.gemini.fallback_model else ""
    return f"Gemini {s.gemini.model}{chain}; резерв Ollama"
