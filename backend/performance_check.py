"""Explicit full-pipeline replay benchmark. Uses real models and audio output, no microphone."""

import asyncio
import functools
import json
import time

import numpy as np

from backend.audio.aec import resample
from backend.audio.stt import WhisperSTT
from backend.brain.conversation import Conversation
from backend.brain.providers import create_llm
from backend.hud.bus import HudBus
from backend.memory.store import MemoryStore
from backend.tools import build_registry

SCENARIOS = (
    ("local", "Чушпан, сколько времени?"),
    ("definition", "Что такое радуга? Ответь одним предложением."),
    ("dialogue", "Помоги выбрать способ отдохнуть после учёбы. Дай три конкретных совета."),
)


async def run(settings, *, runs=2, label="sample", with_vad=False):
    import backend.voice as voice

    speaker = voice.make_speaker(settings)
    if not hasattr(speaker, "_synth_pool"):
        raise ValueError("Полный replay требует текущий Piper/Silero; настройки не изменены.")
    stt = WhisperSTT(
        settings.stt.model,
        settings.stt_language,
        settings.stt_hint,
        settings.resources.mlx_cache_mb,
    )
    output = settings.data_path / "performance-benchmark"
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "label": label,
        "method": "fixed synthesized utterances; real STT/LLM/TTS/audio output; no microphone; "
        + (
            "real Silero VAD endpoint; origin is last VAD speech frame"
            if with_vad
            else "excludes VAD endpointing"
        ),
        "runs": [],
    }
    echo, print_line = voice.echo, voice.console.print
    voice.echo = lambda _: None
    voice.console.print = lambda *args, **kwargs: None
    try:
        started = time.perf_counter()
        await asyncio.gather(speaker.warmup(), stt.warmup())
        await speaker.prepare_wait_phrases()
        report["warmup_s"] = time.perf_counter() - started
        for provider in dict.fromkeys((settings.llm.provider, "ollama")):
            configured = settings.model_copy(deep=True)
            configured.llm.provider = provider
            client = create_llm(configured)
            try:
                await getattr(client, "local", client).warmup()
                for scenario, prompt in SCENARIOS:
                    path = output / (scenario + ".npy")
                    if path.exists():
                        audio = np.load(path, allow_pickle=False)
                    else:
                        audio = await asyncio.get_running_loop().run_in_executor(
                            speaker._synth_pool, speaker._synth, prompt
                        )
                        audio = resample(audio, speaker.sample_rate)
                        np.save(path, audio)
                    for iteration in range(runs):
                        store = MemoryStore(":memory:")
                        try:
                            bus = HudBus()
                            conversation = Conversation(
                                configured,
                                client,
                                store,
                                tools=build_registry(configured, store=store),
                            )
                            conversation.add_exchange("Ты здесь?", "Да, слушаю.")
                            loop = voice.VoiceLoop(
                                configured, conversation, stt, speaker, None, None, bus=bus
                            )
                            loop.state.to(voice.State.THINKING)
                            started = time.perf_counter()
                            endpoint_delay = 0
                            utterance = audio
                            if with_vad:
                                from backend.audio.vad import (
                                    FRAME_SAMPLES,
                                    SileroVAD,
                                    UtteranceSegmenter,
                                )

                                vad, segmenter = SileroVAD(), UtteranceSegmenter(configured.vad)
                                padded = np.pad(
                                    audio, (0, int(configured.vad.silence_ms * 16 + 16000))
                                )
                                for offset in range(
                                    0, len(padded) - FRAME_SAMPLES + 1, FRAME_SAMPLES
                                ):
                                    frame = padded[offset : offset + FRAME_SAMPLES]
                                    event = segmenter.process(frame, vad(frame))
                                    if event.utterance is not None:
                                        utterance, endpoint_delay = event.utterance, event.silence_s
                                        break
                                else:
                                    raise RuntimeError("VAD не завершил контрольную реплику")
                            loop.vad_wait_s = endpoint_delay
                            speech_end = time.perf_counter() - endpoint_delay
                            loop.task = asyncio.create_task(
                                loop._bounded_turn(
                                    functools.partial(
                                        loop.handle, utterance, speech_end, input_mode="replay"
                                    )
                                )
                            )
                            await loop.task
                            stats = next((e for e in bus.snapshot() if e["type"] == "stats"), {})
                            trace = store.performance.detail(stats.get("trace_id", ""))
                            report["runs"].append(
                                {
                                    "scenario": scenario,
                                    "requested_provider": provider,
                                    "iteration": iteration,
                                    "elapsed_s": time.perf_counter() - started,
                                    "vad_endpoint_s": endpoint_delay,
                                    "first_content_audio_s": stats.get("voice_s"),
                                    "stages": stats.get("stages", {}),
                                    "trace": trace,
                                }
                            )
                            (output / (label + ("-vad" if with_vad else "") + ".json")).write_text(
                                json.dumps(report, ensure_ascii=False, indent=2)
                            )
                        finally:
                            store.close()
            finally:
                await client.aclose()
    finally:
        voice.echo, voice.console.print = echo, print_line
        speaker._synth_pool.shutdown(wait=True)
        stt._executor.shutdown(wait=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(
        any(r["trace"] is None or r["trace"]["status"] != "completed" for r in report["runs"])
    )
