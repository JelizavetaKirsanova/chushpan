"""Compare Whisper models on one recorded phrase: `uv run python -m backend bench-stt`."""

from __future__ import annotations

import time

from rich.console import Console
from rich.table import Table

from backend.config import Settings

console = Console(highlight=False)

DEFAULT_MODELS = [
    "mlx-community/whisper-large-v3-turbo",
    "mlx-community/whisper-medium-mlx",
    "mlx-community/whisper-small-mlx",
]


async def run_bench_stt(s: Settings, models: list[str]) -> int:
    from backend.audio.capture import Microphone
    from backend.audio.stt import WhisperSTT
    from backend.audio.vad import SileroVAD, UtteranceSegmenter

    models = models or DEFAULT_MODELS
    vad, segmenter = SileroVAD(), UtteranceSegmenter(s.vad)
    mic = Microphone(s.audio.input_device)
    mic.start()
    console.print("[bold cyan]Произнесите одно предложение[/] (запись остановится после паузы)…")
    audio = None
    try:
        async for frame in mic.frames():
            event = segmenter.process(frame, vad(frame))
            if event.started:
                console.print("[dim]запись…[/]")
            if event.utterance is not None:
                audio = event.utterance
                break
    finally:
        mic.stop()
    console.print(f"[dim]Записано {len(audio) / 16000:.1f} с речи.[/]\n")

    table = Table(title="Распознавание одной записи разными моделями")
    for col in ("Модель", "Загрузка", "Распознавание", "Текст"):
        table.add_column(col, overflow="fold")
    for model in models:
        stt = WhisperSTT(model, s.stt_language, s.stt_hint)
        with console.status(f"Загрузка {model} (скачивается при первом использовании)…"):
            t0 = time.perf_counter()
            await stt.warmup()
            load_s = time.perf_counter() - t0
        runs = []
        for _ in range(3):
            t0 = time.perf_counter()
            text = await stt.transcribe(audio)
            runs.append(time.perf_counter() - t0)
        table.add_row(model.split("/")[-1], f"{load_s:.1f}s", f"{min(runs):.2f}s", text)
    console.print(table)
    console.print(
        f"[dim]Выбор модели: JARVIS_STT__MODEL=mlx-community/<model> (сейчас {s.stt.model})[/]"
    )
    return 0
