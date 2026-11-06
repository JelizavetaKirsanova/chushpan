"""CPU-only regression benchmark: real segmentation/splitting, no audio or device I/O."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from backend.audio.sentences import SentenceSplitter
from backend.audio.vad import FRAME_MS, FRAME_SAMPLES, UtteranceSegmenter
from backend.config import Settings, VADConfig
from backend.context import DeviceContext, RequestContext, request_scope
from backend.intents import ControlSession, compile_plan
from backend.security import Risk
from backend.tools.registry import Tool, ToolRegistry


async def benchmark_controls(runs):
    """Paired scheduling comparison; transport delay is deliberately simulated."""
    rows = []
    for mode in ("sequence", "parallel"):
        samples = []
        for _ in range(runs):
            calls = []

            async def led(command, _calls=calls):
                _calls.append("led")
                await asyncio.sleep(0.025)
                return {"written": True, "physical_confirmed": False}

            async def display(level, _calls=calls):
                _calls.append("display")
                await asyncio.sleep(0.025)
                return {"level": level}

            tools = ToolRegistry(
                [
                    Tool(
                        "led_control",
                        "fake LED",
                        led,
                        {"command": {"type": "string"}},
                        ["command"],
                        risk=Risk.CHANGE,
                    ),
                    Tool(
                        "set_brightness",
                        "fake display",
                        display,
                        {"level": {"type": "integer"}},
                        ["level"],
                        risk=Risk.CHANGE,
                    ),
                ]
            )
            conv = SimpleNamespace(id=1, s=Settings(), tools=tools, on_tool=None)
            command = "Сделай ленту розовой и яркость экрана 30%"
            if mode == "parallel":
                command = "Одновременно " + command
            c = RequestContext(DeviceContext.local_mac(), command)
            controls = ControlSession()
            with request_scope(c):
                plan = controls.plan(command, (c.binding, 1))
                start = time.perf_counter()
                result = await controls.execute(plan, conv)
                samples.append(time.perf_counter() - start)
            assert len(result["steps"]) == len(calls) == 2
        rows.append({"mode": mode, "seconds": samples, "median_s": statistics.median(samples)})
    return {"method": "real planner and guards; two mocked 25 ms tool handlers", "rows": rows}


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
    parser.add_argument("--runs", type=int, choices=range(1, 11), default=5)
    args = parser.parse_args()
    result = benchmark()
    result["controls"] = asyncio.run(benchmark_controls(args.runs))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
