"""Local text-to-speech: Piper, Silero, or the macOS `say` command."""

from __future__ import annotations

import asyncio
import queue
import re
import threading
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from typing import Protocol

import numpy as np

from backend.latency import measure

OnStart = Callable[[], None]
OnLevel = Callable[[float], None]
OnAudio = Callable[[np.ndarray], None]
OnSentence = Callable[[str, float], None]  # text, seconds of speech; on the event loop

SAY_S_PER_CHAR = 0.065  # `say` gives no duration: about this long per character


def apply_pronunciations(text: str, pronunciations: dict[str, str]) -> str:
    """Keep display text intact while adding explicit pronunciation to the spoken text."""
    if not pronunciations:
        return text
    names = "|".join(re.escape(name) for name in sorted(pronunciations, key=len, reverse=True))
    lookup = {name.casefold(): spoken for name, spoken in pronunciations.items()}
    return re.sub(
        rf"\b(?:{names})\b", lambda m: lookup[m.group().casefold()], text, flags=re.IGNORECASE
    )


def speech_text(text: str, pronunciations: dict[str, str], language: str | None) -> str:
    if language == "ru":
        from backend.audio.russian import normalize
        from backend.audio.sentences import clean_for_speech

        return clean_for_speech(normalize(text, pronunciations), "ru")
    return apply_pronunciations(text, pronunciations)


class Speaker(Protocol):
    # Each sentence as it starts to play, so the HUD can write it in step with the voice.
    on_sentence: OnSentence | None

    @property
    def speaking(self) -> bool: ...

    @property
    def interrupted(self) -> bool:
        """True after `interrupt()`, until the next `speak()`."""
        ...

    async def warmup(self) -> None: ...

    async def speak(self, sentences: AsyncIterator[str], on_start: OnStart | None = None) -> bool:
        """Speak sentences as they arrive. Returns False if interrupted."""
        ...

    async def prepare(self, sentences: list[str]) -> None:
        """Synthesize ahead of time, so speaking them later starts and flows at once."""
        ...

    def interrupt(self) -> None: ...


class PiperSpeaker:
    speech_language: str | None = None

    def __init__(
        self,
        voice_path: Path,
        device: str | int | None = None,
        length_scale: float | None = None,
        pronunciations: dict[str, str] | None = None,
    ) -> None:
        self.voice_path = voice_path
        self.device = device
        self.length_scale = length_scale
        self.pronunciations = pronunciations or {}
        self._voice = None
        self._synth_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")
        self._ready: dict[str, np.ndarray] = {}  # sentences synthesized by `prepare`
        self.wait_audio: dict[str, np.ndarray] = {}
        self.wait_durations: dict[str, float] = {}
        self._stop = threading.Event()
        self._playback_lock = asyncio.Lock()
        self.recent_spoken: list[str] = []
        self._speaking = False
        self.on_level: OnLevel | None = None  # loudness of each block played, for the HUD
        # Each block played, at 16 kHz, as it goes to the speakers: the echo canceller's
        # reference. Called from the audio thread.
        self.on_audio: OnAudio | None = None
        self.on_sentence: OnSentence | None = None

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def interrupted(self) -> bool:
        return self._stop.is_set()

    def _load(self) -> None:
        if self._voice is None:
            from piper import PiperVoice

            self._voice = PiperVoice.load(self.voice_path)

    def _synth(self, text: str) -> np.ndarray:
        from piper import SynthesisConfig

        self._load()
        cfg = SynthesisConfig(length_scale=self.length_scale)
        spoken = speech_text(text, self.pronunciations, self.speech_language)
        chunks = [c.audio_int16_array for c in self._voice.synthesize(spoken, syn_config=cfg)]
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)

    @property
    def sample_rate(self) -> int:
        return self._voice.config.sample_rate

    def _play(
        self,
        audio_q: queue.Queue,
        on_start: OnStart | None,
        on_level: OnLevel | None,
        on_sentence: OnSentence | None = None,
    ) -> bool:
        import sounddevice as sd

        from backend.audio.aec import resample
        from backend.audio.level import level
        from backend.audio.vad import SAMPLE_RATE

        rate = self.sample_rate
        block = rate // 20  # 50 ms: small enough for instant interruption
        started = False
        stop = getattr(audio_q, "stop", self._stop)
        on_audio = self.on_audio
        with sd.OutputStream(samplerate=rate, channels=1, dtype="int16", device=self.device) as out:
            while (item := audio_q.get()) is not None:
                sentence, audio = item
                self.recent_spoken.append(sentence)
                self.recent_spoken[:] = self.recent_spoken[-8:]
                if on_sentence:
                    on_sentence(sentence, len(audio) / rate)
                # Resampled per sentence, not per block: no filter edges every 50 ms.
                ref = resample(audio, rate) if on_audio else None
                for i in range(0, len(audio), block):
                    if stop.is_set():
                        out.abort()
                        return False
                    self.last_audio_at = time.perf_counter()
                    if on_level:
                        on_level(level(audio[i : i + block]))
                    if ref is not None:
                        on_audio(ref[i * SAMPLE_RATE // rate : (i + block) * SAMPLE_RATE // rate])
                    if not started:
                        self.playback_started_at = time.perf_counter()
                        if on_start:
                            on_start()
                        started = True
                    out.write(audio[i : i + block])
        if on_level:
            on_level(0.0)
        return True

    async def warmup(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._synth_pool, self._synth, "ok")

    async def prepare(self, sentences: list[str]) -> None:
        loop = asyncio.get_running_loop()
        for sentence in sentences:
            if sentence not in self._ready:
                audio = await loop.run_in_executor(self._synth_pool, self._synth, sentence)
                self._ready[sentence] = audio

    async def prepare_wait_phrases(self) -> None:
        from backend.audio.waiting import PHRASES, WaitingPhrases

        self.waiting_phrases = WaitingPhrases()
        loop = asyncio.get_running_loop()
        for phrase in PHRASES:
            if phrase not in self.wait_audio:
                audio = await loop.run_in_executor(self._synth_pool, self._synth, phrase)
                self.wait_audio[phrase] = audio
                self.wait_durations[phrase] = len(audio) / self.sample_rate

    async def speak(self, sentences: AsyncIterator[str], on_start: OnStart | None = None) -> bool:
        # The cancelled owner finishes its player before a successor can start.
        async with self._playback_lock:
            return await self._speak(sentences, on_start)

    async def _speak(self, sentences: AsyncIterator[str], on_start: OnStart | None) -> bool:
        loop = asyncio.get_running_loop()
        stop = threading.Event()
        self._stop = stop
        self.audio_enqueued_at = None
        self.playback_started_at = None
        audio_q: queue.Queue = queue.Queue()
        audio_q.stop = stop
        callbacks = copy_context()
        callbacks_active = True

        def dispatch(callback, *args):
            if callbacks_active and self._stop is stop and not stop.is_set():
                callback(*args)

        notify = (
            (lambda: loop.call_soon_threadsafe(dispatch, on_start, context=callbacks))
            if on_start
            else None
        )
        report = None
        if (on_level := self.on_level) is not None:
            report = lambda v: loop.call_soon_threadsafe(dispatch, on_level, v, context=callbacks)  # noqa: E731
        said = None
        if (on_sentence := self.on_sentence) is not None:

            def said(text, s):
                loop.call_soon_threadsafe(dispatch, on_sentence, text, s, context=callbacks)

        with measure("tts_model_ready"):
            await loop.run_in_executor(self._synth_pool, self._load)
        self._speaking = True
        player = loop.run_in_executor(None, self._play, audio_q, notify, report, said)
        try:
            # Synthesize sentence N+1 while sentence N is playing.
            async for sentence in sentences:
                if stop.is_set():
                    break
                audio = self.wait_audio.get(sentence)
                if audio is None:
                    audio = self._ready.pop(sentence, None)
                if audio is None:
                    with measure("tts_generation"):
                        audio = await loop.run_in_executor(self._synth_pool, self._synth, sentence)
                if self.audio_enqueued_at is None:
                    self.audio_enqueued_at = time.perf_counter()
                audio_q.put((sentence, audio))
        except BaseException:
            stop.set()
            raise
        finally:
            audio_q.put(None)
            try:
                try:
                    completed = await asyncio.shield(player)
                except asyncio.CancelledError:
                    stop.set()
                    await asyncio.shield(player)
                    raise
            finally:
                callbacks_active = False
                self._speaking = False
        return completed and not stop.is_set()

    def interrupt(self) -> None:
        self._stop.set()


class SileroSpeaker(PiperSpeaker):
    """Russian neural voices using the existing queue, player, interruption and AEC.

    Only the synthesizer differs from Piper; audio stays entirely on this Mac.
    """

    def __init__(
        self,
        voice_path: Path,
        speaker: str = "eugene",
        device: str | int | None = None,
        pronunciations: dict[str, str] | None = None,
    ) -> None:
        super().__init__(voice_path, device=device, pronunciations=pronunciations)
        self.speaker = speaker

    @property
    def sample_rate(self) -> int:
        return 24000

    def _load(self) -> None:
        if self._voice is None:
            import torch

            # The trusted official Silero package is downloaded by setup, never at runtime.
            torch.set_num_threads(2)
            self._voice = torch.package.PackageImporter(str(self.voice_path)).load_pickle(
                "tts_models", "model"
            )
            self._voice.to(torch.device("cpu"))

    def _synth(self, text: str) -> np.ndarray:
        import torch

        self._load()
        spoken = speech_text(text, self.pronunciations, "ru")
        # Silero marks stress before the vowel with '+', Piper uses a combining acute.
        spoken = re.sub(r"([аеёиоуыэюя])\u0301", r"+\1", spoken, flags=re.IGNORECASE)
        with torch.inference_mode():
            audio = self._voice.apply_tts(
                text=spoken,
                speaker=self.speaker,
                sample_rate=self.sample_rate,
                put_accent=True,
                put_yo=True,
            )
        samples = audio.detach().cpu().numpy()
        return (np.clip(samples, -1, 1) * 32767).astype(np.int16)

    async def warmup(self) -> None:
        loop = asyncio.get_running_loop()
        from backend.audio.foreign import lexicon

        await loop.run_in_executor(self._synth_pool, lexicon)
        await loop.run_in_executor(self._synth_pool, self._synth, "Так.")


class SaySpeaker:
    """Fallback using macOS `say`: no downloads, but less natural and not streamed."""

    def __init__(
        self, voice: str | None = None, pronunciations: dict[str, str] | None = None
    ) -> None:
        self.voice = voice
        self.speech_language: str | None = None
        self.pronunciations = pronunciations or {}
        self._proc: asyncio.subprocess.Process | None = None
        self._stop = asyncio.Event()
        self._speaking = False
        self.on_sentence: OnSentence | None = None

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def interrupted(self) -> bool:
        return self._stop.is_set()

    async def warmup(self) -> None:
        return None

    async def prepare(self, sentences: list[str]) -> None:
        return None  # `say` cannot synthesize ahead

    async def speak(self, sentences: AsyncIterator[str], on_start: OnStart | None = None) -> bool:
        self._stop.clear()
        self._speaking = True
        voice_args = ["-v", self.voice] if self.voice else []
        try:
            async for sentence in sentences:
                if self._stop.is_set():
                    break
                if on_start:
                    on_start()
                    on_start = None
                if self.on_sentence:
                    self.on_sentence(sentence, len(sentence) * SAY_S_PER_CHAR)
                spoken = speech_text(sentence, self.pronunciations, self.speech_language)
                self._proc = await asyncio.create_subprocess_exec("say", *voice_args, spoken)
                await self._proc.wait()
        finally:
            self._proc = None
            self._speaking = False
        return not self._stop.is_set()

    def interrupt(self) -> None:
        self._stop.set()
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
