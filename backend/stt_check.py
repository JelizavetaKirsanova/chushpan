"""Explicit, bounded STT diagnostics. Audio is held in RAM and never saved."""

from __future__ import annotations

import asyncio
import time

import numpy as np
from rich.console import Console

from backend.audio.capture import Microphone
from backend.audio.stt import WhisperSTT
from backend.audio.vad import SAMPLE_RATE, SileroVAD, UtteranceSegmenter


def audio_metrics(audio: np.ndarray) -> dict:
    samples = audio.astype(np.float32) / 32768
    return {
        "duration_s": len(audio) / SAMPLE_RATE,
        "rms_dbfs": float(20 * np.log10(max(float(np.sqrt(np.mean(samples**2))), 1e-9))),
        "clipped_samples": int(np.count_nonzero(np.abs(audio.astype(np.int32)) >= 32767)),
    }


async def run(s, record: bool = False, models: list[str] | None = None) -> int:
    import sounddevice as sd

    console = Console(highlight=False, markup=False)
    console.print(f"STT: {s.stt.model}; язык: {s.stt_language}; движок: mlx-whisper")
    console.print(f"VAD: {s.vad.model_dump()}")
    try:
        device = sd.query_devices(s.audio.input_device, "input")
        sd.check_input_settings(
            device=s.audio.input_device, channels=1, dtype="int16", samplerate=SAMPLE_RATE
        )
    except (sd.PortAudioError, ValueError) as exc:
        console.print(
            f"Микрофон недоступен: {type(exc).__name__}. Проверь устройство и разрешения."
        )
        return 1
    console.print(
        f"Микрофон: {device['name']}; системная частота: {device['default_samplerate']} Гц"
    )
    console.print("Поток: 16000 Гц, mono int16; преобразование частоты — CoreAudio/PortAudio.")
    if not record:
        console.print("Запись выключена. Для одной записи в RAM: stt-check --record (до 20 с).")
        return 0
    console.print("Запись включается сейчас. Произнеси одну фразу; запись завершится после паузы.")
    mic = Microphone(s.audio.input_device)
    segmenter = UtteranceSegmenter(s.vad)
    vad = SileroVAD()
    try:
        mic.start()
        async with asyncio.timeout(20):
            async for frame in mic.frames():
                event = segmenter.process(frame, vad(frame))
                if event.utterance is not None:
                    audio = event.utterance
                    break
    except TimeoutError:
        console.print("Лимит записи: завершённой фразы нет; распознавание не выполнялось.")
        return 1
    finally:
        mic.stop()
    console.print(audio_metrics(audio))
    console.print(
        f"Завершение: {event.end_reason}; возможное начало на границе буфера: "
        f"{event.onset_at_buffer_start}; потери кадров: {mic.dropped_frames}; "
        f"переполнения входа: {mic.input_overflows}"
    )
    console.print("Границы VAD — признаки риска, а не доказательство обрезания слов.")
    for model in models or [s.stt.model]:
        stt = None
        try:
            if model.startswith("faster-whisper:"):
                from faster_whisper import WhisperModel

                started = time.perf_counter()
                engine = await asyncio.to_thread(
                    WhisperModel, model.split(":", 1)[1], device="cpu", compute_type="int8"
                )
                load_s = time.perf_counter() - started

                def transcribe(engine=engine):
                    segments, _ = engine.transcribe(
                        audio.astype(np.float32) / 32768,
                        language=s.stt_language,
                        initial_prompt=s.stt_hint,
                        condition_on_previous_text=False,
                        temperature=0,
                        vad_filter=False,
                    )
                    return " ".join(segment.text for segment in segments).strip()

                started = time.perf_counter()
                text = await asyncio.to_thread(transcribe)
                raw = text
            else:
                stt = WhisperSTT(model, s.stt_language, s.stt_hint)
                started = time.perf_counter()
                await stt.warmup()
                load_s = time.perf_counter() - started
                started = time.perf_counter()
                text = await stt.transcribe(audio)
                raw = stt.last_raw
            elapsed = time.perf_counter() - started
            console.print(f"{model}: загрузка {load_s:.2f} с; распознавание {elapsed:.2f} с")
            console.print(f"Исходный текст: {raw}\nПосле фильтра: {text}")
        except Exception as exc:
            console.print(f"{model}: ошибка {type(exc).__name__}; модель не подменялась.")
            return 1
        finally:
            if stt is not None:
                await stt.unload()
                stt._executor.shutdown(wait=True)
    return 0
