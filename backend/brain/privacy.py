"""Best-effort text secret filtering before any LLM request; never an image redactor."""

from __future__ import annotations

import json
import re
from typing import Any

_SECRET_KEY = re.compile(
    r"password|passwd|парол|токен|token|api.?key|secret|authorization|cookie", re.I
)
_LABEL = re.compile(
    r"((?:password|passwd|пароль|токен|token|api[_ -]?key|secret|authorization|cookie)"
    r"\s*(?:[:=]|это\s|равен\s)\s*)(?:\"[^\"\n]*\"|'[^'\n]*'|[^\n;]+)",
    re.I,
)
_SECRET_PHRASE = re.compile(r"((?:мой|моя|my)\s+(?:пароль|password|токен|token)\s+)[^\n;]+", re.I)
_LOOSE_SECRET = re.compile(
    r'((?:пароль|password|токен|token|api[_ -]?key)\s+)[\w@.-]{3,}(?=$|[!,;?"\'])', re.I
)
_PATTERNS = (
    re.compile(r"\bAQ\.[A-Za-z0-9_-]{25,}\b"),
    re.compile(r"\bAIza[\w-]{20,}\b"),
    re.compile(r"\bsk-[\w-]{16,}\b"),
    re.compile(r"\bBearer\s+[^\s\"']+", re.I),
    re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+\b"),
    re.compile(r"[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@[^\s]+", re.I),
)
REDACTED = "[СЕКРЕТ СКРЫТ]"


def contains_secret(text: str) -> bool:
    """Memory is more conservative than ordinary technical chat about tokens."""
    return bool(
        redact(text) != text
        or _SECRET_KEY.search(text)
        or re.search(r"\b[A-Za-z0-9_-]{40,}\b", text)
    )


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
                cleaned = redact(parsed, secrets)
                if cleaned != parsed:
                    return json.dumps(cleaned, ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        for secret in secrets:
            if secret:
                value = value.replace(secret, REDACTED)
        value = _LABEL.sub(lambda m: m[1] + REDACTED, value)
        value = _SECRET_PHRASE.sub(lambda m: m[1] + REDACTED, value)
        value = _LOOSE_SECRET.sub(lambda m: m[1] + REDACTED, value)
        for pattern in _PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value
    if isinstance(value, dict):
        return {
            k: REDACTED if _SECRET_KEY.search(str(k)) else redact(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    return value
