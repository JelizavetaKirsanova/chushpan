"""Repeatable local-only latency sample. Temporary SQLite and mocked device transport."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.automation.models import Block, Scenario  # noqa: E402
from backend.automation.service import Automations  # noqa: E402
from backend.brain.conversation import Conversation  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.memory.store import MemoryStore  # noqa: E402
from backend.security import Risk  # noqa: E402
from backend.tools.registry import Tool, ToolRegistry  # noqa: E402


class IsolatedSettings(Settings):
    model_config = {**Settings.model_config, "yaml_file": None, "yaml_file_encoding": None}

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, **kwargs):
        return (init_settings,)  # No .env, private configuration or external services.


class NoModel:
    async def chat_stream(self, *args, **kwargs):
        raise AssertionError("Local commands must never call the model")
        yield  # pragma: no cover


def summary(samples):
    ordered = sorted(samples)
    return {
        "samples": len(samples),
        "median_ms": round(statistics.median(samples), 4),
        "p95_ms": round(ordered[max(0, int(len(samples) * 0.95) - 1)], 4),
        "max_ms": round(max(samples), 4),
    }


async def run(count=100, phrase_count=1500):
    settings = IsolatedSettings(
        language="ru-RU", assistant_name="Чушпан", location={"timezone": "Europe/Tallinn"}
    )
    with tempfile.TemporaryDirectory(prefix="chushpan-latency-") as directory:
        memory = MemoryStore(Path(directory) / "sample.sqlite")
        device = AsyncMock(side_effect=lambda level: {"level": level})
        tools = ToolRegistry(
            [
                Tool(
                    "set_brightness",
                    "Mock display",
                    device,
                    {"level": {"type": "integer", "minimum": 0, "maximum": 100}},
                    ["level"],
                    risk=Risk.CHANGE,
                )
            ]
        )
        service = Automations(tools, settings, memory)
        service.store.save(
            Scenario(
                name="Sample",
                phrases=[f"Мой ритуал номер {i}" for i in range(phrase_count)],
                actions=[Block(capability="mac.brightness", parameters={"level": 30})],
            )
        )
        conv = Conversation(settings, NoModel(), memory, tools=tools)
        cold = time.perf_counter()
        assert service.recognize(f"Мой ритуал номер {phrase_count - 1}")["scenario_id"]
        cold_ms = (time.perf_counter() - cold) * 1000

        async def request(text):
            start = time.perf_counter()
            answer = "".join([chunk async for chunk in conv.reply(text)])
            assert "подтверждён" not in answer and "Нужно подтверждение" not in answer
            return (time.perf_counter() - start) * 1000

        baseline, integrated, scenarios, lookup = [], [], [], []
        for i in range(count + 10):
            # Interleave order to reduce effects of startup/cache drift.
            for enabled in [False, True] if i % 2 else [True, False]:
                tools.automations = service if enabled else None
                elapsed = await request("Поставь яркость Mac 30 процентов")
                if i >= 10:
                    (integrated if enabled else baseline).append(elapsed)
            tools.automations = service
            elapsed = await request(f"Мой ритуал номер {i % phrase_count}")
            started = time.perf_counter()
            assert service.recognize(f"Мой ритуал номер {phrase_count - 1}")["scenario_id"]
            lookup_ms = (time.perf_counter() - started) * 1000
            if i >= 10:
                scenarios.append(elapsed)
                lookup.append(lookup_ms)
        memory.close()
        return {
            "scope": "temporary SQLite, mocked display; excludes STT/TTS and physical devices",
            "phrases": phrase_count,
            "cold_index_ms": round(cold_ms, 4),
            "known_command_baseline": summary(baseline),
            "known_command_with_scenarios": summary(integrated),
            "known_command_median_delta_ms": round(
                statistics.median(integrated) - statistics.median(baseline), 4
            ),
            "scenario_phrase_end_to_end": summary(scenarios),
            "cached_exact_lookup": summary(lookup),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--phrases", type=int, default=1500)
    args = parser.parse_args()
    if args.samples < 1 or args.phrases < 1:
        parser.error("samples and phrases must be positive")
    print(json.dumps(asyncio.run(run(args.samples, args.phrases)), ensure_ascii=False, indent=2))
