"""CPU-only regression benchmark: real segmentation/splitting, no audio or device I/O."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from backend.audio.sentences import SentenceSplitter
from backend.audio.vad import FRAME_MS, FRAME_SAMPLES, UtteranceSegmenter
from backend.config import VADConfig
from backend.intents import compile_plan


def benchmark():
    text = " ".join(["Длинный ответ без пунктуации содержит слова для потоковой озвучки"] * 20)
    splitter = SentenceSplitter(protect_literals=True)
    first, emitted, position = None, [], 0
    started = time.perf_counter()
    for word in text.split(" "):
        position += len(word) + 1
        chunks = splitter.feed(word + " ")
        if chunks and first is None:
            first = position
        emitted.extend(chunks)
    if rest := splitter.flush():
        emitted.append(rest)
    segmenter = UtteranceSegmenter(VADConfig())
    frame = np.ones(FRAME_SAMPLES, dtype=np.int16)
    for _ in range(120):
        segmenter.process(frame, 0.95)
    endpoint_ms = segmenter.endpoint_frames * FRAME_MS
    serial = compile_plan("Сделай ленту розовой и яркость экрана 30%")
    parallel = compile_plan("Одновременно сделай ленту розовой и яркость экрана 30%")
    return {
        "method": "real CPU splitter, VAD and plan compiler; synthetic tokens/probabilities",
        "microphone": False,
        "audio_played": False,
        "network": False,
        "first_stream_chunk_input_chars": first,
        "stream_chunks": len(emitted),
        "text_preserved": " ".join(" ".join(emitted).split()) == text,
        "cpu_seconds": time.perf_counter() - started,
        "long_command_endpoint_ms": endpoint_ms,
        "sequence_actions": len(serial.actions) if serial else 0,
        "parallel_actions": len(parallel.actions) if parallel else 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = benchmark()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
