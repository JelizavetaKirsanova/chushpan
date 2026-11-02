"""Reproducible local-model/PCM benchmark. No microphone, audio output or real tool handlers."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import time

from backend.automation.models import Block, Scenario
from backend.automation.service import Automations
from backend.brain.conversation import Conversation
from backend.brain.llm import OllamaClient
from backend.config import Settings
from backend.context import voice_addressed
from backend.latency import Profile, current
from backend.memory.store import MemoryStore
from backend.tools import Tool, ToolRegistry, build_registry
from backend.voice import make_speaker, reply_aloud

CASES = (
    ("led", "Сделай ленту розовой", ()),
    ("brightness", "Яркость на 50 процентов", ()),
    ("volume", "Сделай звук тише", ()),
    ("app", "Открой Safari", ()),
    ("compound", "Сделай ленту розовой и яркость MacBook 50 процентов", ()),
    ("scenario", "Включи вечерний свет", ()),
    ("dialogue", "Привет, как настроение? Ответь коротко.", ()),
    ("explanation", "Почему после дождя появляется радуга? Объясни двумя предложениями.", ()),
    (
        "context",
        "А почему так происходит? Ответь коротко.",
        (
            (
                "Почему листья меняют цвет осенью?",
                "Хлорофилл разрушается, и становятся заметны другие пигменты.",
            ),
        ),
    ),
    ("long_answer", "Расскажи, как образуются облака. Дай три коротких пункта.", ()),
)


def drain_pcm(audio_queue, *_callbacks):
    # Deliberately do not invoke playback callbacks: PCM readiness is not audible sound.
    while audio_queue.get() is not None:
        pass
    return True


def safe_registry(settings, store, calls):
    # Preserve the actual enabled tool descriptions/schemas and capability prompt size.
    # Reconstruct Tools so neither their handlers nor their MacAgent can be invoked.
    original = build_registry(settings, store=store, local_mac=False)
    items = []
    for source in original._tools.values():
        if source.name in {"run_scenario", "create_scenario"}:
            continue

        async def mocked(_name=source.name, **arguments):
            calls.append({"tool": _name, "arguments": arguments})
            if _name == "set_brightness":
                return {"level": arguments["level"]}
            if _name == "get_system_status":
                return {"sound": {"volume_percent": 40, "muted": False}}
            if _name == "set_volume":
                return {"ok": True, "volume_percent": arguments.get("level", 30), "muted": False}
            if _name == "open_app":
                return {"submitted": True, "opened": arguments["name"]}
            if _name == "led_control":
                return {"written": True, "delivery_state": "sent", "physical_confirmed": False}
            return {"error": "Интеграции заменены стендом измерений."}

        items.append(
            Tool(
                source.name,
                source.description,
                mocked,
                copy.deepcopy(source.parameters),
                list(source.required),
                dict(source.capability),
                risk=source.risk,
                reply=lambda result: result.get("error") or "Контрольное действие обработано.",
            )
        )
    registry = ToolRegistry(items)
    registry.automations = Automations(registry, settings, store)
    registry.automations.store.save(
        Scenario(
            name="Вечерний свет",
            phrases=["Включи вечерний свет"],
            actions=[Block(capability="mac.brightness", parameters={"level": 25})],
        )
    )
    return registry


async def benchmark(settings, *, label, runs, selected=None, model=None, think=None, num_ctx=None):
    settings = settings.model_copy(deep=True)
    settings.llm.provider = "ollama"  # Never instantiate a cloud client or send an API key.
    if model:
        settings.llm.model = model
    if think is not None:
        settings.llm.think = think
    if num_ctx is not None:
        settings.llm.num_ctx = num_ctx
    settings.user.name = "Пользователь"
    settings.user.form_of_address = None
    speaker = make_speaker(settings)
    if not hasattr(speaker, "_synth_pool"):
        raise ValueError(
            "Измерение поддерживает текущий нейронный голос; системный голос не включается."
        )
    speaker._play = drain_pcm
    client = OllamaClient(settings.llm)
    report = {
        "label": label,
        "method": (
            "real local LLM and TTS; synthetic prompts; mocked integrations; "
            "PCM readiness, not audible sound"
        ),
        "microphone": False,
        "audio_played": False,
        "cloud_requests": False,
        "configuration": {
            "model": settings.llm.model,
            "think": settings.llm.think,
            "num_ctx": settings.llm.num_ctx,
            "keep_alive": settings.llm.keep_alive,
            "tts_engine": settings.tts.engine,
            "voice": settings.voice_name,
        },
        "samples": [],
    }
    try:
        start = time.perf_counter()
        await speaker.warmup()
        report["tts_warmup_s"] = time.perf_counter() - start
        start = time.perf_counter()
        await client.warmup()
        report["model_warmup_s"] = time.perf_counter() - start
        for category, prompt, history in CASES:
            if selected and category not in selected:
                continue
            for iteration in range(runs):
                store = MemoryStore(":memory:")
                calls = []
                profile = Profile(kind="benchmark")
                token, addressed = current.set(profile), voice_addressed.set(True)
                try:
                    registry = safe_registry(settings, store, calls)
                    conversation = Conversation(settings, client, store, tools=registry)
                    for question, answer in history:
                        conversation.add_exchange(question, answer)
                    # Setup, models and SQLite creation are excluded from the request clock.
                    profile.origin = time.perf_counter()
                    try:
                        async with asyncio.timeout(90):
                            completed = await reply_aloud(
                                conversation,
                                speaker,
                                prompt,
                                settings.tts.sentence_min_chars,
                                on_token=lambda _: None,
                            )
                        profile.finish("completed" if completed else "cancelled")
                    except Exception as exc:
                        profile.fail(type(exc).__name__)
                        profile.finish("failed")
                    row = {
                        "category": category,
                        "iteration": iteration,
                        "status": profile.status,
                        "first_text_s": profile.marks.get("first_content_text"),
                        "first_pcm_ready_s": profile.marks.get("tts_audio_enqueued"),
                        "total_generation_s": profile.elapsed(),
                        "marks": profile.marks,
                        "metadata": profile.info,
                        "errors": list(profile.errors),
                        "stages": profile.stages,
                        "mock_calls": calls,
                    }
                    report["samples"].append(row)
                    print(
                        json.dumps(
                            {
                                k: row[k]
                                for k in (
                                    "category",
                                    "iteration",
                                    "status",
                                    "first_text_s",
                                    "first_pcm_ready_s",
                                    "total_generation_s",
                                )
                            }
                        ),
                        flush=True,
                    )
                finally:
                    current.reset(token)
                    voice_addressed.reset(addressed)
                    store.close()
        return report
    finally:
        await client.aclose()
        speaker._synth_pool.shutdown(wait=True)


async def benchmark_stt(settings, runs):
    """Fixed synthetic speech in RAM, cached STT only; never records or plays."""
    import numpy as np
    from huggingface_hub import snapshot_download
    from scipy.signal import resample_poly

    from backend.audio.stt import WhisperSTT
    from backend.local_commands import route

    stt = WhisperSTT(
        settings.stt.model,
        settings.stt_language,
        settings.stt_hint,
        settings.resources.mlx_cache_mb,
    )
    # Refuse to download: this benchmark is strictly local even on a fresh Mac.
    stt._path = snapshot_download(settings.stt.model, local_files_only=True)
    speaker = make_speaker(settings)
    if not hasattr(speaker, "_synth_pool"):
        raise ValueError("Для стенда нужен уже установленный нейронный голос.")
    report = {
        "method": "synthetic speech; cached local Whisper; no microphone or playback",
        "microphone": False,
        "audio_played": False,
        "cloud_requests": False,
        "model": settings.stt.model,
        "samples": [],
    }
    try:
        await speaker.warmup()
        start = time.perf_counter()
        await stt.warmup()
        report["warmup_s"] = time.perf_counter() - start
        phrases = [
            "Сделай ленту розовой",
            "Яркость экрана на пятьдесят процентов",
            "Сделай звук тише",
            "Открой Сафари",
            "Почему после дождя появляется радуга?",
        ]
        loop = asyncio.get_running_loop()
        for index, phrase in enumerate(phrases):
            pcm = await loop.run_in_executor(speaker._synth_pool, speaker._synth, phrase)
            from math import gcd

            divisor = gcd(speaker.sample_rate, 16000)
            audio = (
                resample_poly(
                    pcm.astype(np.float32), 16000 // divisor, speaker.sample_rate // divisor
                )
                .clip(-32768, 32767)
                .astype(np.int16)
            )
            for iteration in range(runs):
                start = time.perf_counter()
                text = await stt.transcribe(audio)
                row = {
                    "case": index,
                    "iteration": iteration,
                    "seconds": time.perf_counter() - start,
                    "recognized": text,
                    "route": route(text),
                    "confident": stt.last_speech_confident,
                }
                report["samples"].append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
        return report
    finally:
        await stt.unload()
        stt._executor.shutdown(wait=True)
        speaker._synth_pool.shutdown(wait=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, choices=["before", "after", "sample"])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--cases", nargs="+", choices=[case[0] for case in CASES])
    parser.add_argument("--model")
    parser.add_argument("--no-think", action="store_true")
    parser.add_argument("--num-ctx", type=int)
    parser.add_argument(
        "--stt", action="store_true", help="Synthetic offline STT; no LLM or playback"
    )
    args = parser.parse_args()
    if not 1 <= args.runs <= 10:
        parser.error("runs must be 1–10")
    if args.stt and (args.cases or args.model or args.no_think or args.num_ctx):
        parser.error("--stt does not use LLM options")
    logging.basicConfig(level=logging.ERROR)
    settings = Settings()
    report = (
        asyncio.run(benchmark_stt(settings, args.runs))
        if args.stt
        else asyncio.run(
            benchmark(
                settings,
                label=args.label,
                runs=args.runs,
                selected=args.cases,
                model=args.model,
                think=False if args.no_think else None,
                num_ctx=args.num_ctx,
            )
        )
    )
    output = settings.data_path / "latency-benchmarks"
    output.mkdir(parents=True, exist_ok=True)
    prefix = "stt-" if args.stt else ""
    target = output / f"{prefix}{args.label}-{time.time_ns()}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"report": str(target)}))


if __name__ == "__main__":
    main()
