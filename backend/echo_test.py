"""Measure echo cancellation in this room: `uv run python -m backend echo-test`.

Jarvis says a few sentences through the speakers while the microphone records. The same
frames go to two voice detectors, one raw and one after the echo canceller: every time the
raw one "hears speech", Jarvis would have interrupted itself with barge-in on.
"""

from __future__ import annotations

import asyncio

import numpy as np
from rich.console import Console

from backend.config import Settings

console = Console(highlight=False)

SENTENCES = {
    "ru": [
        "Это проверка эхоподавления.",
        "Пожалуйста, помолчите, пока я говорю.",
        "Проверяю, какая часть моего голоса попадает в микрофон.",
        "Завтра будет облачно, температура до двадцати четырёх градусов.",
    ],
    "pt": [
        "Este é um teste de cancelamento de eco.",
        "Fique em silêncio enquanto eu falo, por favor.",
        "Vou medir quanto da minha própria voz chega ao microfone.",
        "Amanhã o dia começa nublado, com máxima de vinte e quatro graus.",
    ],
    "en": [
        "This is an echo cancellation test.",
        "Please stay quiet while I speak.",
        "I am measuring how much of my own voice reaches the microphone.",
        "Tomorrow starts cloudy, with a high of twenty four degrees.",
    ],
}


def _db(frames: list[np.ndarray]) -> float:
    if not frames:
        return float("-inf")
    audio = np.concatenate(frames).astype(np.float64)
    return 10 * np.log10(np.mean(audio**2) + 1e-9)


class _Track:
    """One voice detector and what it heard."""

    def __init__(self, s: Settings) -> None:
        from backend.audio.vad import SileroVAD, UtteranceSegmenter

        self.vad = SileroVAD()
        self.segmenter = UtteranceSegmenter(s.vad)
        self.frames: list[np.ndarray] = []
        self.speech = 0
        self.starts = 0

    def feed(self, frame: np.ndarray, threshold: float) -> None:
        prob = self.vad(frame)
        self.frames.append(frame)
        self.speech += prob >= threshold
        if self.segmenter.process(frame, prob).started:
            self.starts += 1


async def run_echo_test(s: Settings) -> int:
    from backend.audio.aec import load_echo_canceller
    from backend.audio.capture import Microphone
    from backend.voice import make_speaker

    if s.tts.engine not in ("piper", "silero"):
        console.print("[red]Для эхоподавления нужен голос Piper или Silero[/]")
        return 1
    aec = load_echo_canceller()
    if aec is None:
        return 1
    speaker = make_speaker(s)
    await speaker.warmup()
    speaker.on_audio = aec.play
    raw, clean = _Track(s), _Track(s)
    mic = Microphone(s.audio.input_device)
    console.print(
        f"Помолчите: {s.assistant_name} будет говорить через динамики примерно 10 секунд."
    )

    async def sentences():
        for text in SENTENCES.get(s.locale.lang, SENTENCES["en"]):
            yield text

    mic.start()
    speaking = asyncio.create_task(speaker.speak(sentences()))
    try:
        async for frame in mic.frames():
            raw.feed(frame, s.vad.threshold)
            for cleaned in aec.process(frame):
                clean.feed(cleaned, s.vad.threshold)
            if speaking.done():
                break
    finally:
        mic.stop()
        speaker.interrupt()
        aec.close()

    n = max(1, len(raw.frames))
    reduction = _db(raw.frames) - _db(clean.frames)
    console.print(
        f"\nМикрофон во время речи {s.assistant_name} ({n * 32 / 1000:.1f} с):\n"
        f"  речь обнаружена   исходно {raw.speech / n:4.0%}"
        f"   после очистки {clean.speech / n:4.0%}\n"
        f"  самопрерывания   исходно {raw.starts:3d}   после очистки {clean.starts:3d}\n"
        f"  эхо уменьшено на {reduction:.0f} дБ"
    )
    if clean.starts == 0:
        console.print(
            f"[green]✔ {s.assistant_name} не слышит себя: можно прерывать его без наушников.[/]\n"
            "[dim]Включить: JARVIS_AUDIO__BARGE_IN=true[/]"
        )
    else:
        console.print(
            f"[yellow]! На этой громкости {s.assistant_name} слышит себя. "
            "Уменьшите громкость и повторите "
            "проверку или используйте наушники для прерывания голосом.[/]"
        )
    return 0
