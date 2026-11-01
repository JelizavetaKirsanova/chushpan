"""Opt-in neutral network/synthesis benchmark; never records or plays audio."""

from __future__ import annotations

import asyncio
import json
import time
import wave

import numpy as np

from backend.brain.conversation import Conversation
from backend.brain.providers import create_llm
from backend.latency import Profile, current
from backend.memory.store import MemoryStore
from backend.tools.clock import clock_tools
from backend.tools.registry import ToolRegistry


class NoNetwork:
    cloud_context = True

    async def chat_stream(self, *args, **kwargs):
        raise RuntimeError("Local benchmark unexpectedly requested a model")
        yield


async def run(
    settings, *, live=False, tts=False, runs=1, label="sample", stream=False, stt_file=None
) -> int:
    report = {"label": label, "microphone": False, "audio_played": False, "runs": []}
    if stt_file:
        from backend.audio.aec import resample
        from backend.audio.stt import WhisperSTT

        with wave.open(str(stt_file)) as source:
            rate = source.getframerate()
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getnframes() > rate * 30
            ):
                raise ValueError("Для STT-теста нужен mono PCM16 WAV длительностью до 30 секунд.")
            audio = np.frombuffer(source.readframes(source.getnframes()), dtype=np.int16)
        audio = resample(audio, rate)
        stt = WhisperSTT(
            settings.stt.model,
            settings.stt_language,
            settings.stt_hint,
            settings.resources.mlx_cache_mb,
        )
        report["stt"] = {
            "model": settings.stt.model,
            "language": settings.stt_language,
            "audio_duration_s": len(audio) / 16000,
            "recognition_s": [],
        }
        try:
            started = time.perf_counter()
            await stt.warmup()
            report["stt"]["load_and_warmup_s"] = time.perf_counter() - started
            for _ in range(runs):
                started = time.perf_counter()
                text = await stt.transcribe(audio)
                report["stt"]["recognition_s"].append(time.perf_counter() - started)
                report["stt"]["nonempty_transcript"] = bool(text)
        finally:
            stt._executor.shutdown(wait=True)
    for _ in range(runs):
        profile = Profile()
        token = current.set(profile)
        store = MemoryStore(":memory:")
        try:
            conv = Conversation(
                settings, NoNetwork(), store, tools=ToolRegistry(clock_tools(settings))
            )
            started = time.perf_counter()
            async for _piece in conv.reply("Сколько времени?"):
                pass
            row = {"local_time_s": time.perf_counter() - started}
            if live:
                client = create_llm(settings)
                started = time.perf_counter()
                first = None
                try:
                    async with asyncio.timeout(40):
                        async for chunk in client.chat_stream(
                            [
                                {"role": "system", "content": "Отвечай по-русски кратко."},
                                {
                                    "role": "user",
                                    "content": "Что такое радуга? Одно короткое предложение.",
                                },
                            ],
                            tools=[] if stream else None,
                        ):
                            if first is None and chunk.content.strip():
                                first = time.perf_counter() - started
                    row["model_first_text_s"] = first
                    row["transport"] = "text_stream" if stream else "buffered"
                    row["model_total_s"] = time.perf_counter() - started
                    row["provider"] = getattr(client, "last_provider", settings.llm.provider)
                    row["model"] = getattr(client, "last_model", None)
                except Exception as exc:
                    row["error_type"] = type(exc).__name__
                    row["model_total_s"] = time.perf_counter() - started
                finally:
                    await client.aclose()
            row["stages"] = profile.stages
            report["runs"].append(row)
        finally:
            store.close()
            current.reset(token)
    if tts:
        from backend.audio.tts import PiperSpeaker
        from backend.voice import make_speaker

        speaker = make_speaker(settings)
        if isinstance(speaker, PiperSpeaker):
            try:
                started = time.perf_counter()
                await speaker.warmup()
                report["tts_load_and_warmup_s"] = time.perf_counter() - started
                started = time.perf_counter()
                await asyncio.get_running_loop().run_in_executor(
                    speaker._synth_pool, speaker._synth, "Проверка скорости."
                )
                report["tts_generation_s"] = time.perf_counter() - started
            finally:
                speaker._synth_pool.shutdown(wait=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if any("error_type" in row for row in report["runs"]) else 0
