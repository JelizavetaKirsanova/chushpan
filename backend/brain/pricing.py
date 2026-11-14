"""Dated local estimates from the maintained LiteLLM model-pricing map."""

from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx

from backend.config import USER_ROOT

SOURCE_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
SOURCE_PAGE = "https://github.com/BerriAI/litellm"
MAX_SOURCE_BYTES = 8_000_000


def cache_path() -> Path:
    return USER_ROOT / "data" / "openai_pricing.json"


def refresh(path: Path | None = None, *, client=None) -> dict:
    target = path or cache_path()
    owned = client is None
    http = client or httpx.Client(timeout=15, follow_redirects=True)
    try:
        response = http.get(SOURCE_URL)
    finally:
        if owned:
            http.close()
    response.raise_for_status()
    if len(response.content) > MAX_SOURCE_BYTES:
        raise ValueError("Ценовой каталог превышает допустимый размер.")
    source = response.json()
    if not isinstance(source, dict):
        raise ValueError("Формат ценового каталога не распознан.")
    prices = {}
    for model, entry in source.items():
        if not isinstance(model, str) or not isinstance(entry, dict):
            continue
        if entry.get("litellm_provider") != "openai" or entry.get("mode") not in {
            "chat",
            "completion",
        }:
            continue
        input_rate = entry.get("input_cost_per_token")
        output_rate = entry.get("output_cost_per_token")
        if not all(
            isinstance(rate, (int, float)) and math.isfinite(rate) and rate >= 0
            for rate in (input_rate, output_rate)
        ):
            continue
        prices[model] = {"input_per_token": input_rate, "output_per_token": output_rate}
    if not prices:
        raise ValueError("В ценовом каталоге нет тарифов OpenAI для текстовых моделей.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "source": SOURCE_PAGE,
                "checked_at": datetime.now(UTC).isoformat(),
                "prices": prices,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(target)
    return {
        "source": SOURCE_PAGE,
        "checked_at": datetime.now(UTC).isoformat(),
        "models": len(prices),
    }


def estimate(model: str, input_tokens: int | None, output_tokens: int | None, path=None):
    if input_tokens is None or output_tokens is None:
        return None
    target = path or cache_path()
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
        rate = document["prices"][model]
        value = (
            int(input_tokens) * rate["input_per_token"]
            + int(output_tokens) * rate["output_per_token"]
        )
        return round(value, 10) if math.isfinite(value) and value >= 0 else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
