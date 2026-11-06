"""Microphone input as an async stream of 16 kHz int16 frames."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import numpy as np

from backend.audio.vad import FRAME_SAMPLES, SAMPLE_RATE

log = logging.getLogger(__name__)


class Microphone:
    def __init__(self, device: str | int | None = None, max_frames: int = 256) -> None:
        self.device = device
        self._queue: asyncio.Queue[tuple[int, np.ndarray]] = asyncio.Queue(maxsize=max_frames)
        self._stream = None
        self.dropped_frames = 0
        self.input_overflows = 0
        self._generation = 0
        self._stream_generation = 0

    def start(self) -> None:
        if self._stream is not None:
            return
        import sounddevice as sd

        loop = asyncio.get_running_loop()
        self._stream_generation += 1
        stream_generation = self._stream_generation

        def callback(indata, frames, time_info, status) -> None:  # audio thread
            if stream_generation != self._stream_generation:
                return
            if status:
                log.debug("input status: %s", status)
                if status.input_overflow:
                    self.input_overflows += 1
            generation = self._generation
            loop.call_soon_threadsafe(self._put, indata[:, 0].copy(), generation, stream_generation)

        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            device=self.device,
            callback=callback,
        )
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        self._stream = stream

    def set_device(self, device):
        if device == self.device:
            return
        previous, running = self.device, self._stream is not None
        self.stop()
        self.flush()
        self.device = device
        if running:
            try:
                self.start()
            except Exception:
                self.device = previous
                self.start()
                raise

    def _put(
        self,
        frame: np.ndarray,
        generation: int | None = None,
        stream_generation: int | None = None,
    ) -> None:
        if generation is not None and generation != self._generation:
            return
        if stream_generation is not None and stream_generation != self._stream_generation:
            return
        if self._queue.full():
            self.dropped_frames += 1
            self._queue.get_nowait()  # drop the oldest frame rather than block audio
        self._queue.put_nowait((self._generation, frame))

    def flush(self) -> None:
        self._generation += 1
        while not self._queue.empty():
            self._queue.get_nowait()

    async def frames(self) -> AsyncIterator[np.ndarray]:
        while True:
            try:
                generation, frame = await asyncio.wait_for(self._queue.get(), timeout=10)
            except TimeoutError:
                log.error("ERROR microphone_no_frames restarting_stream")
                self.stop()
                self.flush()
                # Device removal can outlast one retry. Keep the watchdog alive;
                # cancellation still interrupts the backoff immediately.
                for attempt in range(3):
                    try:
                        self.start()
                        break
                    except Exception:
                        log.error("ERROR microphone_restart attempt=%d", attempt + 1)
                        if attempt == 2:
                            raise
                        await asyncio.sleep(0.5 * (attempt + 1))
                log.info("LISTENING_RESUMED microphone_stream")
                continue
            if generation == self._generation:
                yield frame

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        self._stream_generation += 1
        if stream is not None:
            try:
                stream.stop()
            except Exception as exc:
                log.warning("MIC_STOP_FAILED type=%s", type(exc).__name__)
            finally:
                try:
                    stream.close()
                except Exception as exc:
                    log.warning("MIC_CLOSE_FAILED type=%s", type(exc).__name__)


class SilenceWatch:
    """Notices a microphone that only delivers zeros: macOS does that, with no error, when
    the app running Jarvis was denied microphone access (common when launched at login)."""

    def __init__(self, frames: int = 94) -> None:  # 94 frames of 32 ms = 3 s
        self.frames = frames
        self.seen = 0
        self.done = False

    def __call__(self, frame: np.ndarray) -> bool:
        """True once, when the first `frames` frames were all digital silence."""
        if self.done:
            return False
        if frame.any():
            self.done = True
            return False
        self.seen += 1
        if self.seen >= self.frames:
            self.done = True
            return True
        return False


def input_devices():
    """Metadata only; never opens a recording stream."""
    import sounddevice as sd

    return [
        {"id": i, "name": d["name"]}
        for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0
    ]
