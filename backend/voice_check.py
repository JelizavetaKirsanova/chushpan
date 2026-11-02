"""Offline activation replay: native VAD/Whisper, silent sink, no microphone or tools."""

import asyncio
import json
import time
import wave
from types import SimpleNamespace

import numpy as np

from backend.audio.aec import resample
from backend.audio.stt import WhisperSTT
from backend.audio.vad import FRAME_SAMPLES, SAMPLE_RATE, SileroVAD, UtteranceSegmenter
from backend.config import ROOT_DIR
from backend.voice import VoiceLoop, make_speaker


class SilentSink:
    on_sentence = None
    interrupted = False

    def __init__(self):
        self.responses = 0

    async def speak(self, sentences, on_start=None):
        self.responses += 1
        async for _ in sentences:
            if on_start:
                on_start()
        return True

    def interrupt(self):
        self.interrupted = True


class GateProbe:
    """Counts accepted requests. Deliberately has no LLM, registry, database or device."""

    def __init__(self, settings):
        self.s = settings
        self.received = 0
        self.last_stats = SimpleNamespace(
            first_token_s=None, tokens_per_second=None, tools=[], quiet=False
        )

    async def reply(self, _text):
        self.received += 1
        yield "Тестовый ответ."

    def add_exchange(self, *_):
        pass


async def replay(settings, stt, cases):
    """PCM frames use a sample clock; model execution is awaited, never a live input stream."""
    results = []
    for name, audio in cases:
        conv, speaker, now = GateProbe(settings), SilentSink(), [0.0]
        stt.last_confidence = {}
        loop = VoiceLoop(
            settings,
            conv,
            stt,
            speaker,
            SileroVAD(),
            UtteranceSegmenter(settings.vad),
            clock=lambda now=now: now[0],
            wake_phrase=settings.wakeword.phrase if settings.wakeword.enabled else None,
        )
        if settings.wakeword.enabled and not loop.use_wake:
            raise ValueError("Этот replay проверяет фразу Whisper; ONNX проверяется отдельно.")
        started = time.perf_counter()
        try:
            padded = np.pad(audio, (0, int(settings.vad.silence_ms * 16 + SAMPLE_RATE)))
            for offset in range(0, len(padded) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
                now[0] += FRAME_SAMPLES / SAMPLE_RATE
                loop.on_frame(padded[offset : offset + FRAME_SAMPLES])
                await asyncio.sleep(0)
            if loop.wake_task:
                await loop.wake_task
            if loop.task:
                await loop.task
            await asyncio.sleep(0)
            results.append(
                {
                    "case": name,
                    "wake_enabled": loop.use_wake,
                    "duration_s": len(audio) / SAMPLE_RATE,
                    "processing_s": time.perf_counter() - started,
                    "accepted_requests": conv.received,
                    "silent_responses": speaker.responses,
                    "state": loop.state.state.value,
                    "native_evidence": dict(stt.last_confidence) or None,
                }
            )
        finally:
            loop._close_session()
    return results


async def run(settings, *, decode=False):
    report = {
        "method": "offline PCM -> real Silero VAD -> real Whisper -> VoiceLoop; "
        "no microphone, LLM, tools or audio output; response sink is simulated",
        "limits": "Recorded cough/click and synthesized speech; no real room/TV recording.",
        "wake_enabled": settings.wakeword.enabled,
        "results": [],
    }
    if decode:
        stt = WhisperSTT(
            settings.stt.model,
            settings.stt_language,
            settings.stt_hint,
            settings.resources.mlx_cache_mb,
        )
        synth = make_speaker(settings)
        if not hasattr(synth, "_synth_pool"):
            raise ValueError("Replay требует локальный Piper/Silero, без macOS say.")
        try:
            await stt.warmup()
            cases = [
                ("silence", np.zeros(5 * SAMPLE_RATE, np.int16)),
                (
                    "stationary_noise",
                    np.random.default_rng(5).normal(0, 700, 5 * SAMPLE_RATE).astype(np.int16),
                ),
            ]
            for name, text in [
                ("background_speech", "Помоги выбрать способ отдохнуть после учёбы."),
                ("named_command", "Эй, Чушпан, что такое радуга?"),
            ]:
                pcm = await asyncio.get_running_loop().run_in_executor(
                    synth._synth_pool, synth._synth, text
                )
                cases.append((name, resample(pcm, synth.sample_rate)))
            for name, filename in [("cough", "4-157296-A-24.wav"), ("click", "1-118206-A-31.wav")]:
                with wave.open(str(ROOT_DIR / "tests/fixtures/audio" / filename)) as recording:
                    pcm = np.frombuffer(recording.readframes(recording.getnframes()), dtype="<i2")
                    cases.append((name, resample(pcm, recording.getframerate())))
            report["results"] = await replay(settings, stt, cases)
        finally:
            stt._executor.shutdown(wait=True)
            synth._synth_pool.shutdown(wait=True)
        output = settings.data_path / "diagnostics/voice-check.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_file"] = str(output)
    else:
        report["next_step"] = "Добавь --decode для локального распознавания тестовых файлов."
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0
