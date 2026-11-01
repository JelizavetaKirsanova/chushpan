"""Inspect the actual TTS queue with a fake player; never open speakers or run music."""

import asyncio
import logging
import time
from pathlib import Path

import numpy as np
import pytest

from backend.audio.tts import SileroSpeaker
from backend.audio.waiting import PHRASES, WaitingPhrases, allow_waiting
from backend.brain.conversation import Conversation
from backend.brain.llm import ChatChunk
from backend.config import Settings
from backend.memory.store import MemoryStore
from backend.voice import reply_aloud
from tests.test_morning import morning_conv
from tests.test_voice_loop import FakeConversation, Harness


def queued_speaker():
    speaker = SileroSpeaker(Path("unused"))
    speaker._load = lambda: None
    speaker._synth = lambda _: np.ones(240, dtype=np.int16)
    speaker.wait_audio = {phrase: np.ones(240, dtype=np.int16) for phrase in PHRASES}
    speaker.wait_durations = {phrase: 1 for phrase in PHRASES}
    speaker.waiting_phrases = WaitingPhrases()
    speaker.played = []
    speaker.wait_started = False

    def player(queue, on_start, on_level, on_sentence):
        while (item := queue.get()) is not None:
            sentence, _ = item
            speaker.played.append(sentence)
            if on_start:
                on_start()
                on_start = None
            if sentence in PHRASES:
                speaker.wait_started = True
                queue.stop.wait(1)
        return not queue.stop.is_set()

    speaker._play = player
    return speaker


def test_real_local_morning_never_enqueues_hardcoded_filler(tmp_path, monkeypatch, caplog):
    conv, _ = morning_conv(tmp_path, monkeypatch)
    conv.s.tools.one_moment_after_s = 0.001
    speaker = queued_speaker()
    try:
        with caplog.at_level(logging.INFO):
            assert asyncio.run(reply_aloud(conv, speaker, "Доброе утро, Чушпан", 20))
        assert not any(phrase in sentence for sentence in speaker.played for phrase in PHRASES)
        assert speaker.played[0] == "Доброе утро."
        assert not speaker.wait_started
        assert "event=WAIT_TIMER_START" not in caplog.text
        assert "route=local_morning" in caplog.text
        assert "event=REQUEST_COMPLETED" in caplog.text
    finally:
        speaker._synth_pool.shutdown()
        conv.store.close()


@pytest.mark.parametrize("greeting", ["Доброе утро", "Привет", "Добрый вечер", "Спокойной ночи"])
@pytest.mark.parametrize("delay", [0, 0.02])
def test_model_greeting_fast_or_delayed_has_no_waiting_queue(
    greeting, delay, tmp_path, caplog, monkeypatch
):
    # Exercise the provider path even for the phrase also bound to the confirmed night routine.
    monkeypatch.setattr("backend.brain.conversation.night_requested", lambda _: False)

    class Model:
        async def chat_stream(self, *args, **kwargs):
            await asyncio.sleep(delay)
            yield ChatChunk(content="Привет. Рад тебя слышать.", done=True)

    settings = Settings()
    settings.language = "ru-RU"
    settings.tools.one_moment_after_s = 0.001
    conv = Conversation(settings, Model(), MemoryStore(tmp_path / "greeting.db"))
    speaker = queued_speaker()
    try:
        with caplog.at_level(logging.INFO):
            assert asyncio.run(reply_aloud(conv, speaker, greeting, 1))
        assert not speaker.wait_started
        assert not any(sentence in PHRASES for sentence in speaker.played)
        assert "route=model" in caplog.text
        assert "event=WAIT_TIMER_START" not in caplog.text
    finally:
        speaker._synth_pool.shutdown()
        conv.store.close()


def test_greetings_after_long_turn_repeat_and_session_keep_queue_clean():
    async def scenario():
        speaker = queued_speaker()
        conv = FakeConversation()
        conv.s.language = "ru-RU"
        conv.s.tools.one_moment_after_s = 0.001
        release = asyncio.Event()

        async def long_reply(_):
            await release.wait()
            yield "Длинный ответ готов."

        conv.reply = long_reply
        try:
            task = asyncio.create_task(reply_aloud(conv, speaker, "Объясни сложную задачу", 1))
            deadline = time.monotonic() + 1
            while not speaker.wait_started:
                assert time.monotonic() < deadline
                await asyncio.sleep(0.001)
            release.set()
            assert await asyncio.wait_for(task, 1)
            count = len(speaker.played)
            for _ in range(2):
                conv.reply = FakeConversation("Доброе утро.").reply
                assert await reply_aloud(conv, speaker, "Доброе утро", 1)
            assert speaker.played[count:] == ["Доброе утро.", "Доброе утро."]
            assert sum(sentence in PHRASES for sentence in speaker.played) == 1
            assert speaker._waiting_turn is None
        finally:
            speaker._synth_pool.shutdown()

    asyncio.run(scenario())


def test_late_main_callback_after_completed_turn_is_inert(caplog):
    async def scenario():
        conv, callbacks, starts = FakeConversation("Добрый вечер."), [], []
        conv.s.language = "ru-RU"
        speaker = queued_speaker()

        async def defer(sentences, on_start=None):
            _ = [sentence async for sentence in sentences]
            callbacks.append(on_start)
            return True

        speaker.speak = defer
        try:
            assert await reply_aloud(
                conv, speaker, "Добрый вечер", 1, on_start=lambda: starts.append(1)
            )
            callbacks[0]()  # delayed audio-thread callback delivered after completion
            await asyncio.sleep(0.01)
            assert not starts and not speaker.played
        finally:
            speaker._synth_pool.shutdown()

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert "event=MAIN_AUDIO_START" not in caplog.text


def test_request_finishes_before_timer_and_never_queues_waiting(caplog):
    async def scenario():
        conv = FakeConversation("Готово.")
        conv.s.language = "ru-RU"
        conv.s.tools.one_moment_after_s = 0.01
        speaker = queued_speaker()
        try:
            assert await reply_aloud(conv, speaker, "Расскажи интересное", 1)
            await asyncio.sleep(0.03)
            assert speaker.played == ["Готово."] and not speaker.wait_started
        finally:
            speaker._synth_pool.shutdown()

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert "event=WAIT_TIMER_START" in caplog.text
    assert "event=WAIT_TIMER_CANCEL" in caplog.text
    assert "reason=answer_ready" in caplog.text
    assert "event=WAIT_AUDIO_START" not in caplog.text
    lines = [record.message for record in caplog.records if "VOICE_EVENT" in record.message]
    assert all("request=-" in line and " t=" in line for line in lines)


def test_false_or_empty_followup_after_greeting_creates_no_response():
    async def scenario():
        h = Harness(wake=False)
        h.s.language = "ru-RU"
        h.loop.conv = FakeConversation("Привет.")
        h.loop.speaker = queued_speaker()
        try:
            await reply_aloud(h.loop.conv, h.loop.speaker, "Привет", 1)
            before = list(h.loop.speaker.played)
            await h.loop.handle(np.zeros(16000, dtype=np.int16), 0)
            await h.loop.handle(np.zeros(16000, dtype=np.int16), 0, transcript="")
            assert h.loop.speaker.played == before and not h.loop.speaker.wait_started
            assert h.stt.calls == 0
        finally:
            h.loop.speaker._synth_pool.shutdown()

    asyncio.run(scenario())


def test_waiting_policy_does_not_suppress_substantive_greeting_question():
    assert allow_waiting("Доброе утро, расскажи новости и погоду")
    assert not allow_waiting("") and not allow_waiting("Эй, Чушпа́н, добрый вечер!")


def test_active_session_greeting_recovers_listening_without_waiting():
    from backend.addressee import Decision
    from backend.state import State

    async def scenario():
        h = Harness()
        h.s.language, h.s.assistant_name = "ru-RU", "Чушпан"
        h.s.dialogue.enabled = True
        h.loop.conv = FakeConversation("Привет.")
        h.loop.conv.s.language = "ru-RU"
        h.loop.speaker = queued_speaker()
        # Addressing has already been validated by mocked acoustic/session events.
        h.loop._recipient = lambda *args, **kwargs: Decision("high", "test_session", "continuation")
        h.loop._wake_up(None)
        h.loop.state.to(State.THINKING)
        try:
            h.loop.task = asyncio.create_task(
                h.loop._bounded_turn(
                    lambda: h.loop.handle(
                        np.zeros(16000, dtype=np.int16), 0, transcript="Привет", session_input=True
                    )
                )
            )
            await h.loop.task
            h.loop._after_turn(h.loop.task)
            assert h.loop.state.state is State.LISTENING
            assert h.loop.session_active
            assert h.loop.speaker.played == ["Привет."]
            assert not h.loop.speaker.wait_started
        finally:
            h.loop.speaker._synth_pool.shutdown()

    asyncio.run(scenario())


def test_late_waiting_task_rechecks_answer_ready_even_if_cancel_is_delayed(monkeypatch, caplog):
    real_create = asyncio.create_task

    def delayed_create(coro, *args, **kwargs):
        if coro.cr_code.co_name != "play_waiting":
            return real_create(coro, *args, **kwargs)

        async def late():
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                pass  # simulate an already delivered callback that misses cancellation
            await coro

        return real_create(late(), *args, **kwargs)

    async def scenario():
        conv = FakeConversation()
        conv.s.language = "ru-RU"
        conv.s.tools.one_moment_after_s = 0.001
        speaker = queued_speaker()

        async def reply(_):
            await asyncio.sleep(0.02)
            yield "Ответ готов."

        conv.reply = reply
        try:
            assert await reply_aloud(conv, speaker, "Объясни задачу", 1)
            assert speaker.played == ["Ответ готов."]
            assert not speaker.wait_started
        finally:
            speaker._synth_pool.shutdown()

    monkeypatch.setattr(asyncio, "create_task", delayed_create)
    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert "reason=stale_or_answer_ready" in caplog.text


def test_completed_audio_player_discards_delayed_thread_notification():
    async def scenario():
        from backend.voice import _once

        speaker, callbacks, starts = queued_speaker(), [], []

        def player(queue, notify, *args):
            while queue.get() is not None:
                pass
            callbacks.append(notify)
            return True

        speaker._play = player
        try:
            await speaker.speak(_once("Привет."), on_start=lambda: starts.append(1))
            callbacks[0]()
            await asyncio.sleep(0.01)
            assert not starts
        finally:
            speaker._synth_pool.shutdown()

    asyncio.run(scenario())
