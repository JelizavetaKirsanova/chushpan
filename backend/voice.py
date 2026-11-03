"""Voice conversation loop: `uv run python -m backend voice`."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import itertools
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar

import numpy as np
from rich.console import Console

from backend.addressee import SpeechSignals, TaskSnapshot, choose_task, decide, discussing
from backend.audio import earcon
from backend.audio.activity import (
    apply_controls,
    controls,
    decoder_confident,
    echoes_output,
    stop_requested,
)
from backend.audio.evidence import evidence
from backend.audio.level import level
from backend.audio.sentences import SentenceSplitter, clean_for_speech
from backend.audio.stt import (
    is_hallucination,
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
from backend.context import (
    DeviceContext,
    voice_addressed,
    voice_control_command,
    voice_led_command,
    voice_music_command,
    voice_session,
)
from backend.dialogue import CONTROLS, close_requested, follow_up, normalize, private_headset
from backend.hud import HudBus
from backend.latency import Profile, current, measure
from backend.memory.store import MemoryStore
from backend.night import night_addressed, night_requested
from backend.resources import Resources
from backend.security import command_text
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
        intro = ""
        async for token in tokens:
            if on_token:
                on_token(token)
            for sentence in splitter.feed(token):
                if spoken := (sentence if lang == "ru" else clean_for_speech(sentence, lang)):
                    if (
                        lang == "ru"
                        and not intro
                        and spoken.casefold().strip(".!? ")
                        in {"конечно", "с удовольствием", "давай разберёмся", "сейчас расскажу"}
                    ):
                        intro = spoken
                        continue
                    if intro:
                        spoken, intro = intro + " " + spoken, ""
                    yield spoken
        if (rest := splitter.flush()) and (
            spoken := (rest if lang == "ru" else clean_for_speech(rest, lang))
        ):
            yield (intro + " " + spoken).strip()
            intro = ""
        if intro:
            yield intro

    return await speaker.speak(sentences(), on_start=on_start)


async def reply_aloud(
    conv: Conversation,
    speaker: Speaker,
    user_text: str,
    min_chars: int,
    on_token: Callable[[str], None] | None = None,
    on_start: Callable[[], None] | None = None,
    on_silent_token: Callable[[str], None] | None = None,
) -> bool:
    # Own the callback and both kinds of playback until cancellation cleanup completes.
    lock = getattr(speaker, "_reply_lock", None)
    if lock is None:
        lock = speaker._reply_lock = asyncio.Lock()
    owned = current.get() is None
    profile = current.get() or Profile()
    timing_token = current.set(profile)
    if store := getattr(conv, "store", None):
        store.performance.attach(profile, conv.id)
    status = "completed"
    try:
        with measure("reply_queue"):
            await lock.acquire()
        try:
            from backend.audio.waiting import trace

            token = None
            if request_context.get() is None:
                token = request_context.set(next(_standalone_requests))
                trace("REQUEST_RECEIVED", source="reply")
            try:
                if not user_text.strip():
                    profile.result_status = "rejected"
                    trace("REQUEST_REJECTED", reason="empty_text")
                    return False
                if store is not None:
                    from backend.settings_models import silent_reply

                    if silent_reply(store, conv.s):
                        async for piece in conv.reply(user_text):
                            if piece and on_token:
                                on_token(piece)
                            if piece and on_silent_token:
                                on_silent_token(piece)
                        if finish := getattr(conv, "finish_reply", None):
                            await finish(False)
                        return True
                completed = await _reply_aloud(
                    conv, speaker, user_text, min_chars, on_token, on_start
                )
                if not completed:
                    status = "cancelled"
                return completed
            finally:
                if token is not None:
                    trace("REQUEST_COMPLETED")
                    request_context.reset(token)
        finally:
            lock.release()
    except BaseException as exc:
        status = (
            "cancelled" if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else "failed"
        )
        profile.fail(type(exc).__name__)
        raise
    finally:
        if owned:
            profile.finish(status)
        current.reset(timing_token)


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

    queue = asyncio.Queue(maxsize=32)
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
                    await queue.put(piece)
                    if piece.strip():
                        ready.set()
        finally:
            ready.set()
            if not asyncio.current_task().cancelling():
                await queue.put(end)

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
            first_audio = getattr(speaker, "playback_started_at", None)
            if first_audio is None and not isinstance(speaker, SaySpeaker):
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
        self.vad_started_at: float | None = None
        self.wake_detection_s = 0.0
        self.listen_deadline: float | None = None  # None = listen forever
        self._barged_in = False
        self._sleep_requested = False
        self.pending: list[str] = []  # announcements waiting for a quiet moment
        self.flush_audio: Callable[[], None] = lambda: None
        self.barge_task: asyncio.Task | None = None
        self.session_deadline: float | None = None
        self._session_timer: asyncio.TimerHandle | None = None
        self._session_paused_at: float | None = None
        self._session_remaining = 0.0
        self._wake_pending = deque(maxlen=round(STT_TIMEOUT_S / 0.032))
        self._last_segment = None
        self._session_accepted = -1
        self._session_recording = False
        self._follow_up_ready = False
        self.echo_ready = False
        self.headset_verified = False
        self.last_interrupted = False
        self.last_accepted_topic = ""
        self._prompted_until = 0.0
        self._session_started_at: float | None = None
        self._last_activation = float("-inf")
        self._next_wake_check = 0.0
        self.input_processor = None
        self.microphone_running = False
        self.microphone_starting = False
        self._accepted_audio_events = deque(maxlen=64)
        self.task_snapshots: dict[int, TaskSnapshot] = {}
        registry = getattr(conv, "tools", None)
        if registry is not None:
            registry.on_execution = self._observe_task
        self.bus = bus or HudBus()
        self.bus.on_command = self.command
        self.state.add_listener(self._publish_state)
        self._publish_state(None, self.state.state)
        self.bus.handle("dialogue_settings", self.configure_dialogue)
        self.bus.handle("voice_settings", self.configure_voice)
        self.bus.handle("voice_status", lambda _: self._publish_voice())
        self._publish_session()

    def _recognition(self, text: str, result: str, reason: str = "") -> None:
        # Ephemeral local UI only. Transcript text is not added to timing traces/logs.
        profile = current.get()
        self.bus.publish(
            {
                "type": "voice_recognition",
                "text": text,
                "result": result,
                "reason": reason,
                "request_id": self.state.request_id,
                "trace_id": profile.request_id if profile else None,
            }
        )

    def _observe_task(self, execution_id, name, status, result) -> None:
        owner = request_context.get()
        if owner is None:
            return  # CLI/chat/background jobs don't become the current voice task.
        if status == "result":
            status = "unknown"
            if isinstance(result, dict):
                if result.get("result_status") == "requires_confirmation":
                    status = "confirmation_required"
                elif result.get("result_status") == "partial_success":
                    status = "partial"
                elif result.get("result_status") == "failed" or result.get("error"):
                    status = "failed"
                elif name == "led_control" and result.get("written") is True:
                    status = "submitted"
                elif result.get("result_status") == "success":
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
        controls = getattr(self.conv, "controls", None)
        control_context = bool(
            controls
            and controls.plan(
                text,
                ((self.conv.device.device_id, self.conv.device.session_id), self.conv.id),
                self.s.led.comfortable_brightness,
            )
            is not None
        )
        automations = getattr(registry, "automations", None)
        if not control_context and automations and automations.recognize(text)["scenario_id"]:
            control_context = True
        # A separately spoken wake word grants just the first request in its short window.
        prompted = not topic and self.clock() < self._prompted_until
        decision = decide(
            text,
            addressed=addressed,
            active=self.session_active if active is None else active,
            topic=topic,
            signals=SpeechSignals(evidence(audio).usable, decoder_confident(self.stt), echo),
            task=choose_task(text, self._recent_tasks()),
            strictness=self.s.dialogue.addressee_strictness,
            confirmation_pending=pending,
            followup_pending=bool(
                self.conv
                and hasattr(self.conv, "pending_followup")
                and self.conv.pending_followup()
            ),
            control_context=control_context,
            continuation_window=self._follow_up_ready and self.session_active,
            background_filter=self.s.dialogue.background_filter,
        )
        if (
            prompted
            and not decision.accepted
            and decision.reason in {"recipient_unclear", "strict_wake_required"}
            and follow_up(text)
            and decoder_confident(self.stt)
        ):
            decision = decide(
                text,
                addressed=True,
                active=self.session_active,
                topic=topic,
                signals=SpeechSignals(evidence(audio).usable, True, echo),
            )
        if profile := current.get():
            profile.update(
                session_active=self.session_active, continuation_window=self._follow_up_ready
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
            and (self._session_paused_at is not None or self.clock() < self.session_deadline)
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
                "remaining_s": self._session_remaining
                if self._session_paused_at is not None
                else max(0, (self.session_deadline or self.clock()) - self.clock()),
                "paused": self._session_paused_at is not None,
                "recording": self.segmenter.in_speech,
                "phase": "follow_up_listening"
                if self.session_active and self.state.state is State.LISTENING
                else self.state.state.value,
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
        if self._session_started_at is None:
            self._session_started_at = self.clock()
        self._session_paused_at = None
        self.session_deadline = min(
            self.clock() + self.s.dialogue.duration_s,
            self._session_started_at + self.s.dialogue.max_session_s,
        )
        self.listen_deadline = self.session_deadline
        if self._session_timer:
            self._session_timer.cancel()
        self._session_timer = asyncio.get_running_loop().call_later(
            max(0.01, self.session_deadline - self.clock()), self._session_timeout
        )
        log.info(
            "SESSION_ACTIVE request=%s duration_s=%s",
            self.state.request_id,
            self.s.dialogue.duration_s,
        )
        self._publish_session()

    def _pause_session(self, reason: str) -> None:
        if not self.session_active or self._session_paused_at is not None:
            return
        self._session_remaining = max(0, self.session_deadline - self.clock())
        self._session_paused_at = self.clock()
        self.listen_deadline = None
        if self._session_timer:
            self._session_timer.cancel()
            self._session_timer = None
        log.info("SESSION_PAUSED reason=%s remaining_s=%.3f", reason, self._session_remaining)
        self._publish_session()

    def _resume_session(self) -> None:
        if self._session_paused_at is None or self.session_deadline is None:
            return
        self._session_paused_at = None
        self.session_deadline = min(
            self.clock() + self._session_remaining,
            self._session_started_at + self.s.dialogue.max_session_s,
        )
        self.listen_deadline = self.session_deadline
        self._session_timer = asyncio.get_running_loop().call_later(
            max(0.01, self.session_deadline - self.clock()), self._session_timeout
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
        self._session_paused_at = None
        self._session_remaining = 0
        self._follow_up_ready = False
        self._session_started_at = None
        self.last_accepted_topic = ""
        if close := getattr(self.conv, "close_interaction", None):
            close()
        if self._session_timer:
            self._session_timer.cancel()
            self._session_timer = None

    def _publish_voice(self) -> None:
        self.bus.publish(
            {
                "type": "voice_settings",
                **controls(self.s).model_dump(),
                "activation_enabled": self.use_wake,
                "wake_source": "whisper" if self.wake_phrase else "onnx",
                "wake_threshold": self.s.wakeword.threshold,
                "min_avg_logprob": self.s.wakeword.min_avg_logprob,
                "max_no_speech_prob": self.s.wakeword.max_no_speech_prob,
                "max_session_s": self.s.dialogue.max_session_s,
                "noise_available": self.input_processor is not None,
                "noise_active": bool(
                    self.input_processor and self.input_processor.noise_suppression
                ),
                "microphone": (
                    "starting"
                    if self.microphone_starting
                    else "listening"
                    if self.microphone_running
                    else "stopped"
                ),
                "state": self.state.state.value,
            }
        )

    def configure_voice(self, value) -> None:
        import sqlite3

        from pydantic import ValidationError

        old_noise = self.input_processor.noise_suppression if self.input_processor else None
        try:
            if self.busy:
                raise ValueError("Дождись завершения ответа или останови его.")
            updated = self.s.model_copy(deep=True)
            selected = apply_controls(updated, value)
            if (
                selected.mode != "continuous"
                and not self.use_wake
                and not self.wake
                and not updated.wakeword.phrase
            ):
                raise ValueError("Для включения ONNX-активации нужен перезапуск приложения.")
            if selected.noise_suppression and self.input_processor is None:
                raise ValueError("Шумоподавление недоступно: модуль WebRTC не загружен.")
            if self.input_processor:
                self.input_processor.configure_noise(selected.noise_suppression)
            store = getattr(self.conv, "store", None)
            if store:
                store.set_preference("voice", "controls", selected.model_dump())
            self.s.audio, self.s.wakeword, self.s.dialogue, self.s.vad = (
                updated.audio,
                updated.wakeword,
                updated.dialogue,
                updated.vad,
            )
            self.wake_phrase = self.s.wakeword.phrase
            self.use_wake = selected.mode != "continuous"
            if self.wake:
                self.wake.threshold = self.s.wakeword.threshold
            from backend.audio.vad import UtteranceSegmenter

            self.segmenter = UtteranceSegmenter(self.s.vad)
            if self.use_wake:
                self._go_to_sleep()
            else:
                self._close_session()
                self.listen_deadline = None
                self.flush_audio()
                self.vad.reset()
                if self.state.state is State.SLEEPING:
                    self.state.to(State.LISTENING)
            self._publish_voice()
            self.bus.publish(
                {
                    "type": "voice_settings_result",
                    "ok": True,
                    "message": "Настройки голоса применены и сохранены."
                    if store
                    else "Настройки голоса применены до перезапуска; хранилище недоступно.",
                }
            )
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            if self.input_processor and old_noise is not None:
                self.input_processor.configure_noise(old_noise)
            message = (
                "Проверь значения настроек голоса."
                if isinstance(exc, ValidationError)
                else "Не удалось сохранить настройки голоса."
                if isinstance(exc, (OSError, sqlite3.Error))
                else str(exc)
            )
            self._publish_voice()
            self.bus.publish({"type": "voice_settings_result", "ok": False, "message": message})

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
        if store := getattr(self.conv, "store", None):
            store.set_preference("voice", "controls", controls(self.s).model_dump())
            store.set_preference(
                "voice",
                "dialogue",
                {
                    "nameless_interruptions": config.nameless_interruptions,
                    "addressee_strictness": config.addressee_strictness,
                },
            )
        if not config.enabled:
            self._close_session()
            if not self.busy and self.use_wake:
                self._go_to_sleep()
        elif self.session_deadline is not None:
            self._open_session()
        self._publish_session()
        self._publish_voice()
        self.bus.publish({"type": "dialogue_settings_result", "ok": True})

    def _publish_state(self, old: State | None, new: State) -> None:
        log.info("VOICE_STATE from=%s to=%s", old.value if old else "startup", new.value)
        self.bus.publish({"type": "state", "state": new.value, "request_id": self.state.request_id})
        self._publish_session()
        self._publish_voice()

    def _phase(self, name: str) -> None:
        owner = request_context.get() or self.state.request_id
        if self.state.set_phase(name, owner):
            self.bus.publish({"type": "activity", "phase": name, "request_id": owner})

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def _listen_for(self, seconds: float) -> None:
        self.listen_deadline = self.clock() + seconds if self.use_wake else None

    def _wake_up(self, score: float | None, *, preserve_input=False) -> None:
        if score is not None and self.clock() - self._last_activation < self.s.wakeword.cooldown_s:
            return
        self._last_activation = self.clock()
        self._session_started_at = self.clock()
        log.info("WAKE_DETECTED")
        self.last_accepted_topic = ""
        self._follow_up_ready = False
        self._prompted_until = self.clock() + self.s.wakeword.listen_timeout_s
        if self.wake_task and self.wake_task is not asyncio.current_task():
            self.wake_task.cancel()
        self.vad.reset()
        self.segmenter.reset()
        self.state.to(State.LISTENING)
        self._phase("listening")
        if not preserve_input:
            self.flush_audio()
        self._listen_for(self.s.wakeword.listen_timeout_s)
        self._open_session()
        if self.chime:
            self.chime(earcon.WAKE)
        why = self.wake_phrase or ("интерфейс" if score is None else f"активация {score:.2f}")
        console.print(f"\n[cyan]●[/] [dim]слушаю ({why})[/]")

    def _go_to_sleep(self) -> None:
        log.info(
            "SESSION_CLOSED reason=%s",
            "explicit_close" if self._sleep_requested else "idle_or_policy",
        )
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
        if name == "stop":
            if self.busy:
                self._sleep_requested = self.use_wake
                self.speaker.interrupt()
                self.task.cancel()
            return
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
        store = getattr(self.conv, "store", None)
        if store:
            from backend.settings_models import quiet_now, silent_reply

            policy = store.preference("settings", "notifications") or {}
            quiet = quiet_now(store, self.s)
            if policy.get("reminders") is False or quiet and quiet.pause_initiative:
                return
            if silent_reply(store, self.s):
                self.bus.publish({"type": "notice", "message": text, "error": False})
                return
        self.pending.append(text)
        self._maybe_announce()

    def _maybe_announce(self) -> None:
        if not self.pending or self.busy:
            return
        if store := getattr(self.conv, "store", None):
            from backend.settings_models import quiet_now, silent_reply

            quiet = quiet_now(store, self.s)
            suppressed = (store.preference("settings", "notifications") or {}).get(
                "reminders"
            ) is False or bool(quiet and quiet.pause_initiative)
            if suppressed or silent_reply(store, self.s):
                texts, self.pending = self.pending, []
                if not suppressed:
                    for text in texts:
                        self.bus.publish({"type": "notice", "message": text, "error": False})
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

    def submit_text(self, value: dict) -> bool:
        """Explicit local UI input, using the same conversation, tools and turn owner."""
        if (
            set(value) - {"text", "speak"}
            or not isinstance(value.get("text"), str)
            or not 0 < len(value["text"].strip()) <= 4000
            or type(value.get("speak", False)) is not bool
        ):
            self.bus.publish(
                {
                    "type": "notice",
                    "error": True,
                    "message": "Сообщение должно содержать от 1 до 4000 символов.",
                }
            )
            return False
        if self.busy:
            self.bus.publish(
                {
                    "type": "notice",
                    "error": True,
                    "message": "Дождись текущего ответа или останови его кнопкой микрофона.",
                }
            )
            return False
        if self.state.state is State.SLEEPING:
            if self.clock() < self.mute_until:
                return
            self.state.to(State.LISTENING)
        self.state.to(State.THINKING)
        self.task = asyncio.create_task(
            self._bounded_turn(
                functools.partial(
                    self._handle_text, value["text"].strip(), value.get("speak", False)
                ),
                kind="text",
            )
        )
        self.task.add_done_callback(self._after_turn)
        return True

    async def _handle_text(self, text: str, speak: bool) -> None:
        if center := getattr(self, "settings_center", None):
            await center.cancel_previews()
            center.capture_target = None
            if center.capture_timer:
                center.capture_timer.cancel()
        profile = current.get()
        profile.update(input_mode="text")
        self.conv.store.performance.attach(profile, self.conv.id)
        self.bus.publish({"type": "user", "text": text})
        self._phase("generating")

        def on_start():
            if self.state.state is State.THINKING:
                self.state.to(State.SPEAKING)

        try:
            if speak:
                await reply_aloud(
                    self.conv,
                    self.speaker,
                    text,
                    self.s.tts.sentence_min_chars,
                    on_start=on_start,
                    on_silent_token=lambda piece: self.bus.publish(
                        {"type": "reply", "delta": piece}
                    ),
                )
            else:
                async with contextlib.aclosing(self.conv.reply(text)) as stream:
                    async for piece in stream:
                        self.bus.publish({"type": "reply", "delta": piece})
                await self.conv.finish_reply()
        finally:
            end_reply(self.bus, self.speaker)
            totals = profile.totals()
            self.bus.publish(
                {
                    "type": "stats",
                    "trace_id": profile.request_id,
                    "stt_s": None,
                    "response_s": profile.marks.get("first_content_text"),
                    "voice_s": None,
                    "first_token_s": totals.get("model_first_chunk"),
                    "tts_s": totals.get("tts_generation"),
                    "stages": totals,
                    "model_used": "model_total" in totals,
                    "tokens_per_second": self.conv.last_stats.tokens_per_second,
                    "tools": self.conv.last_stats.tools,
                }
            )

    async def _bounded_turn(self, operation, *, kind: str = "voice") -> None:
        from backend.audio.waiting import trace

        profile = Profile(kind=kind)
        profile_token = current.set(profile)
        owner = self.state.begin_request()
        request_token = request_context.set(owner)
        phase_token = phase_sink.set(self._phase)
        address_token = voice_addressed.set(True)
        music_token = voice_music_command.set(None)
        led_token = voice_led_command.set(None)
        control_token = voice_control_command.set(None)
        session_token = voice_session.set(None)
        self.bus.request_id = owner
        trace("REQUEST_RECEIVED", source="audio" if kind == "voice" else "text")
        failed = True
        try:
            self._pause_session("processing")
            self.flush_audio()
            async with asyncio.timeout(TURN_TIMEOUT_S):
                await operation()
            failed = False
        except asyncio.CancelledError:
            log.info("PLAYBACK_CANCEL request=%s kind=main reason=request_cancelled", owner)
            raise
        except Exception as exc:
            profile.fail(type(exc).__name__)
            self._phase("error")
            import traceback
            from pathlib import Path

            site = traceback.extract_tb(exc.__traceback__)[-1]
            profile.update(error_component=Path(site.filename).name, error_line=site.lineno)
            log.error(
                "ERROR voice_turn type=%s location=%s:%d",
                type(exc).__name__,
                Path(site.filename).name,
                site.lineno,
            )
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
            profile.finish(
                "cancelled"
                if asyncio.current_task().cancelling()
                else "failed"
                if failed
                else "completed"
            )
            current.reset(profile_token)
            phase_sink.reset(phase_token)
            request_context.reset(request_token)
            voice_addressed.reset(address_token)
            voice_music_command.reset(music_token)
            voice_led_command.reset(led_token)
            voice_control_command.reset(control_token)
            voice_session.reset(session_token)

    def _after_turn(self, task: asyncio.Task) -> None:
        if task is not self.task:
            return
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("ERROR voice_task type=%s", type(error).__name__)
        self.state.to(State.LISTENING)
        self._phase("listening")
        log.info("LISTENING_RESUMED")
        self.bus.publish(
            {"type": "addressee", "state": "waiting", "request_id": self.state.request_id}
        )
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
                self._follow_up_ready = True
                self._open_session()
            else:
                self._resume_session()
                self._session_timeout()
        else:
            if self.use_wake:
                self._go_to_sleep()
            else:
                self._listen_for(ECHO_TAIL_S + self.s.wakeword.follow_up_s)
        # Speech may have started during the last playback block. Preserve its onset.
        if not self.segmenter.in_speech:
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
            if self.clock() < self.mute_until:
                self.segmenter.process(frame, 0.0)
                return
            if self.wake_phrase:
                if self.wake_task and not self.wake_task.done():
                    self._wake_pending.append(frame.copy())
                    return
                event = self.segmenter.process(frame, self.vad(frame))
                if event.started:
                    self.vad_started_at = time.perf_counter() - event.speech_ms / 1000
                    log.info("RECORDING wake_check")
                if event.utterance is not None:
                    self._last_segment = event
                    self._wake_pending.clear()
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
        if (
            working
            and not self.s.audio.barge_in
            and not (self.use_wake and self.state.state is State.SPEAKING)
        ):
            return
        if not working:
            now = self.clock()
            if now < self.mute_until and not self.segmenter.in_speech:
                self.segmenter.process(frame, 0.0)
                return
            expired = self.listen_deadline is not None and now >= self.listen_deadline
            if expired and not self.segmenter.in_speech:
                self._go_to_sleep()
                return
        event = self.segmenter.process(frame, self.vad(frame))
        if event.started:
            self.vad_started_at = time.perf_counter() - event.speech_ms / 1000
            self._session_recording = self.session_active
            self._pause_session("recording")
            log.info(
                "RECORDING command vad_start_ms=%.0f threshold=%.2f",
                event.speech_ms,
                self.segmenter.threshold,
            )
        if event.utterance is not None:
            self._last_segment = event
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
                self._resume_session()
                return
            if working or self.busy:
                if self.busy and (self.barge_task is None or self.barge_task.done()):
                    self.barge_task = asyncio.create_task(
                        self._validate_barge(
                            event.utterance, self.task, stop_only=not self.s.audio.barge_in
                        )
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

    async def _validate_barge(
        self, audio: np.ndarray, previous: asyncio.Task, *, stop_only=False
    ) -> None:
        """Never cancel an answer on VAD alone: background speech is also speech."""
        started = time.perf_counter()
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
                    (normalize(text) in CONTROLS or close_requested(text))
                    and acoustic.usable
                    and acoustic.active_ms >= 320
                    and decoder_confident(self.stt)
                ):
                    request, nameless = text, True

            if not decoder_confident(self.stt, self.s.wakeword):
                log.info("BARGE_SKIP reason=uncertain_stt")
                return
            echo = echoes_output(text, self.speaker, now=time.perf_counter())
            if stop_only and (request is None or not stop_requested(request)):
                return
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
            if request and stop_requested(request):
                self.bus.publish(
                    {"type": "notice", "error": False, "message": "Озвучка остановлена."}
                )
                return
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
        """Local activation stage; only native decoder evidence can open a dialogue."""
        now = self.clock()
        if (
            self.state.state is not State.SLEEPING
            or now < self._next_wake_check
            or now - self._last_activation < self.s.wakeword.cooldown_s
        ):
            log.info("ACTIVATION_SKIP reason=cooldown")
            return
        self._next_wake_check = now + self.s.wakeword.check_interval_s
        if not evidence(audio).usable:
            return
        profile = Profile(kind="activation", origin=speech_end)
        store = getattr(self.conv, "store", None)
        if store is not None:
            store.performance.attach(profile)
        token = current.set(profile)
        self._phase("wake_detection")
        status = "rejected"
        start = time.perf_counter()
        try:
            if self.vad_started_at is not None:
                profile.mark("vad_start", self.vad_started_at, allow_before_origin=True)
            profile.mark("vad_end", speech_end)
            profile.interval("vad_wait", speech_end, speech_end + self.vad_wait_s)
            with measure("wake_detection", wake_source="whisper"):
                text = await asyncio.wait_for(self.stt.transcribe(audio), STT_TIMEOUT_S)
            stt_completed_at = time.perf_counter()
            profile.update(
                wake_source="whisper",
                stt_chars=len(text),
                stt_result="recognized" if text else "empty",
                audio_duration_s=len(audio) / 16000,
                **(getattr(self.stt, "last_confidence", None) or {}),
            )
            reason = "not_addressed"
            request = None
            if not decoder_confident(self.stt, self.s.wakeword):
                reason = "uncertain_stt"
            elif echoes_output(text, self.speaker, now=time.perf_counter()):
                reason = "own_tts_echo"
            elif discussing(text):
                reason = "third_person_discussion"
            else:
                request = (
                    text
                    if is_morning_greeting(text, self.s.assistant_name, require_name=True)
                    or night_addressed(text)
                    else strip_wake_phrase(text, self.wake_phrase or self.s.assistant_name)
                )
                if request is None:
                    for prefix in (
                        self.s.assistant_name,
                        f"эй {self.s.assistant_name}",
                        f"привет {self.s.assistant_name}",
                    ):
                        request = strip_wake_phrase(text, prefix)
                        if request is not None:
                            break
            confidence = getattr(self.stt, "last_confidence", None) or {}
            log.info(
                "STT_EVIDENCE stage=wake chars=%d logprob=%s no_speech=%s compression=%s",
                len(text),
                confidence.get("min_avg_logprob"),
                confidence.get("max_no_speech_prob"),
                confidence.get("max_compression_ratio"),
            )
            center = getattr(self, "settings_center", None)
            if center and center.capture_wake(text, accepted=request is not None, reason=reason):
                profile.update(
                    route="wake_test",
                    reason="wake_test_accepted" if request is not None else reason,
                )
                status = "completed"
                return
            if request is None or self.state.state is not State.SLEEPING:
                profile.update(reason=reason)
                log.info("ACTIVATION_REJECT reason=%s", reason)
                return
            profile.update(reason="explicit_address")
            status = "activated"
            elapsed = stt_completed_at - start
            self.wake_detection_s = time.perf_counter() - start
            buffered = list(self._wake_pending)
            self._wake_pending.clear()
            self._wake_up(None, preserve_input=True)
            if not request:
                for frame in buffered:
                    self.on_frame(frame)
            if request and not is_hallucination(request):
                self.state.to(State.THINKING)
                self.task = asyncio.create_task(
                    self._bounded_turn(
                        functools.partial(
                            self.handle,
                            audio,
                            speech_end,
                            transcript=request,
                            stt_elapsed=elapsed,
                            stt_completed_at=stt_completed_at,
                            wake_validated=True,
                        )
                    )
                )
                self.task.add_done_callback(self._after_turn)
        except Exception as exc:
            log.warning("ACTIVATION_REJECT reason=recognition_error type=%s", type(exc).__name__)
            profile.update(reason="recognition_error")
            self.vad.reset()
            self.segmenter.reset()
        finally:
            profile.finish("cancelled" if asyncio.current_task().cancelling() else status)
            current.reset(token)
            if self.state.state is State.SLEEPING:
                self._phase("idle")

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
        stt_completed_at: float | None = None,
        session_input: bool = False,
        wake_validated: bool = False,
        from_barge: bool = False,
        input_mode: str = "microphone",
    ) -> None:
        s = self.s
        profile = current.get() or Profile()
        profile.kind = "voice"
        profile.update(input_mode=input_mode)
        profile.origin = speech_end
        profile.mark("user_speech_end", speech_end)
        if self.vad_started_at is not None:
            profile.mark("vad_start", self.vad_started_at, allow_before_origin=True)
        profile.mark("vad_end", speech_end)
        store = getattr(self.conv, "store", None)
        if store is not None:
            store.performance.attach(profile, self.conv.id)
        profile.interval("vad_wait", speech_end, speech_end + self.vad_wait_s)
        profile.add("wake_detection", self.wake_detection_s)
        t0 = time.perf_counter()
        stt_interval = profile.begin("stt") if transcript is None else None
        self.bus.publish({"type": "activity", "phase": "recognizing"})
        self._phase("recognizing")
        if transcript is None and not evidence(audio).usable:
            profile.result_status = "rejected"
            log.info("STT_REJECT request=%s reason=no_acoustic_speech", request_context.get())
            return
        recognized = False
        try:
            text = (
                await asyncio.wait_for(self.stt.transcribe(audio), STT_TIMEOUT_S)
                if transcript is None
                else transcript
            )
            recognized = True
        finally:
            stt_s = time.perf_counter() - t0 if transcript is None else stt_elapsed
            if transcript is None:
                profile.end(
                    stt_interval,
                    "cancelled"
                    if asyncio.current_task().cancelling()
                    else "ok"
                    if recognized
                    else "error",
                )
            else:
                end = stt_completed_at or time.perf_counter()
                profile.interval("stt", end - stt_s, end)
            profile.mark("stt_completed" if recognized else "stt_failed", at=stt_completed_at)
        self._recognition(text, "recognized")
        log.info("STT_RESULT command chars=%d night=%s", len(text), night_requested(text))
        profile.update(
            stt_chars=len(text),
            stt_result="recognized" if text else "empty",
            audio_duration_s=len(audio) / 16000,
        )
        if self._last_segment:
            profile.update(
                recording_end=self._last_segment.end_reason, speech_ms=self._last_segment.speech_ms
            )
        profile.update(
            wake_enabled=self.use_wake, **(getattr(self.stt, "last_confidence", None) or {})
        )
        settings_center = getattr(self, "settings_center", None)
        if settings_center and settings_center.capture(text, decoder_confident(self.stt)):
            return
        if not decoder_confident(self.stt) or echoes_output(
            text, self.speaker, now=time.perf_counter()
        ):
            profile.result_status = "rejected"
            profile.update(reason="uncertain_stt_or_echo")
            return
        if settings_center:
            await settings_center.cancel_previews()
        original_text = text
        automations = getattr(getattr(self.conv, "tools", None), "automations", None)
        if (
            automations
            and automations.capture_target
            and automations.capture(text, decoder_confident(self.stt))
        ):
            return
        addressed = wake_validated
        if (
            is_sign_off(text, s.assistant_name)
            and normalize(s.assistant_name) in normalize(text).split()
        ):
            addressed = True
        for prefix in (
            (
                self.wake_phrase,
                self.s.assistant_name,
                f"эй {self.s.assistant_name}",
                f"привет {self.s.assistant_name}",
            )
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
                profile.result_status = "rejected"
                log.info("STT_REJECT reason=session_expired")
                return
        if addressed and not text:
            profile.result_status = "rejected"
            return
        if addressed:
            text = original_text
        if is_hallucination(text) or (
            addressed and command_text(text) and is_hallucination(command_text(text))
        ):
            profile.result_status = "rejected"
            profile.update(reason="empty_or_filler")
            return
        if text and is_wake_phrase(text, s.assistant_name):
            profile.result_status = "rejected"
            console.print(f"[dim](обращение: «{text}» · продолжаю слушать)[/]")
            return
        if not text:
            profile.result_status = "rejected"
            log.info("STT_REJECT request=%s reason=empty_or_filtered", request_context.get())
            return
        decision = None
        if self.use_wake and s.dialogue.enabled and s.locale.lang == "ru":
            decision = self._recipient(
                text, audio, addressed=addressed, active=self.session_active or session_input
            )
            if not decision.accepted:
                profile.result_status = "rejected"
                profile.update(reason=decision.reason, stt_result="rejected")
                self._recognition(text, "rejected", decision.reason)
                return
            voice_addressed.set(decision.kind == "direct")
            if self.session_active or session_input:
                device = getattr(self.conv, "device", None)
                if device and device.authenticated:
                    voice_session.set((device.device_id, device.session_id))
            if decision.kind == "music":
                voice_music_command.set(text)
            elif decision.kind == "led":
                voice_led_command.set(text)
            elif decision.kind == "devices":
                voice_control_command.set(text)
        event_key = round(speech_end, 6)
        if speech_end > 0 and event_key in self._accepted_audio_events:
            profile.result_status = "rejected"
            profile.update(reason="duplicate_audio_event")
            log.info("STT_REJECT reason=duplicate_audio_event")
            return
        if speech_end > 0:
            self._accepted_audio_events.append(event_key)
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
        if close_requested(text):
            if not self.use_wake:
                # An explicit close ends continuous listening too; resume only on wake.
                self.use_wake = self.s.wakeword.enabled = True
                if not self.wake and not self.wake_phrase:
                    self.wake_phrase = f"эй {self.s.assistant_name}"
                    self.s.wakeword.phrase = self.wake_phrase
            answer = "Хорошо. Позови, когда понадоблюсь."
            self.conv.add_exchange(text, answer)
            self.bus.publish({"type": "user", "text": text})
            self.request_sleep()
            self._close_session()
            self._publish_session()
            self._publish_voice()

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
        self._follow_up_ready = False
        profile.update(stt_result="accepted")
        self._recognition(text, "accepted")
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
        self._pause_session("response")
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
            first_audio = getattr(self.speaker, "playback_started_at", None)
            if first_audio is None and not isinstance(self.speaker, SaySpeaker):
                first_audio = time.perf_counter()
            queued = getattr(self.speaker, "audio_enqueued_at", None)
            sentence_ready = getattr(self.speaker, "first_sentence_at", None)
            if queued is not None and sentence_ready is not None:
                profile.add("tts_first_audio", queued - sentence_ready)
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
            if isinstance(self.speaker, PiperSpeaker):
                response = profile.marks.get("first_content_audio")
            self.bus.publish(
                {
                    "type": "stats",
                    "trace_id": profile.request_id,
                    **profile.summary(),
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
                on_silent_token=lambda piece: self.bus.publish({"type": "reply", "delta": piece}),
            )
        except LLMError as exc:
            profile.fail(type(exc).__name__)
            log.error("ERROR model type=%s request=%s", type(exc).__name__, request_context.get())
            self._phase("error")
            self.bus.publish(
                {
                    "type": "notice",
                    "error": True,
                    "message": "Модель не ответила. Можно повторить запрос.",
                }
            )
            return
        finally:
            end_reply(self.bus, self.speaker)
            profile.add("response_total", time.perf_counter() - speech_end)
            if not asyncio.current_task().cancelling():
                publish_stats()
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
    desktop=None,
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

    bus = desktop.bus if desktop else HudBus()
    bus.publish({"type": "state", "state": "booting"})
    if desktop is None:
        autostart_switch(s, bus)
    server = HudServer(s, bus) if use_hud and desktop is None else None
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
    store = MemoryStore.open_resilient(s.db_path)
    from backend.audio.activity import migrate_interaction_defaults
    from backend.settings_models import apply_persisted

    try:
        migrate_interaction_defaults(store, s)
        apply_persisted(s, store)
    except (ValueError, TypeError):
        log.warning("SETTINGS invalid_saved_value using_config")
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
    if desktop is None:
        await boot.track("location", resolve_location(s))
    try:
        if saved_controls := store.preference("voice", "controls"):
            apply_controls(s, saved_controls)
            use_wake = s.wakeword.enabled if wake_word is None else wake_word
    except (ValueError, TypeError):
        log.warning("VOICE_SETTINGS invalid_saved_value using_config")
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
    if tools.automations:
        tools.automations.bind(bus, conv)
    export_button(s, bus, store, conv)
    stt = WhisperSTT(s.stt.model, s.stt_language, s.stt_hint, s.resources.mlx_cache_mb)
    speaker = make_speaker(s)
    speaker.on_sentence = write_as_spoken(bus)
    speaker.on_error = lambda message: bus.publish(
        {"type": "notice", "error": True, "message": message}
    )
    from backend.hud.dashboard import Dashboard

    dashboard = Dashboard(bus, s, store, conv, weather)
    if isinstance(speaker, PiperSpeaker):
        speaker.on_level = lambda v: bus.publish({"type": "level", "out": v})
    aec = None
    if s.audio.echo_cancellation or s.audio.noise_suppression:
        if isinstance(speaker, PiperSpeaker):
            from backend.audio.aec import load_echo_canceller

            aec = load_echo_canceller(
                noise_suppression=s.audio.noise_suppression,
                echo_cancellation=s.audio.echo_cancellation,
            )
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

    async def load_voice():
        return await asyncio.gather(
            ready("voice", speaker.warmup()),
            ready("model", conv.prime()),
            ready("whisper", stt.warmup()),
            ready("wake", asyncio.to_thread(load_wake)),
        )

    loading = asyncio.ensure_future(load_voice()) if desktop is None else None
    speaking: asyncio.Task | None = None
    briefing_text = ""
    try:
        with console.status("Загрузка языковой модели, Whisper и голоса…"):
            wake = None
            if loading is not None:
                _, _, _, wake = await loading
            if desktop is None and s.locale.lang == "ru" and isinstance(speaker, SileroSpeaker):
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
    loop.echo_ready = aec is not None and s.audio.echo_cancellation
    loop.input_processor = aec
    loop._publish_voice()
    if desktop is None:
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
    if desktop:
        resources.loaded = False
    loop.state.add_listener(resources.on_state)
    from backend.settings_center import SettingsCenter

    settings_center = SettingsCenter(s, store, conv, bus, loop=loop)
    if media:
        loop.state.add_listener(media.on_state)
    if loop.state.state is State.SLEEPING:
        resources.on_state(None, State.SLEEPING)
    if speaking:
        loop.brief(speaking)
    if server or desktop:
        # After the briefing's fetch: the news are then a cache hit.
        background += start_feeds(s, bus, weather, news)
        background += dashboard.start(loop)
    mic = Microphone(s.audio.input_device)
    loop.flush_audio = mic.flush
    loop.change_input_device = mic.set_device
    silence = SilenceWatch()

    async def desktop_warmup():
        # A failed LLM warmup does not disable local STT or settings.
        await desktop.ensure_ollama()
        results = await asyncio.gather(
            speaker.warmup(),
            stt.warmup(),
            asyncio.to_thread(load_wake),
            conv.prime(),
            return_exceptions=True,
        )
        for name, result in zip(("voice", "whisper", "wake", "model"), results, strict=True):
            desktop.components[name] = "error" if isinstance(result, BaseException) else "ready"
        if any(isinstance(result, BaseException) for result in results[:3]):
            raise RuntimeError("STT, TTS или модель активации недоступны")
        loop.wake = results[2]
        loop.use_wake = use_wake
        resources.loaded = True
        if use_wake and loop.state.state is State.LISTENING:
            loop.state.to(State.SLEEPING)

    async def standard_capture():
        mic.start()
        loop.microphone_running = True
        loop._publish_voice()
        async for frame in mic.frames():
            yield frame

    try:
        capture = desktop.capture(mic, loop, desktop_warmup) if desktop else standard_capture()
        async for frame in capture:
            if silence(frame):
                console.print(f"[red]{MIC_DENIED}[/]")
            for cleaned in aec.process(frame) if aec else (frame,):
                loop.on_frame(cleaned)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception as exc:
        from backend.permissions import instruction

        log.warning("Audio capture failed: %s", type(exc).__name__)
        console.print("Не удалось открыть микрофон. " + instruction("microphone"))
        return 1
    finally:
        mic.stop()
        loop.microphone_running = False
        loop._publish_voice()
        loop._close_session()
        timers.cancel_all()
        resources.close()
        await settings_center.close()
        if loop.busy:
            speaker.interrupt()
            loop.task.cancel()
        if loop.wake_task:
            loop.wake_task.cancel()
        if loop.barge_task:
            loop.barge_task.cancel()
        for task in background:
            task.cancel()
        await dashboard.close()
        if tools.automations:
            await tools.automations.close()
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
