"""Live wake word scores, for tuning the threshold: `uv run python -m backend wake-test`."""

from __future__ import annotations

import asyncio
import time

from rich.console import Console

from backend.audio.wakeword import WakeWordDetector, load_wake_model
from backend.config import Settings

console = Console(highlight=False)
BAR_WIDTH = 40


async def run_wake_test(s: Settings) -> int:
    if s.wakeword.phrase:
        return await _test_phrase(s)
    from backend.audio.capture import Microphone

    try:
        model = load_wake_model(s.wakeword_path, s.wakeword_dir)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    detector = WakeWordDetector(model, s.wakeword.threshold)
    mic = Microphone(s.audio.input_device)
    mic.start()
    threshold = s.wakeword.threshold
    mark = round(threshold * BAR_WIDTH)
    console.print(f"Скажите «Hey Jarvis» · модель {s.wakeword_path.name} · порог {threshold}")
    console.print("[dim]Показаны оценки выше 0,05. Ctrl+C — выход.[/]")

    peak, hits, last_hit = 0.0, 0, 0.0
    try:
        async for frame in mic.frames():
            for score in detector.scores(frame):
                now = time.monotonic()
                if score >= 0.05:
                    filled = round(score * BAR_WIDTH)
                    bar = "".join(
                        "█" if i < filled else ("|" if i == mark else "·") for i in range(BAR_WIDTH)
                    )
                    color = "green" if score >= threshold else "yellow"
                    console.print(f"[{color}]{bar}[/] {score:.3f}")
                peak = max(peak, score)
                if score >= threshold and now - last_hit > 1.5:  # one count per phrase
                    hits, last_hit = hits + 1, now
                    console.print(f"[bold green]✔ обнаружено ({hits})[/]")
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        mic.stop()
    console.print(f"\nМаксимальная оценка {peak:.3f} · активаций {hits}")
    return 0


async def _test_phrase(s: Settings) -> int:
    """Use the same VAD, recognizer and phrase matcher as the sleeping voice loop."""
    from backend.audio.capture import Microphone
    from backend.audio.stt import WhisperSTT, strip_wake_phrase
    from backend.audio.vad import SileroVAD, UtteranceSegmenter

    stt = WhisperSTT(s.stt.model, s.stt_language, s.stt_hint, s.resources.mlx_cache_mb)
    with console.status("Подготовка распознавания речи…"):
        await stt.warmup()
    vad, segmenter = SileroVAD(), UtteranceSegmenter(s.vad)
    mic = Microphone(s.audio.input_device)
    console.print(f"Скажите «{s.wakeword.phrase}». Фраза проверяется локально. Ctrl+C — выход.")
    hits = 0
    mic.start()
    try:
        async for frame in mic.frames():
            event = segmenter.process(frame, vad(frame))
            if event.utterance is None:
                continue
            text = await stt.transcribe(event.utterance)
            request = strip_wake_phrase(text, s.wakeword.phrase)
            if request is not None:
                hits += 1
                console.print(f"✔ Активация {hits}: {text}", markup=False)
            elif text:
                console.print(f"Без активации: {text}", markup=False)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        mic.stop()
    console.print(f"Активаций: {hits}")
    return 0
