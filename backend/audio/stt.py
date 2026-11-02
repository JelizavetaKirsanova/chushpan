"""Speech-to-text with Whisper on Apple Silicon (mlx-whisper)."""

from __future__ import annotations

import asyncio
import gc
import logging
import math
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import numpy as np

log = logging.getLogger(__name__)

# Phrases Whisper tends to invent on silence or noise (learned from video subtitles).
_HALLUCINATIONS = {
    "obrigado",
    "obrigada",
    "tchau",
    "legendas pela comunidade amaraorg",
    "legenda adriana zanotto",
    "inscrevase no canal",
    "thank you",
    "thanks for watching",
    "thank you for watching",
    "you",
    "bye",
    "subtitles by the amaraorg community",
    "спасибо за просмотр",
    "благодарю за просмотр",
    "подписывайтесь на канал",
    "субтитры создал dimatorzok",
    "субтитры сделал dimatorzok",
    "продолжение следует",
}


# Hallucinations that are also real farewells: right after a reply, take them at face value.
_FAREWELLS = {"obrigado", "obrigada", "tchau", "thank you", "bye", "спасибо", "пока", "до свидания"}


def _normalize(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


def _assistant_names(name: str) -> set[str]:
    names = {_normalize(name)}
    if "jarvis" in names:
        names.add("джарвис")
    return names


def is_hallucination(text: str) -> bool:
    norm = _normalize(text)
    # Pure hesitation/noise is not a request. Keep words following it intact, including
    # short agreements: these still need the existing dialogue/confirmation checks.
    filler = re.fullmatch(r"(?:х?м{2,}|ум+|у{2,}|а{3,}|э{2,})", norm.replace(" ", ""))
    return not norm or norm in _HALLUCINATIONS or bool(filler)


def is_farewell(text: str) -> bool:
    return _normalize(text) in _FAREWELLS


# The whole utterance must be a sign-off: "thanks, you can rest" yes, "you can rest after
# telling me the weather" no. qwen3 often answered these without calling go_to_sleep.
_SIGN_OFF_FILLER = (
    r"(?:ok|okay|certo|ta bom|tá bom|está bom|esta bom|tudo bem|tudo certo|beleza|perfeito"
    r"|ótimo|otimo|então|entao|muito|obrigad[oa]|valeu|thanks|thank you|great"
    r"|ладно|хорошо|отлично|всё|все|большое|спасибо|благодарю|тебе|вам|вас|{name})"
)
_SIGN_OFF = (
    r"pode (?:ir )?(?:descansar|dormir)|vai descansar|vá descansar|pode parar de ouvir"
    r"|(?:é|e|era) (?:só|so) isso|por enquanto (?:é|e) (?:só|so)|(?:é|e) tudo"
    r"|até (?:mais|logo|amanhã|amanha|depois)|tchau"
    r"|that'?s all|that is all|go to sleep|you can (?:rest|sleep)|goodbye|bye"
    r"|можешь (?:идти )?(?:отдыхать|спать)|можете (?:идти )?(?:отдыхать|спать)"
    r"|отдыхай|отдыхайте|спи|перестань слушать|перестаньте слушать"
    r"|(?:это )?вс[её]|на этом вс[её]|пока|до (?:свидания|встречи|завтра)"
)


def is_sign_off(text: str, name: str = "Jarvis") -> bool:
    """True when the user is only ending the conversation ("Obrigada, pode descansar.")."""
    names = "|".join(re.escape(n) for n in _assistant_names(name))
    filler = _SIGN_OFF_FILLER.format(name=names)
    tail = r"(?:por (?:enquanto|hoje|agora)|for now|for today|на сегодня|на сейчас)"
    pattern = rf"(?:{filler}\s+)*(?:{_SIGN_OFF})(?:\s+(?:{filler}|{tail}))*"
    return re.fullmatch(pattern, re.sub(r"\s+", " ", _normalize(text))) is not None


_THANKS = re.compile(
    r"\b(?:obrigad[oa]|valeu|agradeço|agradeco|thanks|thank you|cheers|спасибо|благодарю)\b"
)


def only_thanks(text: str, name: str = "Jarvis") -> bool:
    """True when the whole utterance is a thank-you ("Muito obrigada, Jarvis.")."""
    names = "|".join(re.escape(n) for n in _assistant_names(name))
    words = rf"(?:{_SIGN_OFF_FILLER.format(name=names)}|agradeço|agradeco|cheers|senhor|sir)"
    norm = re.sub(r"\s+", " ", _normalize(text))
    return thanks(text) and re.fullmatch(rf"{words}(?: {words})*", norm) is not None


def thanks(text: str) -> bool:
    """True when the person thanked ("Obrigada, pode descansar")."""
    return _THANKS.search(_normalize(text)) is not None


def echoes_hint(text: str, hint: str | None) -> bool:
    """True when Whisper just repeated (most of) its prompt instead of transcribing."""
    if not hint:
        return False
    t, h = _normalize(text), _normalize(hint)
    return t == h or (len(t) >= 0.6 * len(h) and t in h)


_WAKE_PREFIXES = (
    "",
    "hey",
    "hei",
    "ei",
    "ey",
    "oi",
    "olá",
    "ola",
    "ok",
    "okay",
    "hi",
    "hello",
    "эй",
    "хей",
    "привет",
    "слушай",
    "окей",
)


def is_wake_phrase(text: str, name: str = "Jarvis") -> bool:
    """True when the user only called the assistant ("Hey Jarvis.") without a request."""
    norm = _normalize(text)
    names = _assistant_names(name)
    return any(norm == f"{p} {n}".strip() for p in _WAKE_PREFIXES for n in names)


def strip_wake_phrase(text: str, phrase: str) -> str | None:
    """Match a whole phrase at the start, returning a request said in the same breath.

    Accents, punctuation, case and ё/е do not affect the match; a name merely mentioned
    later in a sentence never activates the assistant. None means no match, "" means
    the wake phrase alone.
    """

    def unaccent(value: str) -> str:
        return unicodedata.normalize(
            "NFC", unicodedata.normalize("NFD", value).replace("\u0301", "")
        )

    clean = unaccent(text)
    expected = re.findall(r"\w+", unaccent(phrase).casefold().replace("ё", "е"))
    words = list(re.finditer(r"\w+", clean))
    if not expected or len(words) < len(expected):
        return None
    if [m.group().casefold().replace("ё", "е") for m in words[: len(expected)]] != expected:
        return None
    return clean[words[len(expected) - 1].end() :].lstrip(" \t\n,.!?;:—–-«»\"'")


def is_morning_greeting(text: str, name: str, *, require_name: bool = False) -> bool:
    """An explicit greeting command, never a mention in unrelated speech."""
    norm = " ".join(_normalize(text).split())
    names = "|".join(re.escape(n) for n in _assistant_names(name))
    named = rf"(?:(?:эй )?(?:{names}) доброе утро|доброе утро (?:{names}))"
    pattern = named if require_name else rf"(?:{named}|доброе утро)"
    return re.fullmatch(pattern, norm) is not None


class WhisperSTT:
    def __init__(
        self, model: str, language: str, hint: str | None = None, cache_mb: int | None = None
    ) -> None:
        self.model = model
        self.language = language
        self.hint = hint
        self.cache_mb = cache_mb  # cap on the GPU buffers MLX keeps for reuse; None = no cap
        self._capped = False
        self._path: str | None = None
        self.last_raw = ""  # last transcript before filtering, for diagnostics
        self.last_speech_confident = False
        self.last_confidence: dict = {}
        # MLX state is not thread-safe: run every call on the same worker thread.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
        self._pending: asyncio.Future | None = None

    def _model_path(self) -> str:
        """Use the local copy when present, so startup needs no network at all."""
        if self._path is None:
            from huggingface_hub import snapshot_download

            try:
                self._path = snapshot_download(self.model, local_files_only=True)
            except Exception:  # noqa: BLE001
                self._path = snapshot_download(self.model)
        return self._path

    def _transcribe_sync(self, audio: np.ndarray) -> str:
        import mlx_whisper

        if self.cache_mb is not None and not self._capped:
            import mlx.core as mx

            mx.set_cache_limit(self.cache_mb * 1024**2)
            self._capped = True

        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self._model_path(),
            language=self.language,
            task="transcribe",
            temperature=0.0,
            condition_on_previous_text=False,
            initial_prompt=self.hint,
            verbose=None,
        )
        # Use the decoder's acoustic confidence as well as VAD. These are Whisper's
        # standard silence/failure thresholds; neither guesses a replacement transcript.
        segments = result.get("segments", [])
        metrics = [
            {
                key: segment.get(key)
                for key in ("no_speech_prob", "avg_logprob", "compression_ratio")
            }
            for segment in segments
        ]
        if metrics and all(
            type(value) in (int, float) and math.isfinite(value)
            for segment in metrics
            for value in segment.values()
        ):
            self.last_confidence = {
                "max_no_speech_prob": max(s["no_speech_prob"] for s in metrics),
                "min_avg_logprob": min(s["avg_logprob"] for s in metrics),
                "max_compression_ratio": max(s["compression_ratio"] for s in metrics),
            }
        else:
            self.last_confidence = {}
        self.last_speech_confident = bool(segments) and all(
            segment.get("no_speech_prob", 1) <= 0.6 and segment.get("avg_logprob", -100) >= -1.0
            for segment in segments
        )
        accepted = [
            segment
            for segment in segments
            if not (segment.get("no_speech_prob", 0) > 0.6 and segment.get("avg_logprob", 0) < -1.0)
        ]
        if segments and not accepted:
            log.info("STT_REJECT reason=decoder_no_speech")
            return ""
        if segments and len(accepted) != len(segments):
            return "".join(segment.get("text", "") for segment in accepted).strip()
        return str(result.get("text", "")).strip()

    async def _run(self, audio: np.ndarray) -> str:
        loop = asyncio.get_running_loop()
        if self._pending is not None and not self._pending.done():
            raise RuntimeError("Предыдущая локальная STT-операция ещё выполняется.")
        if self._pending is not None and not self._pending.cancelled():
            self._pending.exception()
        self._pending = loop.run_in_executor(self._executor, self._transcribe_sync, audio)
        return await asyncio.shield(self._pending)

    async def warmup(self) -> None:
        await self._run(np.zeros(16_000, dtype=np.float32))

    def _unload_sync(self) -> None:
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder

        ModelHolder.model = None
        ModelHolder.model_path = None
        gc.collect()
        mx.clear_cache()

    async def unload(self) -> None:
        """Free the model (~500 MB); the next transcription loads it again (~0.3 s)."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._unload_sync)

    async def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe 16 kHz int16 audio; returns "" for silence or known hallucinations."""
        from backend.audio.evidence import evidence

        self.last_speech_confident = False
        self.last_confidence = {}

        if not evidence(audio).usable:
            self.last_raw = ""
            log.info("STT_REJECT reason=no_acoustic_speech")
            return ""
        text = await self._run(audio.astype(np.float32) / 32768.0)
        self.last_raw = text
        if is_hallucination(text) or echoes_hint(text, self.hint):
            return ""
        return text
