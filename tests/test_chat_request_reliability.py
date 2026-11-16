import asyncio
from types import SimpleNamespace

from backend.brain.llm import LLMError
from backend.hud import HudBus
from backend.voice import VoiceOutputError, speak_tokens
from tests.test_voice_loop import Harness


def test_fast_repeated_text_messages_are_queued_and_run_in_order():
    async def scenario():
        harness = Harness(wake=False)
        events = harness.loop.bus.subscribe()
        started, release = asyncio.Event(), asyncio.Event()
        received = []

        async def handle(text, speak):
            received.append(text)
            harness.loop.bus.publish({"type": "user", "text": text})
            if text == "first":
                started.set()
                await release.wait()

        harness.loop._handle_text = handle
        assert harness.loop.submit_text({"text": "first"})
        first_task = harness.loop.task
        assert first_task is not None
        await started.wait()
        assert harness.loop.submit_text({"text": "second"})
        assert harness.loop.submit_text({"text": "third"})
        assert len(harness.loop._text_queue) == 2
        release.set()
        await first_task

        while harness.loop._text_queue or harness.loop.busy:
            task = harness.loop.task
            if task is not None:
                await task
            await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert received == ["first", "second", "third"]
        assert harness.loop.state.state.value == "listening"
        seen = []
        while not events.empty():
            seen.append(events.get_nowait())
        queued = [event["client_request_id"] for event in seen if event["type"] == "chat_queued"]
        accepted = [event["client_request_id"] for event in seen if event["type"] == "user"]
        assert len(set(queued)) == 3
        assert accepted == queued

    asyncio.run(scenario())


def test_text_input_runs_while_voice_session_is_active_without_wake_word():
    async def scenario():
        harness = Harness(wake=True)
        harness.loop.s.dialogue.enabled = True
        harness.loop.s.dialogue.duration_s = 0
        harness.loop._open_session()
        assert harness.loop.session_active
        received = []

        async def handle(text, speak):
            received.append(text)

        harness.loop._handle_text = handle
        assert harness.loop.submit_text({"text": "сколько времени?"})
        await harness.loop.task
        await asyncio.sleep(0)

        assert received == ["сколько времени?"]
        assert harness.loop.session_active

    asyncio.run(scenario())


def test_model_failure_creates_an_assistant_chat_message_and_is_bounded(monkeypatch):
    async def scenario():
        harness = Harness(wake=False)
        queue = harness.loop.bus.subscribe()

        async def fail():
            harness.loop.bus.publish({"type": "user", "text": "сколько сейчас времени?"})
            raise LLMError("provider unavailable")

        await harness.loop._bounded_turn(fail, kind="text")
        assert harness.loop.bus._lines[-2]["role"] == "user"
        assert harness.loop.bus._lines[-1]["role"] == "assistant"
        assert "Попробуй ещё раз" in harness.loop.bus._lines[-1]["text"]

        from backend import voice

        monkeypatch.setattr(voice, "TURN_TIMEOUT_S", 0.01)

        async def never_finishes():
            await asyncio.Event().wait()

        await harness.loop._bounded_turn(never_finishes, kind="text")
        assert "Не получилось обработать" in harness.loop.bus._lines[-1]["text"]
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert any("0.01 секунд" in event.get("message", "") for event in events)

    asyncio.run(scenario())


def test_tts_failure_still_drains_and_publishes_the_complete_generated_text():
    async def scenario():
        class BrokenSpeaker:
            on_sentence = None

            async def speak(self, sentences, on_start=None):
                async for _sentence in sentences:
                    raise OSError("audio output unavailable")

        class Conv:
            s = SimpleNamespace(locale=SimpleNamespace(lang="en"))
            store = None

            async def reply(self, _text):
                yield "First sentence. "
                yield "Second sentence."

            async def finish_reply(self, _completed=False):
                return None

        generated = []
        try:
            await speak_tokens(
                BrokenSpeaker(),
                Conv().reply("question"),
                1,
                "en",
                on_generated=generated.append,
            )
        except VoiceOutputError:
            pass
        else:
            raise AssertionError("TTS failure should be reported to the caller")

        assert "".join(generated) == "First sentence. Second sentence."

    asyncio.run(scenario())


def test_slow_hud_client_does_not_lose_request_or_transcript_events():
    bus = HudBus()
    queue = bus.subscribe()
    for index in range(700):
        bus.publish({"type": "reply", "delta": str(index)})
    assert queue.qsize() == 700
    assert [queue.get_nowait()["delta"] for _ in range(700)] == [str(i) for i in range(700)]

    bus.publish({"type": "level", "mic": 0.1})
    bus.publish({"type": "level", "mic": 0.2})
    assert queue.qsize() == 1
    assert queue.get_nowait()["mic"] == 0.2
