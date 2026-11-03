"""Local text-to-speech: Piper, Silero, or the macOS `say` command."""

from __future__ import annotations

import asyncio
import logging
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

from backend.latency import Profile, current, measure

OnStart = Callable[[], None]
OnLevel = Callable[[float], None]
OnAudio = Callable[[np.ndarray], None]
OnSentence = Callable[[str, float], None]  # text, seconds of speech; on the event loop

SAY_S_PER_CHAR = 0.065  # `say` gives no duration: about this long per character
log = logging.getLogger(__name__)


class SynthesisError(RuntimeError):
    """One or more fragments failed; valid fragments have finished playing."""


def speakable(text: str) -> bool:
    return any(char.isalnum() for char in text)


def synthesis_chunks(text: str, limit: int = 450) -> list[str]:
    """Bound the model input, retaining every word of long unpunctuated answers."""
    result = []
    while len(text) > limit:
        cut = text.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = text.find(" ", limit)
            if cut < 0:
                break  # A single long word/name must remain intact.
        result.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        result.append(text)
    return result


def apply_pronunciations(text: str, pronunciations: dict[str, str]) -> str:
    """Keep display text intact while adding explicit pronunciation to the spoken text."""
    if not pronunciations:
        return text
    names = "|".join(re.escape(name) for name in sorted(pronunciations, key=len, reverse=True))
    lookup = {name.casefold(): spoken for name, spoken in pronunciations.items()}
    return re.sub(
        rf"\b(?:{names})\b", lambda m: lookup[m.group().casefold()], text, flags=re.IGNORECASE
    )


def speech_text(
    text: str,
    pronunciations: dict[str, str],
    language: str | None,
    *,
    normalize_numbers=True,
    normalize_english=True,
) -> str:
    if language == "ru":
        from backend.audio.russian import normalize
        from backend.audio.sentences import clean_for_speech

        return clean_for_speech(
            normalize(text, pronunciations, numbers=normalize_numbers, foreign=normalize_english),
            "ru",
        )
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
        self.volume, self.pause_ms = 1.0, 0
        self.rate = self.pitch = "medium"
        self.normalize_numbers = self.normalize_english = True
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
        with measure("tts_text_preparation"):
            spoken = speech_text(
                text,
                self.pronunciations,
                self.speech_language,
                normalize_numbers=self.normalize_numbers,
                normalize_english=self.normalize_english,
            )
        chunks = [c.audio_int16_array for c in self._voice.synthesize(spoken, syn_config=cfg)]
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)

    @property
    def sample_rate(self) -> int:
        return self._voice.config.sample_rate

    async def _synth_measured(self, text: str) -> np.ndarray:
        """Collect worker timings locally, publish them only on the event loop."""
        parent = current.get()
        worker = Profile(origin=parent.origin) if parent else None

        def synth():
            token = current.set(worker)
            try:
                return self._synth(text)
            finally:
                current.reset(token)

        try:
            return await asyncio.get_running_loop().run_in_executor(self._synth_pool, synth)
        finally:
            if parent and worker:
                for row in list(worker.stages):
                    if row["end_s"] is not None:
                        parent.interval(
                            row["stage"],
                            worker.origin + row["start_s"],
                            worker.origin + row["end_s"],
                            row["outcome"],
                        )

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
        previous_end = None
        on_gap = getattr(audio_q, "on_gap", None)
        with sd.OutputStream(samplerate=rate, channels=1, dtype="int16", device=self.device) as out:
            while (item := audio_q.get()) is not None:
                sentence, audio = item
                if (
                    previous_end is not None
                    and on_gap
                    and time.perf_counter() - previous_end > 0.25
                ):
                    on_gap(previous_end, time.perf_counter())
                self.recent_spoken.append(sentence)
                self.recent_spoken[:] = self.recent_spoken[-8:]
                sentence_started = False
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
                    out.write(audio[i : i + block])
                    # Only successful writes count as audio. A queued callback before
                    # the first write could report sound after a device failure/cancel.
                    if not sentence_started:
                        if on_sentence:
                            on_sentence(sentence, len(audio) / rate)
                        sentence_started = True
                    if not started:
                        self.playback_started_at = time.perf_counter()
                        if on_start:
                            on_start()
                        started = True
                previous_end = time.perf_counter()
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
        with measure("tts_queue"):
            await self._playback_lock.acquire()
        try:
            return await self._speak(sentences, on_start)
        finally:
            self._playback_lock.release()

    async def _speak(self, sentences: AsyncIterator[str], on_start: OnStart | None) -> bool:
        loop = asyncio.get_running_loop()
        stop = threading.Event()
        self._stop = stop
        self.audio_enqueued_at = None
        self.playback_started_at = None
        self.first_sentence_at = None
        audio_q: queue.Queue = queue.Queue()
        audio_q.stop = stop
        callbacks = copy_context()
        callbacks_active = True
        profile = current.get()
        if profile:
            audio_q.on_gap = lambda start, end: loop.call_soon_threadsafe(
                profile.interval, "playback_gap", start, end
            )
        playback_interval = None

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
        on_sentence = self.on_sentence

        def sentence_started(text, seconds, at):
            nonlocal playback_interval
            from backend.audio.waiting import is_waiting_only

            if profile:
                waiting = is_waiting_only(text)
                profile.mark("first_audio", at=at)
                profile.mark("waiting_audio" if waiting else "first_content_audio", at=at)
                if playback_interval is None:
                    playback_interval = profile.begin(
                        "waiting_playback" if waiting else "tts_playback", at=at
                    )
            if on_sentence:
                on_sentence(text, seconds)

        def said(text, seconds):
            loop.call_soon_threadsafe(
                dispatch, sentence_started, text, seconds, time.perf_counter(), context=callbacks
            )

        with measure("tts_model_ready"):
            await loop.run_in_executor(self._synth_pool, self._load)
        self._speaking = True
        failures = 0
        fragment = 0
        log.info("TTS_START")
        player = loop.run_in_executor(None, self._play, audio_q, notify, report, said)
        try:
            # Synthesize sentence N+1 while sentence N is playing.
            async for sentence in sentences:
                fragment += 1
                if not speakable(sentence):
                    log.info("TTS_FRAGMENT_SKIP index=%d reason=punctuation_only", fragment)
                    continue
                # Keep two sentences ahead without synthesizing an entire long
                # answer while playback is still on sentence one. Never block
                # the event loop on the thread queue, including on cancellation.
                while audio_q.qsize() >= 2 and not stop.is_set() and not player.done():
                    await asyncio.sleep(0.01)
                if stop.is_set():
                    break
                if player.done():
                    if not player.result():
                        stop.set()
                    break
                if self.first_sentence_at is None:
                    self.first_sentence_at = time.perf_counter()
                    if profile:
                        from backend.audio.waiting import is_waiting_only

                        profile.mark(
                            "waiting_sentence_ready"
                            if is_waiting_only(sentence)
                            else "first_sentence_ready"
                        )
                audio = self.wait_audio.get(sentence)
                if audio is None:
                    audio = self._ready.pop(sentence, None)
                if audio is None:
                    with measure("tts_generation"):
                        try:
                            audio = await self._synth_measured(sentence)
                        except Exception as exc:
                            failures += 1
                            log.error(
                                "TTS_FRAGMENT_ERROR index=%d type=%s", fragment, type(exc).__name__
                            )
                            if profile:
                                profile.fail("tts_fragment_error")
                                profile.update(
                                    error_component="tts_generation", fragment_index=fragment
                                )
                            if callback := getattr(self, "on_error", None):
                                callback("Фрагмент не озвучен. Продолжаю остальную озвучку.")
                            continue
                if not len(audio):
                    log.info("TTS_FRAGMENT_SKIP index=%d reason=no_spoken_text", fragment)
                    continue
                if stop.is_set():
                    break
                if self.audio_enqueued_at is None:
                    self.audio_enqueued_at = time.perf_counter()
                    if profile:
                        from backend.audio.waiting import is_waiting_only

                        profile.mark(
                            "waiting_audio_enqueued"
                            if is_waiting_only(sentence)
                            else "tts_audio_enqueued"
                        )
                if self.volume != 1.0:
                    audio = np.clip(audio.astype(np.float32) * self.volume, -32768, 32767).astype(
                        np.int16
                    )
                if self.pause_ms:
                    audio = np.concatenate(
                        (
                            audio,
                            np.zeros(
                                round(self.sample_rate * self.pause_ms / 1000), dtype=np.int16
                            ),
                        )
                    )
                audio_q.put((sentence, audio))
        except asyncio.CancelledError:
            log.info("TTS_CANCEL reason=task_cancelled")
            stop.set()
            raise
        except Exception:
            # Drain valid queued audio before surfacing a producer failure.
            log.error("TTS_PRODUCER_ERROR")
            raise
        except BaseException:
            stop.set()
            raise
        finally:
            audio_q.put(None)
            player_ok = False
            try:
                try:
                    completed = await asyncio.shield(player)
                    player_ok = bool(completed)
                except asyncio.CancelledError:
                    stop.set()
                    await asyncio.shield(player)
                    raise
            finally:
                if profile:
                    profile.end(
                        playback_interval,
                        "cancelled" if stop.is_set() else "ok" if player_ok else "error",
                    )
                callbacks_active = False
                self._speaking = False
                log.info(
                    "TTS_END completed=%s interrupted=%s failed_fragments=%d",
                    player_ok,
                    stop.is_set(),
                    failures,
                )
        if failures:
            raise SynthesisError("tts_fragment_error")
        return completed and not stop.is_set()

    def interrupt(self) -> None:
        log.info("TTS_CANCEL reason=explicit_interrupt")
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
        with measure("tts_text_preparation"):
            spoken = speech_text(
                text,
                self.pronunciations,
                "ru",
                normalize_numbers=self.normalize_numbers,
                normalize_english=self.normalize_english,
            )
            if not speakable(spoken):
                return np.zeros(0, dtype=np.int16)
            # Silero marks stress before the vowel with '+', Piper uses a combining acute.
            spoken = re.sub(r"([аеёиоуыэюя])\u0301", r"+\1", spoken, flags=re.IGNORECASE)
        chunks = []
        with torch.inference_mode():
            for part in synthesis_chunks(spoken):
                if self.rate != "medium" or self.pitch != "medium":
                    from html import escape

                    source = {
                        "ssml_text": (
                            f'<speak><prosody rate="{self.rate}" pitch="{self.pitch}">'
                            f"{escape(part)}</prosody></speak>"
                        )
                    }
                else:
                    source = {"text": part}
                audio = self._voice.apply_tts(
                    **source,
                    speaker=self.speaker,
                    sample_rate=self.sample_rate,
                    put_accent=True,
                    put_yo=True,
                )
                samples = audio.detach().cpu().numpy()
                chunks.append((np.clip(samples, -1, 1) * 32767).astype(np.int16))
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)

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
