"""Voice conversation loop: `uv run python -m backend voice`."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import itertools
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar

import numpy as np
from rich.console import Console

from backend.addressee import SpeechSignals, TaskSnapshot, choose_task, decide, discussing
from backend.audio import earcon
from backend.audio.evidence import evidence
from backend.audio.level import level
from backend.audio.sentences import SentenceSplitter, clean_for_speech
from backend.audio.stt import (
    is_morning_greeting,
    is_sign_off,
    is_wake_phrase,
    only_thanks,
    strip_wake_phrase,
    thanks,
)
from backend.audio.tts import PiperSpeaker, SaySpeaker, SileroSpeaker, Speaker
from backend.audio.wakeword import WakeWordDetector
from backend.brain.conversation import Conversation
from backend.brain.llm import LLMError
from backend.brain.providers import configured_label, create_llm
from backend.config import Settings
from backend.context import DeviceContext, voice_addressed, voice_music_command
from backend.dialogue import CLOSE, CONTROLS, follow_up, normalize, private_headset
from backend.hud import HudBus
from backend.latency import Profile, current
from backend.memory.store import MemoryStore
from backend.night import night_addressed, night_requested
from backend.resources import Resources
from backend.state import State, StateMachine, request_context
from backend.tools import Tool, build_registry
from backend.tools.geocode import resolve_location
from backend.tools.media import MediaService
from backend.tools.timers import TimerManager

console = Console(highlight=False)
log = logging.getLogger(__name__)
phase_sink: ContextVar[Callable[[str], None] | None] = ContextVar("voice_phase", default=None)
_standalone_requests = itertools.count(-1, -1)


def emit_phase(name: str) -> None:
    if sink := phase_sink.get():
        sink(name)


def echo(text: str) -> None:
    """Print streamed reply text. soft_wrap: rich would wrap each chunk as if it started a
    line, turning a space into a newline mid-sentence ("São Bernardo do\nCampo")."""
    console.print(text, end="", markup=False, soft_wrap=True)


FOOTPRINT_SETTLE_S = 2  # after an unload or reload, before measuring it for the HUD
ECHO_TAIL_S = 0.4  # ignore the mic briefly after speaking so the room echo dies down
STT_TIMEOUT_S = 20
TURN_TIMEOUT_S = 180


def write_as_spoken(bus: HudBus) -> Callable[[str, float], None]:
    """A speaker's `on_sentence`: the HUD writes each sentence while it is heard, not as
    the model generates it (much faster than speech) (D-42)."""

    def on_sentence(text: str, seconds: float) -> None:
        bus.publish({"type": "reply", "delta": f"{text} ", "duration_s": round(seconds, 2)})

    return on_sentence


def end_reply(bus: HudBus, speaker: Speaker) -> None:
    """Interrupted, the HUD stops writing where the voice stopped; else it writes the rest."""
    bus.publish({"type": "reply_end", "cut": bool(getattr(speaker, "interrupted", False))})


def make_speaker(s: Settings) -> Speaker:
    if s.tts.engine == "say":
        speaker = SaySpeaker(s.tts.say_voice, s.tts.pronunciations)
    elif s.tts.engine == "silero":
        speaker = SileroSpeaker(
            s.voice_path,
            speaker=s.tts.silero_speaker,
            device=s.audio.output_device,
            pronunciations=s.tts.pronunciations,
        )
    else:
        speaker = PiperSpeaker(
            s.voice_path, s.audio.output_device, s.tts.length_scale, s.tts.pronunciations
        )
    speaker.speech_language = s.locale.lang
    return speaker


def speech_sentences(text: str, min_chars: int, lang: str) -> list[str]:
    """The sentences `speak_tokens` makes of `text` arriving at once, to synthesize ahead."""
    splitter = SentenceSplitter(min_chars, protect_literals=lang == "ru")
    pieces = [*splitter.feed(text), splitter.flush()]
    return [p if lang == "ru" else clean_for_speech(p, lang) for p in pieces if p]


async def _once(text: str) -> AsyncIterator[str]:
    yield text


async def speak_tokens(
    speaker: Speaker,
    tokens: AsyncIterator[str],
    min_chars: int,
    lang: str,
    on_token: Callable[[str], None] | None = None,
    on_start: Callable[[], None] | None = None,
) -> bool:
    """Stream generated text into the speaker sentence by sentence."""
    splitter = SentenceSplitter(min_chars, protect_literals=lang == "ru")

    async def sentences() -> AsyncIterator[str]:
        async for token in tokens:
            if on_token:
                on_token(token)
            for sentence in splitter.feed(token):
                if spoken := (sentence if lang == "ru" else clean_for_speech(sentence, lang)):
                    yield spoken
        if (rest := splitter.flush()) and (
            spoken := (rest if lang == "ru" else clean_for_speech(rest, lang))
        ):
            yield spoken

    return await speaker.speak(sentences(), on_start=on_start)


async def reply_aloud(
    conv: Conversation,
    speaker: Speaker,
    user_text: str,
    min_chars: int,
    on_token: Callable[[str], None] | None = None,
    on_start: Callable[[], None] | None = None,
) -> bool:
    # Own the callback and both kinds of playback until cancellation cleanup completes.
    lock = getattr(speaker, "_reply_lock", None)
    if lock is None:
        lock = speaker._reply_lock = asyncio.Lock()
    async with lock:
        from backend.audio.waiting import trace

        token = None
        if request_context.get() is None:
            token = request_context.set(next(_standalone_requests))
            trace("REQUEST_RECEIVED", source="reply")
        try:
            if not user_text.strip():
                trace("REQUEST_REJECTED", reason="empty_text")
                return False
            return await _reply_aloud(conv, speaker, user_text, min_chars, on_token, on_start)
        finally:
            if token is not None:
                trace("REQUEST_COMPLETED")
                request_context.reset(token)


async def _reply_aloud(conv, speaker, user_text, min_chars, on_token, on_start) -> bool:
    """Stream the LLM reply into the speaker sentence by sentence."""
    completed = False
    try:
        if conv.s.locale.lang == "ru":
            completed = await _russian_reply_aloud(
                conv, speaker, user_text, min_chars, on_token, on_start
            )
        else:
            completed = await speak_tokens(
                speaker, conv.reply(user_text), min_chars, conv.s.locale.lang, on_token, on_start
            )
    finally:
        # Synthesis can run several sentences ahead. Playback, rather than generation,
        # determines when the morning music may begin.
        if finish := getattr(conv, "finish_reply", None):
            error = await finish(completed)
            if error:
                await speak_tokens(
                    speaker, _once(error), min_chars, conv.s.locale.lang, on_token, on_start
                )
    return completed


async def _russian_reply_aloud(conv, speaker, user_text, min_chars, on_token, on_start) -> bool:
    from backend.audio.waiting import allow_waiting, trace

    queue = asyncio.Queue()
    ready = asyncio.Event()
    end = object()
    owner = object()
    speaker._waiting_turn = owner
    active = True
    main_started = False
    timer_open = False

    def close_timer(reason):
        nonlocal timer_open
        if timer_open:
            timer_open = False
            trace("WAIT_TIMER_CANCEL", reason=reason)

    async def produce():
        try:
            async with contextlib.aclosing(conv.reply(user_text)) as stream:
                async for piece in stream:
                    queue.put_nowait(piece)
                    if piece.strip():
                        ready.set()
        finally:
            ready.set()
            queue.put_nowait(end)

    async def tokens():
        while (piece := await queue.get()) is not end:
            yield piece
        await producer

    def live():
        return active and speaker._waiting_turn is owner

    async def play_waiting(phrase):
        # Recheck at execution time: the task may have been queued before answer-ready.
        if not live() or ready.is_set():
            trace("WAIT_AUDIO_SKIP", reason="stale_or_answer_ready")
            return
        emit_phase("waiting")
        trace("WAIT_AUDIO_START", cached=True)
        await speaker.speak(_once(phrase))

    def main_start():
        nonlocal main_started
        if not live() or main_started:
            return
        main_started = True
        close_timer("main_audio_started")
        trace("MAIN_AUDIO_START")
        if on_start:
            on_start()

    producer = asyncio.create_task(produce())
    waiting = None
    saved_callback = getattr(speaker, "on_sentence", None)
    try:
        if allow_waiting(user_text):
            timer_open = True
            trace("WAIT_TIMER_START", delay_s=conv.s.tools.one_moment_after_s)
            try:
                await asyncio.wait_for(ready.wait(), conv.s.tools.one_moment_after_s)
                close_timer("answer_ready")
                trace("WAIT_AUDIO_SKIP", reason="answer_ready")
            except TimeoutError:
                close_timer("elapsed")
                picker = getattr(speaker, "waiting_phrases", None)
                durations = getattr(speaker, "wait_durations", {})
                if (
                    live()
                    and not ready.is_set()
                    and picker
                    and (phrase := picker.choose(durations))
                ):
                    speaker.on_sentence = None
                    waiting = asyncio.create_task(play_waiting(phrase))
                else:
                    trace("WAIT_AUDIO_SKIP", reason="ready_or_uncached")
        else:
            trace("WAIT_TIMER_SKIP", reason="local_social_or_empty")
        await ready.wait()
        close_timer("answer_ready")
        if waiting is not None:
            if not waiting.done():
                trace("WAIT_AUDIO_CANCEL", reason="answer_ready")
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        speaker.on_sentence = saved_callback
        emit_phase("processing")
        completed = False
        try:
            completed = await speak_tokens(speaker, tokens(), min_chars, "ru", on_token, main_start)
            return completed
        finally:
            trace("MAIN_AUDIO_END", completed=completed, started=main_started)
    finally:
        active = False
        close_timer("request_ended")
        if waiting is not None and not waiting.done():
            trace("WAIT_AUDIO_CANCEL", reason="request_ended")
            waiting.cancel()
        producer.cancel()
        await asyncio.gather(producer, *([waiting] if waiting else []), return_exceptions=True)
        if speaker._waiting_turn is owner:
            speaker._waiting_turn = None
        speaker.on_sentence = saved_callback


async def speak_briefing(
    s: Settings,
    speaker: Speaker,
    tokens: AsyncIterator[str],
    started_at: float,
    timings: dict[str, float],
    bus: HudBus | None = None,
) -> None:
    """Speak the activation briefing, then print when each part of the startup was ready."""
    bus = bus or HudBus()
    first_audio: float | None = None

    def on_start() -> None:
        nonlocal first_audio
        if first_audio is None:
            first_audio = time.perf_counter()
            bus.publish({"type": "state", "state": State.SPEAKING.value})

    console.print(f"\n[bold cyan]{s.assistant_name} ›[/] ", end="")
    try:
        await speak_tokens(
            speaker,
            tokens,
            s.tts.sentence_min_chars,
            s.locale.lang,
            on_token=echo,
            on_start=on_start,
        )
    finally:
        end_reply(bus, speaker)
    details = [f"{name} {at:.2f}s" for name, at in timings.items()]
    if first_audio is not None:
        details.insert(0, f"голос через {first_audio - started_at:.2f} с после запуска")
    console.print(f"\n[dim]сводка · {' · '.join(details)}[/]")


def sleep_tool(request_sleep: Callable[[], None]) -> Tool:
    async def go_to_sleep() -> dict:
        request_sleep()
        return {"ok": True, "note": "reply with a brief farewell; you sleep after speaking"}

    return Tool(
        name="go_to_sleep",
        description=(
            "Stop listening until the wake word is said again. Call whenever the user ends "
            "the conversation: says goodbye or thanks you with nothing more to ask ('that's "
            "all', 'é só isso, obrigada', 'por enquanto é só'), or asks you to rest or stand by."
        ),
        handler=go_to_sleep,
        capability={
            "ru": "переходить в режим ожидания после завершения разговора",
            "pt": "voltar a dormir quando a conversa terminar",
            "en": "go back to sleep when the conversation is over",
        },
    )


def autostart_switch(s: Settings, bus: HudBus) -> None:
    """The HUD's "launch at login" switch: show its state and flip it on request."""
    import sys

    from backend import autostart

    if sys.platform != "darwin":
        return
    bus.publish({"type": "autostart", "enabled": autostart.enabled()})
    tasks: set[asyncio.Task] = set()

    async def flip(on: bool) -> None:
        try:
            now = await asyncio.to_thread(autostart.set_enabled, s, on)
            bus.publish({"type": "autostart", "enabled": now})
            console.print(
                f"\n[dim]Автозапуск {'включён' if now else 'выключен'} (из интерфейса)[/]"
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Launch at login not changed")
            bus.publish({"type": "autostart", "enabled": autostart.enabled(), "error": str(exc)})

    def on_request(value) -> None:
        task = asyncio.create_task(flip(bool(value)))
        tasks.add(task)  # a reference, or the task may be collected mid-way
        task.add_done_callback(tasks.discard)

    bus.handle("autostart", on_request)


def export_button(s: Settings, bus: HudBus, store: MemoryStore, conv: Conversation) -> None:
    """The HUD's "export" button: the conversation as Markdown in Downloads, shown in Finder."""
    from backend.memory.export import export_conversation

    tasks: set[asyncio.Task] = set()

    async def export() -> None:
        try:
            path = await asyncio.to_thread(export_conversation, s, store, conv.id)
        except OSError as exc:
            log.exception("Conversation not exported")
            bus.publish({"type": "exported", "error": str(exc)})
            return
        console.print(f"\n[dim]Разговор сохранён: {path}[/]")
        bus.publish({"type": "exported", "path": str(path), "name": path.name})
        with contextlib.suppress(OSError):
            await asyncio.create_subprocess_exec("open", "-R", str(path))

    def on_request(_) -> None:
        task = asyncio.create_task(export())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    bus.handle("export", on_request)


def tools_detail(st) -> str | None:
    if not st.tools:
        return None
    return f"tools {', '.join(st.tools)} {st.tools_s:.2f}s"


class VoiceLoop:
    """Routes microphone frames by state: wake word while sleeping, VAD while listening."""

    def __init__(
        self,
        s: Settings,
        conv: Conversation,
        stt,
        speaker: Speaker,
        vad,
        segmenter,
        wake: WakeWordDetector | None = None,
        chime: Callable[[np.ndarray], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        bus: HudBus | None = None,
        wake_phrase: str | None = None,
    ):
        self.s = s
        self.conv = conv
        self.stt = stt
        self.speaker = speaker
        self.vad = vad
        self.segmenter = segmenter
        self.wake = wake
        self.wake_phrase = wake_phrase
        self.use_wake = wake is not None or bool(wake_phrase)
        self.chime = chime
        self.clock = clock
        self.state = StateMachine(State.SLEEPING if self.use_wake else State.LISTENING, clock)
        self.task: asyncio.Task | None = None
        self.wake_task: asyncio.Task | None = None
        self.mute_until = 0.0
        self.vad_wait_s = 0.0
        self.wake_detection_s = 0.0
        self.listen_deadline: float | None = None  # None = listen forever
        self._barged_in = False
        self._sleep_requested = False
        self.pending: list[str] = []  # announcements waiting for a quiet moment
        self.flush_audio: Callable[[], None] = lambda: None
        self.barge_task: asyncio.Task | None = None
        self.session_deadline: float | None = None
        self._session_timer: asyncio.TimerHandle | None = None
        self._session_accepted = -1
        self._session_recording = False
        self.echo_ready = False
        self.headset_verified = False
        self.last_interrupted = False
        self.last_accepted_topic = ""
        self._prompted_until = 0.0
        self.task_snapshots: dict[int, TaskSnapshot] = {}
        registry = getattr(conv, "tools", None)
        if registry is not None:
            registry.on_execution = self._observe_task
        self.bus = bus or HudBus()
        self.bus.on_command = self.command
        self.state.add_listener(self._publish_state)
        self._publish_state(None, self.state.state)
        self.bus.handle("dialogue_settings", self.configure_dialogue)
        self._publish_session()

    def _observe_task(self, execution_id, name, status, result) -> None:
        owner = request_context.get()
        if owner is None:
            return  # CLI/chat/background jobs don't become the current voice task.
        if status == "result":
            status = "unknown"
            if isinstance(result, dict):
                if result.get("status") == "confirmation_required":
                    status = "confirmation_required"
                elif result.get("error") or result.get("ok") is False:
                    status = "failed"
                elif result.get("ok") is True:
                    status = "completed"
        self.task_snapshots[execution_id] = TaskSnapshot(owner, name, status, self.clock())
        if status == "running":
            self.bus.publish({"type": "addressee", "state": "task_running", "request_id": owner})
        while len(self.task_snapshots) > self.s.llm.context_messages:
            del self.task_snapshots[next(iter(self.task_snapshots))]

    def _recent_tasks(self) -> list[TaskSnapshot]:
        return [
            task
            for task in self.task_snapshots.values()
            if task.status == "running" or self.clock() - task.updated_at < TURN_TIMEOUT_S
        ]

    def _recipient(self, text, audio, *, addressed=False, echo=False, active=None):
        registry = getattr(self.conv, "tools", None)
        device = getattr(self.conv, "device", None)
        action = (
            registry.confirmations.pending.get((device.device_id, device.session_id))
            if registry and device
            else None
        )
        pending = bool(action and action.expires > registry.confirmations.clock())
        topic = self.last_accepted_topic
        # A separately spoken wake word grants just the first request in its short window.
        prompted = not topic and self.clock() < self._prompted_until
        decision = decide(
            text,
            addressed=addressed,
            active=self.session_active if active is None else active,
            topic=topic,
            signals=SpeechSignals(
                evidence(audio).usable, getattr(self.stt, "last_speech_confident", False), echo
            ),
            task=choose_task(text, self._recent_tasks()),
            strictness=self.s.dialogue.addressee_strictness,
            confirmation_pending=pending,
            followup_pending=bool(
                self.conv
                and hasattr(self.conv, "pending_followup")
                and self.conv.pending_followup()
            ),
        )
        if (
            prompted
            and not decision.accepted
            and decision.reason in {"recipient_unclear", "strict_wake_required"}
            and follow_up(text)
            and getattr(self.stt, "last_speech_confident", False)
        ):
            decision = decide(
                text,
                addressed=True,
                active=self.session_active,
                topic=topic,
                signals=SpeechSignals(evidence(audio).usable, True, echo),
            )
        log.info(
            "ADDRESSEE request=%s confidence=%s reason=%s kind=%s",
            self.state.request_id,
            decision.confidence,
            decision.reason,
            decision.kind,
        )
        self.bus.publish(
            {
                "type": "addressee",
                "state": (
                    "direct"
                    if decision.accepted and decision.kind == "direct"
                    else "task"
                    if decision.accepted and decision.kind == "task"
                    else "continuation"
                    if decision.accepted
                    else "ambiguous"
                    if decision.confidence == "medium"
                    else "waiting"
                ),
                "request_id": self.state.request_id,
            }
        )
        return decision

    @property
    def session_active(self) -> bool:
        return bool(
            self.s.dialogue.enabled
            and self.session_deadline is not None
            and self.clock() < self.session_deadline
        )

    @property
    def nameless_ready(self) -> bool:
        return bool(
            self.session_active
            and self.s.dialogue.nameless_interruptions
            and self.s.audio.barge_in
            and self.echo_ready
            and self.headset_verified
            and self.s.dialogue.addressee_strictness == "contextual"
        )

    def _publish_session(self) -> None:
        self.bus.publish(
            {
                "type": "session",
                "active": self.session_active,
                "listening": self.session_active and self.state.state is State.LISTENING,
                "requires_wake": self.use_wake and not self.session_active,
                "remaining_s": max(0, (self.session_deadline or self.clock()) - self.clock()),
                "enabled": self.s.dialogue.enabled,
                "duration_s": self.s.dialogue.duration_s,
                "nameless_interruptions": self.s.dialogue.nameless_interruptions,
                "addressee_strictness": self.s.dialogue.addressee_strictness,
                "nameless_ready": self.nameless_ready,
                "interrupted": self.last_interrupted,
                "reason": "Без имени: только короткие реплики продолжения в гарнитуре с AEC."
                if self.nameless_ready
                else "Для перебивания нужно имя: необходимы активная сессия, включённый режим, "
                "AEC и подтверждённые микрофон и выход гарнитуры.",
            }
        )

    def _open_session(self) -> None:
        if not self.s.dialogue.enabled or not self.use_wake:
            return
        self.session_deadline = self.clock() + self.s.dialogue.duration_s
        self.listen_deadline = self.session_deadline
        if self._session_timer:
            self._session_timer.cancel()
        self._session_timer = asyncio.get_running_loop().call_later(
            self.s.dialogue.duration_s, self._session_timeout
        )
        log.info(
            "SESSION_ACTIVE request=%s duration_s=%s",
            self.state.request_id,
            self.s.dialogue.duration_s,
        )
        self._publish_session()

    def _session_timeout(self) -> None:
        if (
            self.session_deadline is None
            or self.session_active
            or self.busy
            or self.segmenter.in_speech
        ):
            return
        log.info("SESSION_CLOSED reason=idle_timeout")
        self._go_to_sleep()

    def _close_session(self) -> None:
        self.session_deadline = None
        if self._session_timer:
            self._session_timer.cancel()
            self._session_timer = None

    def configure_dialogue(self, value) -> None:
        from pydantic import ValidationError

        from backend.config import DialogueConfig

        try:
            if not isinstance(value, dict) or set(value) - {
                "enabled",
                "duration_s",
                "nameless_interruptions",
                "addressee_strictness",
            }:
                raise ValueError("invalid dialogue setting")
            config = DialogueConfig.model_validate(
                {**self.s.dialogue.model_dump(), **value}, strict=True
            )
        except (ValueError, ValidationError):
            self.bus.publish(
                {
                    "type": "notice",
                    "error": True,
                    "message": "Не удалось применить настройки диалога.",
                }
            )
            self._publish_session()
            self.bus.publish({"type": "dialogue_settings_result", "ok": False})
            return
        self.s.dialogue = config
        if not config.enabled:
            self._close_session()
            if not self.busy and self.use_wake:
                self._go_to_sleep()
        elif self.session_deadline is not None:
            self._open_session()
        self._publish_session()
        self.bus.publish({"type": "dialogue_settings_result", "ok": True})

    def _publish_state(self, _: State | None, new: State) -> None:
        self.bus.publish({"type": "state", "state": new.value, "request_id": self.state.request_id})
        self._publish_session()

    def _phase(self, name: str) -> None:
        owner = request_context.get() or self.state.request_id
        if self.state.set_phase(name, owner):
            self.bus.publish({"type": "activity", "phase": name, "request_id": owner})

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def _listen_for(self, seconds: float) -> None:
        self.listen_deadline = self.clock() + seconds if self.use_wake else None

    def _wake_up(self, score: float | None) -> None:
        log.info("WAKE_DETECTED")
        self.last_accepted_topic = ""
        self._prompted_until = self.clock() + self.s.wakeword.listen_timeout_s
        if self.wake_task and self.wake_task is not asyncio.current_task():
            self.wake_task.cancel()
        self.vad.reset()
        self.segmenter.reset()
        self.state.to(State.LISTENING)
        self._phase("listening")
        self.flush_audio()
        self._listen_for(self.s.wakeword.listen_timeout_s)
        self._open_session()
        if self.chime:
            self.chime(earcon.WAKE)
        why = self.wake_phrase or ("интерфейс" if score is None else f"активация {score:.2f}")
        console.print(f"\n[cyan]●[/] [dim]слушаю ({why})[/]")

    def _go_to_sleep(self) -> None:
        self._close_session()
        self.state.to(State.SLEEPING)
        self._phase("idle")
        self.listen_deadline = None
        self.flush_audio()
        self._publish_session()
        self.bus.publish(
            {"type": "addressee", "state": "waiting", "request_id": self.state.request_id}
        )
        if self.wake:
            self.wake.reset()
        self.vad.reset()
        self.segmenter.reset()
        self.mute_until = self.clock() + ECHO_TAIL_S
        if self.chime:
            self.chime(earcon.SLEEP)
        phrase = self.wake_phrase or f"Hey {self.s.assistant_name}"
        console.print(f"[dim]○ ожидание · скажите «{phrase}»[/]")

    def request_sleep(self) -> None:
        """Go to sleep once the current reply ends (the `go_to_sleep` tool)."""
        if self.use_wake:
            self._sleep_requested = True

    def command(self, name: str) -> None:
        """A HUD button: wake up as if called, or go to sleep, cutting any reply short."""
        if not self.use_wake:
            return
        if name == "wake" and self.state.state is State.SLEEPING:
            self._wake_up(None)
        elif name == "sleep" and self.state.state is not State.SLEEPING:
            if self.busy:
                self._sleep_requested = True
                self.speaker.interrupt()
                self.task.cancel()
            else:
                self._go_to_sleep()

    def announce(self, text: str) -> None:
        """Speak something unprompted as soon as Jarvis is not busy or being spoken to."""
        self.pending.append(text)
        self._maybe_announce()

    def _maybe_announce(self) -> None:
        if not self.pending or self.busy:
            return
        if self.wake_task and not self.wake_task.done():
            return
        if self.state.state not in (State.SLEEPING, State.LISTENING) or self.segmenter.in_speech:
            return
        texts, self.pending = self.pending, []
        self.state.to(State.SPEAKING)
        self.task = asyncio.create_task(
            self._bounded_turn(functools.partial(self._speak_announcement, texts))
        )
        self.task.add_done_callback(self._after_turn)

    async def _speak_announcement(self, texts: list[str]) -> None:
        if self.chime:
            self.chime(earcon.ALERT)
            await asyncio.sleep(earcon.duration_s(earcon.ALERT) + 0.1)
        for text in texts:
            console.print(f"\n[bold cyan]{self.s.assistant_name} ›[/] {text}")
            self.conv.add_assistant_note(text)

        async def sentences() -> AsyncIterator[str]:
            for text in texts:
                yield text

        try:
            await self.speaker.speak(sentences())
        finally:
            end_reply(self.bus, self.speaker)

    def brief(self, task: asyncio.Task) -> None:
        """Take the activation briefing, started before the loop existed, as the current turn."""
        self.state.to(State.SPEAKING)
        self.task = task
        task.add_done_callback(self._after_turn)

    async def _bounded_turn(self, operation) -> None:
        from backend.audio.waiting import trace

        profile_token = current.set(Profile())
        owner = self.state.begin_request()
        request_token = request_context.set(owner)
        phase_token = phase_sink.set(self._phase)
        address_token = voice_addressed.set(True)
        music_token = voice_music_command.set(None)
        self.bus.request_id = owner
        trace("REQUEST_RECEIVED", source="audio")
        failed = True
        try:
            self.flush_audio()
            async with asyncio.timeout(TURN_TIMEOUT_S):
                await operation()
            failed = False
        except asyncio.CancelledError:
            log.info("PLAYBACK_CANCEL request=%s kind=main reason=request_cancelled", owner)
            raise
        except Exception as exc:
            self._phase("error")
            log.error("ERROR voice_turn type=%s", type(exc).__name__)
            self.bus.publish(
                {
                    "type": "notice",
                    "error": True,
                    "message": "Не удалось завершить запрос. Прослушивание восстановлено; "
                    "действие автоматически не повторяется.",
                }
            )
            console.print(
                "[red]Голосовой запрос прерван из-за ошибки или таймаута; слушаю снова.[/]"
            )
        finally:
            if failed and self.speaker is not None and asyncio.current_task() is self.task:
                self.speaker.interrupt()
            trace("REQUEST_COMPLETED", failed=failed)
            current.reset(profile_token)
            phase_sink.reset(phase_token)
            request_context.reset(request_token)
            voice_addressed.reset(address_token)
            voice_music_command.reset(music_token)

    def _after_turn(self, task: asyncio.Task) -> None:
        if task is not self.task:
            return
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("ERROR voice_task type=%s", type(error).__name__)
        self.state.to(State.LISTENING)
        self._phase("listening")
        log.info("LISTENING_RESUMED")
        if self._sleep_requested:
            self._sleep_requested = False
            self._barged_in = False
            self._go_to_sleep()
            return
        if self._barged_in:
            # The validating task owns the next turn and will reset the capture buffers.
            self._barged_in = False
            return
        self.mute_until = self.clock() + ECHO_TAIL_S
        if self.s.dialogue.enabled and self.session_deadline is not None:
            if self._session_accepted == self.state.request_id:
                self._open_session()
            else:
                self._session_timeout()
        else:
            if self.use_wake:
                self._go_to_sleep()
            else:
                self._listen_for(ECHO_TAIL_S + self.s.wakeword.follow_up_s)
        self.vad.reset()
        self.segmenter.reset()
        self.flush_audio()

    def on_frame(self, frame: np.ndarray) -> None:
        try:
            self._on_frame(frame)
        except Exception as exc:
            log.error("ERROR audio_frame type=%s", type(exc).__name__)
            self.vad.reset()
            self.segmenter.reset()
            log.info("LISTENING_RESUMED audio_detector")

    def _on_frame(self, frame: np.ndarray) -> None:
        if self.session_deadline is not None and not self.session_active and not self.busy:
            self._session_timeout()
        if self.bus.clients:
            self.bus.publish({"type": "level", "mic": level(frame)})
        if self.pending:
            self._maybe_announce()
        if self.state.state is State.SLEEPING:
            if self.wake_phrase:
                if self.clock() < self.mute_until or (self.wake_task and not self.wake_task.done()):
                    return
                event = self.segmenter.process(frame, self.vad(frame))
                if event.started:
                    log.info("RECORDING wake_check")
                if event.utterance is not None:
                    self.vad_wait_s = event.silence_s
                    self.wake_task = asyncio.create_task(
                        self._check_wake_phrase(
                            event.utterance, time.perf_counter() - event.silence_s
                        )
                    )
            elif self.wake:
                started = time.perf_counter()
                score = self.wake.detect(frame)
                if score is not None:
                    self.wake_detection_s = time.perf_counter() - started
                    self._wake_up(score)
            return
        # Not `busy`: the turn's done-callback may still be pending.
        working = self.state.state is not State.LISTENING
        if working and not self.s.audio.barge_in:
            return  # half duplex: don't listen while thinking or speaking
        if not working:
            now = self.clock()
            if now < self.mute_until:
                return
            expired = self.listen_deadline is not None and now >= self.listen_deadline
            if expired and not self.segmenter.in_speech:
                self._go_to_sleep()
                return
        event = self.segmenter.process(frame, self.vad(frame))
        if event.started:
            self._session_recording = self.session_active
            log.info(
                "RECORDING command vad_start_ms=%.0f threshold=%.2f",
                event.speech_ms,
                self.segmenter.threshold,
            )
        if event.utterance is not None:
            acoustic = evidence(event.utterance)
            log.info(
                "VAD_END duration_s=%.3f active_ms=%.0f rms=%.2f usable=%s working=%s",
                acoustic.duration_s,
                acoustic.active_ms,
                acoustic.rms,
                acoustic.usable,
                working,
            )
            log.info(
                "VAD_EVIDENCE speech_ms=%.0f end=%s stationary_noise=%s",
                event.speech_ms,
                event.end_reason,
                acoustic.stationary_noise,
            )
            if not acoustic.usable:
                log.info("STT_REJECT reason=no_acoustic_speech")
                return
            if working or self.busy:
                if self.busy and (self.barge_task is None or self.barge_task.done()):
                    self.barge_task = asyncio.create_task(
                        self._validate_barge(event.utterance, self.task)
                    )
                return
            self.vad_wait_s = event.silence_s
            self.state.to(State.THINKING)
            self.task = asyncio.create_task(
                self._bounded_turn(
                    functools.partial(
                        self.handle,
                        event.utterance,
                        time.perf_counter() - event.silence_s,
                        session_input=self._session_recording,
                    )
                )
            )
            self.task.add_done_callback(self._after_turn)

    async def _validate_barge(self, audio: np.ndarray, previous: asyncio.Task) -> None:
        """Never cancel an answer on VAD alone: background speech is also speech."""
        import re
        from difflib import SequenceMatcher

        started = time.perf_counter()
        recent = (
            tuple(getattr(self.speaker, "recent_spoken", []))
            if started - getattr(self.speaker, "last_audio_at", started) <= ECHO_TAIL_S
            else ()
        )
        try:
            text = await asyncio.wait_for(self.stt.transcribe(audio), STT_TIMEOUT_S)
            request = strip_wake_phrase(text, self.wake_phrase or self.s.assistant_name)
            if request is None:
                request = strip_wake_phrase(text, self.s.assistant_name)
            if request is None:
                request = strip_wake_phrase(text, f"эй {self.s.assistant_name}")
            nameless = False
            if (
                request is None
                and self.session_active
                and self.s.dialogue.nameless_interruptions
                and self.echo_ready
            ):
                self.headset_verified = await asyncio.to_thread(private_headset, self.s.audio)
                self._publish_session()
            if request is None and self.nameless_ready:
                acoustic = evidence(audio)
                if (
                    normalize(text) in CONTROLS | CLOSE
                    and acoustic.usable
                    and acoustic.active_ms >= 320
                    and getattr(self.stt, "last_speech_confident", False)
                ):
                    request, nameless = text, True

            def clean(value):
                return " ".join(re.findall(r"\w+", value.casefold()))

            echo = (
                any(
                    clean(text) in clean(spoken)
                    or SequenceMatcher(None, clean(text), clean(spoken)).ratio() >= 0.85
                    for spoken in recent
                )
                if text
                else False
            )
            decision = self._recipient(text, audio, addressed=request is not None, echo=echo)
            if (
                decision.accepted
                and decision.kind == "task"
                and self.task is previous
                and not previous.done()
            ):
                task = choose_task(text, self._recent_tasks())
                self.bus.publish(
                    {
                        "type": "notice",
                        "error": False,
                        "message": task.summary()
                        if task
                        else "Не могу однозначно определить задачу. Уточни с обращением.",
                    }
                )
                return  # status never cancels or reruns an operation
            if not decision.accepted:
                return
            if self.task is not previous or previous.done() or request is None or echo:
                log.info("BARGE_SKIP reason=stale_unaddressed_or_echo")
                return
            log.info("INTERRUPTED request=%s reason=addressed_speech", self.state.request_id)
            log.info("BARGE_ACCEPT nameless=%s", nameless)
            self.last_interrupted = True
            self._publish_session()
            self._phase("interrupted")
            self._barged_in = True
            # Cancellation belongs to the previous task; wait for its audio cleanup.
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)
            if self.task is not previous:
                return
            self._barged_in = False
            self.state.to(State.LISTENING)
            self.vad.reset()
            self.segmenter.reset()
            self.flush_audio()
            self._listen_for(self.s.wakeword.listen_timeout_s)
            self._open_session()
            if request:
                self.state.to(State.THINKING)
                self.task = asyncio.create_task(
                    self._bounded_turn(
                        functools.partial(
                            self.handle,
                            audio,
                            started,
                            transcript=request,
                            stt_elapsed=time.perf_counter() - started,
                            from_barge=True,
                            wake_validated=not nameless,
                        )
                    )
                )
                self.task.add_done_callback(self._after_turn)
        except Exception as exc:
            log.info("BARGE_SKIP reason=recognition_error type=%s", type(exc).__name__)

    async def _check_wake_phrase(self, audio: np.ndarray, speech_end: float) -> None:
        """Whisper checks custom phrases without involving the LLM or a network service."""
        start = time.perf_counter()
        if not evidence(audio).usable:
            log.info("STT_REJECT wake_check reason=no_acoustic_speech")
            return
        try:
            text = await asyncio.wait_for(self.stt.transcribe(audio), STT_TIMEOUT_S)
            log.info("STT_RESULT wake_check chars=%d night=%s", len(text), night_requested(text))
        except Exception:  # noqa: BLE001 - a missed activation must not stop listening
            log.error("ERROR wake_stt", exc_info=False)
            self.vad.reset()
            self.segmenter.reset()
            log.info("LISTENING_RESUMED wake_detector")
            return
        if self.state.state is not State.SLEEPING:
            return
        if discussing(text):
            log.info("ADDRESSEE confidence=low reason=third_person_discussion")
            return
        request = (
            text
            if is_morning_greeting(text, self.s.assistant_name, require_name=True)
            or night_addressed(text)
            else strip_wake_phrase(text, self.wake_phrase)
        )
        if request is None:
            for prefix in (self.s.assistant_name, f"эй {self.s.assistant_name}"):
                if (request := strip_wake_phrase(text, prefix)) is not None:
                    break
        if request is None:
            return
        elapsed = time.perf_counter() - start
        self.wake_detection_s = elapsed
        self._wake_up(None)
        if request:
            self.state.to(State.THINKING)
            self.task = asyncio.create_task(
                self._bounded_turn(
                    functools.partial(
                        self.handle,
                        audio,
                        speech_end,
                        transcript=request,
                        stt_elapsed=elapsed,
                        wake_validated=True,
                    )
                )
            )
            self.task.add_done_callback(self._after_turn)

    async def _say_by_code(self, user_text: str, signing_off: bool) -> None:
        """Fixed lines for a sign-off or a lone thank-you: the model echoed the sign-off
        ("Pode descansar, senhora") and said "Por nada, senhora. Disponha." (D-43)."""
        s = self.s
        from backend.audio.waiting import trace

        trace("REQUEST_ROUTE", route="local_signoff_or_thanks")
        addr = f", {s.owner_address}" if s.owner_address else ""
        if not signing_off:
            line = s.locale.welcome
        else:
            line = s.locale.sign_off_thanked if thanks(user_text) else s.locale.sign_off
        text = line.format(addr=addr)
        echo(text)
        self.conv.add_exchange(user_text, text)

        def on_start() -> None:
            trace("MAIN_AUDIO_START")
            if self.state.state is State.THINKING:
                self.state.to(State.SPEAKING)

        try:
            await self.speaker.speak(_once(text), on_start=on_start)
        finally:
            trace("MAIN_AUDIO_END")
            end_reply(self.bus, self.speaker)
        console.print()

    async def handle(
        self,
        audio: np.ndarray,
        speech_end: float,
        *,
        transcript: str | None = None,
        stt_elapsed: float = 0.0,
        session_input: bool = False,
        wake_validated: bool = False,
        from_barge: bool = False,
    ) -> None:
        s = self.s
        profile = current.get() or Profile()
        profile.add("vad_wait", self.vad_wait_s)
        profile.add("wake_detection", self.wake_detection_s)
        t0 = time.perf_counter()
        self.bus.publish({"type": "activity", "phase": "recognizing"})
        self._phase("recognizing")
        if transcript is None and not evidence(audio).usable:
            log.info("STT_REJECT request=%s reason=no_acoustic_speech", request_context.get())
            return
        try:
            text = (
                await asyncio.wait_for(self.stt.transcribe(audio), STT_TIMEOUT_S)
                if transcript is None
                else transcript
            )
        finally:
            stt_s = time.perf_counter() - t0 if transcript is None else stt_elapsed
            profile.add("stt", stt_s)
        log.info("STT_RESULT command chars=%d night=%s", len(text), night_requested(text))
        original_text = text
        addressed = wake_validated
        for prefix in (
            (self.wake_phrase, self.s.assistant_name, f"эй {self.s.assistant_name}")
            if self.s.dialogue.enabled
            else ()
        ):
            if prefix and (rest := strip_wake_phrase(text, prefix)) is not None:
                text, addressed = rest, True
                break
        if self.use_wake and self.s.dialogue.enabled:
            if addressed:
                self._open_session()
            if not self.session_active and not session_input:
                log.info("STT_REJECT reason=session_expired")
                return
        if addressed and not text:
            return
        if addressed:
            text = original_text
        if text and is_wake_phrase(text, s.assistant_name):
            console.print(f"[dim](обращение: «{text}» · продолжаю слушать)[/]")
            return
        if not text:
            log.info("STT_REJECT request=%s reason=empty_or_filtered", request_context.get())
            return
        decision = None
        if self.use_wake and s.dialogue.enabled and s.locale.lang == "ru":
            decision = self._recipient(
                text, audio, addressed=addressed, active=self.session_active or session_input
            )
            if not decision.accepted:
                return
            voice_addressed.set(decision.kind == "direct")
            if decision.kind == "music":
                voice_music_command.set(text)
        self._prompted_until = 0
        if decision is not None and decision.kind == "task":
            task = choose_task(text, self._recent_tasks())
            answer = task.summary() if task else "Не могу однозначно определить предыдущую задачу."
            self.bus.publish({"type": "user", "text": text})
            self.conv.add_exchange(text, answer)

            def on_status_start():
                if self.state.state is State.THINKING:
                    self.state.to(State.SPEAKING)

            try:
                await self.speaker.speak(_once(answer), on_start=on_status_start)
            finally:
                end_reply(self.bus, self.speaker)
            self._session_accepted = self.state.request_id
            return
        signing_off = is_sign_off(text, s.assistant_name)
        if normalize(text) in CLOSE:
            answer = "Хорошо. Позови, когда понадоблюсь."
            self.conv.add_exchange(text, answer)
            self.bus.publish({"type": "user", "text": text})
            self.request_sleep()
            self._close_session()
            self._publish_session()

            def on_close_start():
                if self.state.state is State.THINKING:
                    self.state.to(State.SPEAKING)

            try:
                await self.speaker.speak(_once(answer), on_start=on_close_start)
            finally:
                end_reply(self.bus, self.speaker)
            return
        self._session_accepted = self.state.request_id
        self.last_accepted_topic = (
            text if addressed or not self.last_accepted_topic else self.last_accepted_topic
        )
        if not from_barge:
            self.last_interrupted = False
        if self.use_wake and self.s.dialogue.enabled:
            self._open_session()
        if signing_off:
            # Decided here, not by the model: qwen3 often replied "I'll rest now" without
            # calling go_to_sleep. We sleep after the farewell.
            self.request_sleep()
        console.print(f"\n[bold]{s.locale.you_label} ›[/] {text}")
        console.print(f"[bold cyan]{s.assistant_name} ›[/] ", end="")
        self.bus.publish({"type": "user", "text": text})
        self.bus.publish({"type": "activity", "phase": "processing"})
        self._phase("processing")
        log.info("RESPONSE_START request=%s reason=accepted_stt", request_context.get())
        if s.locale.native_prompt and (signing_off or only_thanks(text, s.assistant_name)):
            await self._say_by_code(text, signing_off)
            return

        first_audio: float | None = None
        owner_task = asyncio.current_task()

        def on_start() -> None:
            nonlocal first_audio
            if owner_task is not self.task or owner_task.done() or owner_task.cancelling():
                return
            self._phase("speaking")
            first_audio = time.perf_counter()
            queued = getattr(self.speaker, "audio_enqueued_at", None)
            playback = getattr(self.speaker, "playback_started_at", None)
            if queued is not None and playback is not None:
                profile.add("playback_queue", playback - queued)
            if self.state.state is State.THINKING:  # Piper reports from another thread, late
                self.state.to(State.SPEAKING)
            publish_stats()

        def publish_stats():
            totals = profile.totals()
            model_used = (
                "model_first_chunk" in totals
                or "model_total" in totals
                or any("_attempt_" in name for name in totals)
            )
            response = first_audio - speech_end if first_audio is not None else None
            self.bus.publish(
                {
                    "type": "stats",
                    "stt_s": stt_s,
                    "first_token_s": totals.get(
                        "model_first_chunk", totals.get("provider_first_chunk")
                    )
                    if model_used
                    else 0.0,
                    "voice_s": response,
                    "response_s": response,
                    "tts_s": totals.get("tts_generation", 0.0),
                    "model_used": model_used,
                    "stages": totals,
                    "attempts": [row for row in profile.stages if "_attempt_" in row["stage"]],
                    "tokens_per_second": self.conv.last_stats.tokens_per_second,
                    "tools": self.conv.last_stats.tools,
                }
            )

        try:
            await reply_aloud(
                self.conv,
                self.speaker,
                text,
                s.tts.sentence_min_chars,
                on_token=echo,
                on_start=on_start,
            )
        except LLMError as exc:
            log.error("ERROR model type=%s request=%s", type(exc).__name__, request_context.get())
            self._phase("error")
            self.bus.publish(
                {
                    "type": "notice",
                    "error": True,
                    "message": "Модель не ответила. Можно повторить запрос.",
                }
            )
            publish_stats()
            return
        finally:
            end_reply(self.bus, self.speaker)
        st = self.conv.last_stats
        if st.quiet:
            self.request_sleep()  # e.g. the music asked for is playing: let it be heard
        details = [f"stt {stt_s:.2f}s"]
        if st.first_token_s is not None:
            details.append(f"первое слово модели {st.first_token_s:.2f} с")
        if tools := tools_detail(st):
            details.append(tools)
        if first_audio is not None:
            details.append(f"голос через {first_audio - speech_end:.2f} с")
        if st.tokens_per_second:
            details.append(f"{st.tokens_per_second:.0f} ток/с")
        console.print(f"\n[dim]{' · '.join(details)}[/]")
        publish_stats()
        log.info(
            "VOICE_TIMINGS stages=%s response_s=%s",
            profile.totals(),
            first_audio - speech_end if first_audio is not None else None,
        )


OLLAMA_WAIT_AT_LOGIN_S = 120
WINDOW_QUIT = "hud window quit"  # cancel message: Cmd+Q in the HUD window ends Jarvis
MIC_DENIED = (
    "Микрофон передаёт только тишину: вероятно, macOS запретила доступ приложению "
    "запуска Чушпана. Открой Системные настройки → Конфиденциальность и безопасность "
    "→ Микрофон. Разреши доступ приложению запуска и перезапусти его."
)


async def run_voice(s: Settings, at_login: bool = False, **kwargs) -> int:
    """`voice`, one instance at a time. At login, an instance already open is not an error."""
    from backend.instance import InstanceLock

    lock = InstanceLock(s.data_path / "jarvis.lock")
    if not lock.acquire():
        pid = lock.owner()
        running = f" (pid {pid})" if pid else ""
        console.print(f"[yellow]{s.assistant_name} уже запущен{running}.[/]")
        console.print("[dim]Остановить автозапуск: `uv run python -m backend autostart stop`[/]")
        return 0 if at_login else 1
    try:
        return await _run_voice(s, at_login=at_login, **kwargs)
    except asyncio.CancelledError as exc:
        if WINDOW_QUIT not in exc.args:
            raise
        return 0  # quit from the HUD window while loading: exit 0, so launchd lets it be
    finally:
        lock.release()


async def _run_voice(
    s: Settings,
    resume: bool = False,
    wake_word: bool | None = None,
    briefing: bool | None = None,
    started_at: float | None = None,
    hud: bool | None = None,
    at_login: bool = False,
) -> int:
    from backend.audio.capture import Microphone, SilenceWatch
    from backend.audio.stt import WhisperSTT
    from backend.audio.vad import SileroVAD, UtteranceSegmenter
    from backend.audio.wakeword import load_wake_model
    from backend.boot import BootProgress, wait_for_internet
    from backend.brain import briefing as brief
    from backend.hud.feeds import footprint_panel, start_feeds
    from backend.hud.server import HudServer, open_hud
    from backend.tools.news import NewsService
    from backend.tools.weather import WeatherService

    started_at = started_at or time.perf_counter()
    use_wake = s.wakeword.enabled if wake_word is None else wake_word
    use_briefing = s.briefing.enabled if briefing is None else briefing
    if use_briefing and not brief.due(s):
        console.print("[dim]Сегодня сводка уже прочитана (briefing.once_a_day)[/]")
        use_briefing = False
    use_hud = s.hud.enabled if hud is None else hud

    bus = HudBus()
    bus.publish({"type": "state", "state": "booting"})
    autostart_switch(s, bus)
    server = HudServer(s, bus) if use_hud else None
    if server and not await server.start():
        server = None
    window = None
    if server and s.hud.window:
        from backend.app_bundle import launcher
        from backend.hud.window import HudWindow

        window = HudWindow(server.url, s.assistant_name, *launcher(s))
        # Cmd+Q in the window quits Jarvis: the main loop below ends as with Ctrl+C.
        main_task = asyncio.current_task()
        window.on_quit = lambda: main_task.cancel(WINDOW_QUIT)
    if server and s.hud.open_on_start:
        background = [asyncio.create_task(open_hud(bus, server.url, window))]
    else:
        background = []

    steps = ["ollama", "location", "voice", "whisper", "wake", "model"]
    if use_briefing:
        steps[2:2] = ["internet", "weather", "news"]
        steps += ["briefing", "speech"]
    boot = BootProgress(bus, steps)

    # The profile decides how long Ollama keeps the model (D-37).
    llm = create_llm(
        s.model_copy(
            update={"llm": s.llm.model_copy(update={"keep_alive": s.resources.keep_alive})}
        )
    )
    boot.start("ollama")
    if at_login and not await llm.wait_ready(0):
        console.print("[dim]Ожидание запуска Ollama…[/]")
        if not await llm.wait_ready(OLLAMA_WAIT_AT_LOGIN_S):
            console.print(f"[red]Ollama не отвечает по адресу {s.llm.host}[/]")
            for task in background:
                task.cancel()
            if window:
                await window.close()
            if server:
                await server.stop()
            await llm.aclose()
            return 1
    boot.done("ollama")
    # Before the model: the system prompt names the place (cached after the first run).
    await boot.track("location", resolve_location(s))
    store = MemoryStore.open_resilient(s.db_path)
    if not store.memory_available:
        console.print("[yellow]SQLite недоступна: долговременная память отключена.[/]")
    # The callbacks reach `loop`, created below, only after startup.
    timers = TimerManager(
        s,
        on_fire=lambda t: loop.announce(timers.announcement(t)),
        on_change=lambda: bus.publish({"type": "timers", "items": timers.snapshot()}),
    )
    extra = [sleep_tool(lambda: loop.request_sleep())] if use_wake else []
    # Shared with the briefing, so "more news?" right after it hits the cache.
    weather = WeatherService(s) if "weather" in s.tools.enabled else None
    news = NewsService(s) if "news" in s.tools.enabled else None
    media = MediaService(s, wake=use_wake, store=store) if "media" in s.tools.enabled else None
    tools = build_registry(s, timers, extra, weather, news, store, media)
    from backend.hud.workspace import bind_workspace

    bind_workspace(bus, s, store)
    if hasattr(llm, "on_status"):
        llm.on_status = bus.publish
        bus.publish(llm.snapshot())
    conv_id = store.last_conversation() if resume else store.desktop_conversation()
    conv = Conversation(
        s,
        llm,
        store,
        conv_id,
        tools,
        warm_after_turn=True,
        device=DeviceContext.local_mac(store.desktop_session()),
    )
    conv.on_tool = lambda name: bus.publish({"type": "tool", "name": name})
    export_button(s, bus, store, conv)
    stt = WhisperSTT(s.stt.model, s.stt_language, s.stt_hint, s.resources.mlx_cache_mb)
    speaker = make_speaker(s)
    speaker.on_sentence = write_as_spoken(bus)
    if isinstance(speaker, PiperSpeaker):
        speaker.on_level = lambda v: bus.publish({"type": "level", "out": v})
    aec = None
    if s.audio.barge_in and s.audio.echo_cancellation:
        if isinstance(speaker, PiperSpeaker):
            from backend.audio.aec import load_echo_canceller

            aec = load_echo_canceller()
            if aec:
                speaker.on_audio = aec.play
        else:
            console.print("[yellow]Эхоподавление требует Piper: используйте наушники[/]")
    bus.publish(
        {
            "type": "hello",
            "name": s.assistant_name,
            "language": s.language,
            "model": configured_label(s, llm),
            "owner": s.owner_address,
            "place": s.location.display_name,
            "tools": tools.names,
            "wake_word": use_wake,
            "wake_phrase": (s.wakeword.phrase or f"Hey {s.assistant_name}") if use_wake else None,
        }
    )

    mode = "попеременная речь"
    if s.audio.barge_in:
        mode = "прерывание голосом · эхоподавление" if aec else "прерывание голосом (в наушниках)"
    console.print(f"[bold cyan]{s.assistant_name}[/] · {configured_label(s, llm)} · {mode}")
    console.print(f"[dim]Инструменты: {', '.join(tools.names) or 'нет'}[/]")
    if use_wake:
        phrase = s.wakeword.phrase or f"Hey {s.assistant_name}"
        console.print(f"[dim]Скажите «{phrase}» для активации. Ctrl+C — выход.[/]")
    else:
        console.print(
            "[dim]Слушаю постоянно (без фразы активации). Говорите как обычно. Ctrl+C — выход.[/]"
        )
    if server:
        console.print(f"[dim]HUD: {server.url}[/]")

    timings: dict[str, float] = {}  # when each part was ready, since start

    async def ready(name: str, work):
        result = await boot.track(name, work)
        timings[name] = time.perf_counter() - started_at
        return result

    def load_wake() -> WakeWordDetector | None:
        if not use_wake or s.wakeword.phrase:
            return None
        return WakeWordDetector(
            load_wake_model(s.wakeword_path, s.wakeword_dir), s.wakeword.threshold
        )

    async def gather_data() -> brief.BriefingData:
        """Weather and news, after waiting for the network (Wi-Fi joins late at login)."""
        boot.start("internet")
        if not await wait_for_internet(s.briefing.network_wait_s):
            for name in ("internet", "weather", "news"):
                boot.set(name, "offline")
            return brief.BriefingData()
        boot.done("internet")
        data = await brief.fetch(conv, weather, news, on_step=boot.set)
        timings["data"] = time.perf_counter() - started_at
        return data

    # Everything loads at once behind the loading screen; the briefing is then written
    # and synthesized in full, so at 100% it is spoken without a pause (D-38).
    fetching = asyncio.create_task(gather_data()) if use_briefing else None
    loading = asyncio.gather(
        ready("voice", speaker.warmup()),
        ready("model", conv.prime()),
        ready("whisper", stt.warmup()),
        ready("wake", asyncio.to_thread(load_wake)),
    )
    speaking: asyncio.Task | None = None
    briefing_text = ""
    try:
        with console.status("Загрузка языковой модели, Whisper и голоса…"):
            _, _, _, wake = await loading
            if s.locale.lang == "ru" and isinstance(speaker, SileroSpeaker):
                try:
                    started_cache = time.perf_counter()
                    await speaker.prepare_wait_phrases()
                    log.info(
                        "WAIT_AUDIO_CACHE count=%d preparation_s=%.3f",
                        len(speaker.wait_audio),
                        time.perf_counter() - started_cache,
                    )
                except Exception as exc:
                    log.warning("WAIT_AUDIO_CACHE unavailable type=%s", type(exc).__name__)
            if fetching:
                data = await fetching
                briefing_text = await ready("briefing", brief.compose(conv, data))
                sentences = speech_sentences(briefing_text, s.tts.sentence_min_chars, s.locale.lang)
                await ready("speech", speaker.prepare(sentences))
    except Exception as exc:  # noqa: BLE001
        for task in (fetching, loading, *background):
            if task:
                task.cancel()
        console.print(f"\n[red]Не удалось запустить: {exc}[/]")
        console.print("Для диагностики: [bold]uv run python -m backend doctor[/].")
        if window:
            await window.close()
        if server:
            await server.stop()
        await llm.aclose()
        store.close()
        if aec:
            aec.close()
        return 1
    boot.finish()
    if briefing_text:
        speaking = asyncio.create_task(
            speak_briefing(s, speaker, _once(briefing_text), started_at, timings, bus)
        )
        if data.news:  # offline, the next start tries again

            def given(task: asyncio.Task) -> None:
                # Heard, even if cut short by a question; not if the speech itself failed.
                if task.cancelled() or task.exception() is None:
                    brief.mark_given(s)

            speaking.add_done_callback(given)

    chime = None
    if s.wakeword.chime:
        on_audio = aec.play if aec else None
        chime = functools.partial(earcon.play, device=s.audio.output_device, on_audio=on_audio)
    loop = VoiceLoop(
        s,
        conv,
        stt,
        speaker,
        SileroVAD(),
        UtteranceSegmenter(s.vad),
        wake=wake,
        chime=chime,
        bus=bus,
        wake_phrase=s.wakeword.phrase if use_wake else None,
    )
    loop.echo_ready = aec is not None
    loop.headset_verified = await asyncio.to_thread(private_headset, s.audio)
    loop._publish_session()

    async def show_footprint() -> None:
        await asyncio.sleep(FOOTPRINT_SETTLE_S)
        with contextlib.suppress(Exception):
            bus.publish(await footprint_panel(s))

    def on_resources(_: bool) -> None:
        if server:
            background.append(asyncio.create_task(show_footprint()))

    resources = Resources(
        s,
        conv,
        stt,
        on_change=on_resources,
        keep_stt_loaded=bool(use_wake and s.wakeword.phrase),
    )
    loop.state.add_listener(resources.on_state)
    if media:
        loop.state.add_listener(media.on_state)
    if loop.state.state is State.SLEEPING:
        resources.on_state(None, State.SLEEPING)
    if speaking:
        loop.brief(speaking)
    if server:
        # After the briefing's fetch: the news are then a cache hit.
        background += start_feeds(s, bus, weather, news)
    mic = Microphone(s.audio.input_device)
    loop.flush_audio = mic.flush
    silence = SilenceWatch()

    try:
        try:
            mic.start()
        except Exception as exc:  # noqa: BLE001 - clean up models/window on input failure
            from backend.permissions import instruction

            log.warning("Microphone failed to start: %s", type(exc).__name__)
            console.print("Не удалось открыть микрофон. " + instruction("microphone"))
            return 1
        async for frame in mic.frames():
            if silence(frame):
                console.print(f"[red]{MIC_DENIED}[/]")
            for cleaned in aec.process(frame) if aec else (frame,):
                loop.on_frame(cleaned)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        mic.stop()
        loop._close_session()
        timers.cancel_all()
        resources.close()
        if loop.busy:
            speaker.interrupt()
            loop.task.cancel()
        if loop.wake_task:
            loop.wake_task.cancel()
        if loop.barge_task:
            loop.barge_task.cancel()
        for task in background:
            task.cancel()
        if window:
            await window.close()
        if server:
            await server.stop()
        await llm.aclose()
        store.close()
        if aec:
            aec.close()

    addr = f", {s.owner_address}" if s.owner_address else ""
    console.print(f"\n[cyan]{s.assistant_name}:[/] {s.locale.farewell.format(addr=addr)}")
    return 0
