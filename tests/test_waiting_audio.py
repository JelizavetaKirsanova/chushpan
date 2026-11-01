import asyncio
import random
import threading
from pathlib import Path

import numpy as np
import pytest

from backend.audio.tts import SileroSpeaker
from backend.audio.waiting import PHRASES, WaitingPhrases
from backend.brain.llm import LLMError
from backend.voice import _once, reply_aloud
from tests.test_voice_loop import FakeConversation, FakeSpeaker


def test_variety_recent_exclusion_and_short_duration_ratio():
    assert 15 <= len(PHRASES) <= 20
    durations = {phrase: 1 if index < 13 else 3 for index, phrase in enumerate(PHRASES)}
    picker = WaitingPhrases(random.Random(42))
    seen = []
    for _ in range(2000):
        phrase = picker.choose(durations)
        assert phrase not in seen[-4:]
        seen.append(phrase)
    assert set(seen) == set(PHRASES)
    assert 0.65 < sum(durations[phrase] <= 2 for phrase in seen) / len(seen) < 0.75
    assert picker.choose({}) is None


@pytest.mark.parametrize("mode", ["fast", "slow", "uncached", "error", "llm_error", "local_finish"])
def test_waiting_is_separate_interruptible_and_does_not_block_reply(mode):
    async def scenario():
        conv = FakeConversation()
        conv.s.language = "ru-RU"
        conv.s.tools.one_moment_after_s = 0.001
        speaker = FakeSpeaker()
        speaker.waiting_phrases = WaitingPhrases(random.Random(2))
        speaker.wait_durations = {} if mode == "uncached" else {PHRASES[0]: 1}
        waiting_started, release = asyncio.Event(), asyncio.Event()
        heard, displayed, hud = [], [], []
        local_completed = []
        speaker.on_sentence = lambda *args: hud.append(args)
        original = speaker.speak

        async def speak(sentences, on_start=None):
            async for sentence in sentences:
                if sentence in PHRASES:
                    heard.append(sentence)
                    waiting_started.set()
                    await asyncio.Event().wait()
                else:
                    await original(_once(sentence), on_start)
            return True

        async def reply(command):
            if mode == "local_finish":

                async def local_command():
                    await release.wait()
                    local_completed.append(True)
                    return "Готово."

                yield await local_command()
                return
            if mode in {"slow", "error", "llm_error"}:
                await release.wait()
            elif mode == "uncached":
                await asyncio.sleep(0.01)
            if mode == "error":
                raise RuntimeError("reply failure")
            if mode == "llm_error":
                raise LLMError("reply failure")
            yield "Готово."

        speaker.speak = speak
        conv.reply = reply
        task = asyncio.create_task(reply_aloud(conv, speaker, "Запрос", 1, displayed.append))
        if mode in {"slow", "error", "llm_error", "local_finish"}:
            await asyncio.wait_for(waiting_started.wait(), 1)
            assert not task.done()
            release.set()
        if mode in {"error", "llm_error"}:
            with pytest.raises(
                LLMError if mode == "llm_error" else RuntimeError, match="reply failure"
            ):
                await asyncio.wait_for(task, 1)
        else:
            assert await asyncio.wait_for(task, 1)
            assert displayed == ["Готово."]
            assert speaker.said == ["Готово."]
            assert all(entry[0] == "Готово." for entry in hud)
        assert len(heard) == (1 if mode in {"slow", "error", "llm_error", "local_finish"} else 0)
        assert speaker.on_sentence is not None
        assert local_completed == ([True] if mode == "local_finish" else [])

    asyncio.run(scenario())


def test_silero_wait_audio_is_prepared_once_and_reused_without_synthesis():
    async def scenario():
        speaker = SileroSpeaker(Path("unused"))
        generated = []

        def synth(text):
            generated.append(text)
            return np.zeros(480, dtype=np.int16)

        def play(queue, *args):
            while queue.get() is not None:
                pass
            return True

        speaker._load = lambda: None
        speaker._synth = synth
        speaker._play = play
        try:
            await speaker.prepare_wait_phrases()
            await speaker.prepare_wait_phrases()
            await speaker.speak(_once(PHRASES[0]))
            await speaker.speak(_once(PHRASES[0]))
            assert generated == list(PHRASES)
            assert len(speaker.wait_audio) == len(PHRASES)
        finally:
            speaker._synth_pool.shutdown(wait=True)

    asyncio.run(scenario())


def test_cancelled_filler_stops_audio_worker_before_main_reply():
    async def scenario():
        speaker = SileroSpeaker(Path("unused"))
        speaker._load = lambda: None
        speaker.wait_audio = {"Секунду.": np.zeros(480, dtype=np.int16)}
        started, stopped = threading.Event(), threading.Event()

        def play(queue, *args):
            queue.get()
            started.set()
            speaker._stop.wait(1)
            stopped.set()
            return False

        speaker._play = play
        try:
            task = asyncio.create_task(speaker.speak(_once("Секунду.")))
            assert await asyncio.to_thread(started.wait, 1)
            speaker.interrupt()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert stopped.is_set()
            assert not speaker.speaking
        finally:
            speaker._stop.set()
            speaker._synth_pool.shutdown(wait=True)

    asyncio.run(scenario())
