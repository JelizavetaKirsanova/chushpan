import asyncio
from unittest.mock import AsyncMock

import pytest

from backend import voice
from backend.state import State
from tests.test_voice_loop import FRAME, FakeConversation, FakeSpeaker, Harness, custom_harness


@pytest.mark.parametrize(
    "text",
    [
        "Чушпан, я спать",
        "Эй, Чушпан, я спать",
        "Чушпан. Я спать.",
        "Чушпан, включи режим сна",
        "Чушпан, запусти ночной режим",
    ],
)
def test_night_wakes_and_routes_without_second_stt(text):
    async def scenario():
        h = custom_harness(text)
        heard = []

        async def reply(command):
            heard.append(command)
            yield "Подтверди ночной сценарий кодом."

        h.loop.conv.reply = reply
        await h.frames(20, prob=0.9)
        await h.frames(35)
        await h.loop.wake_task
        await h.loop.task
        await asyncio.sleep(0)
        assert heard == [text]
        assert h.stt.calls == 1
        assert h.state is State.LISTENING

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["stt", "reply", "tts"])
@pytest.mark.parametrize("failure", ["timeout", "error"])
def test_hung_stage_times_out_then_next_turn_succeeds(monkeypatch, stage, failure):
    async def scenario():
        h = Harness(text="Какая погода?", wake=False)
        h.loop.speaker = FakeSpeaker()
        h.loop.conv = FakeConversation("Работает.")
        monkeypatch.setattr(voice, "STT_TIMEOUT_S", 0.01)
        monkeypatch.setattr(voice, "TURN_TIMEOUT_S", 0.03)

        async def hang(*args, **kwargs):
            if failure == "error":
                raise RuntimeError("simulated stage error")
            await asyncio.Event().wait()

        if stage == "stt":
            h.stt.transcribe = hang
        elif stage == "tts":
            h.loop.speaker.speak = hang
        else:

            async def reply(command):
                await hang()
                yield "unreachable"

            h.loop.conv.reply = reply
        await h.frames(20, prob=0.9)
        await h.frames(35)
        await h.loop.task
        await asyncio.sleep(0)
        assert h.state is State.LISTENING and not h.loop.busy
        h.stt.transcribe = AsyncMock(return_value="Какая погода?")
        h.loop.speaker = FakeSpeaker()
        h.loop.conv = FakeConversation("Работает.")
        h.now += 1
        await h.frames(20, prob=0.9)
        await h.frames(35)
        await h.loop.task
        await asyncio.sleep(0)
        assert h.loop.speaker.said == ["Работает."]
        assert h.state is State.LISTENING

    asyncio.run(scenario())


def test_stale_callback_cannot_reset_new_turn():
    async def scenario():
        h = Harness(wake=False)
        old = asyncio.create_task(asyncio.sleep(0))
        await old
        h.loop.task = asyncio.create_task(asyncio.sleep(0))
        h.loop.state.to(State.THINKING)
        h.loop._after_turn(old)
        assert h.state is State.THINKING
        await h.loop.task

    asyncio.run(scenario())


def test_detector_error_does_not_stop_frame_loop():
    h = Harness(wake=False)
    h.loop.vad = type(
        "Detector",
        (),
        {
            "__call__": lambda self, frame: (_ for _ in ()).throw(RuntimeError()),
            "reset": lambda self: None,
        },
    )()
    h.loop.on_frame(FRAME)
    assert h.state is State.LISTENING


def test_microphone_watchdog_reopens_stream_without_real_recording(monkeypatch):
    from unittest.mock import Mock

    from backend.audio.capture import Microphone

    async def scenario():
        mic = Microphone()
        mic.stop = Mock()
        mic.start = Mock(side_effect=lambda: mic._put(FRAME))
        original = asyncio.wait_for
        attempts = 0

        async def wait_for(operation, timeout):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                operation.close()
                raise TimeoutError
            return await original(operation, timeout)

        monkeypatch.setattr(asyncio, "wait_for", wait_for)
        frames = mic.frames()
        assert await anext(frames) is FRAME
        await frames.aclose()
        mic.stop.assert_called_once()
        mic.start.assert_called_once()

    asyncio.run(scenario())
