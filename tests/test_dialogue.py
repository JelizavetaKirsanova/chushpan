import asyncio
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import voice
from backend.brain.conversation import Conversation
from backend.brain.llm import ChatChunk, LLMError
from backend.config import AudioConfig, Settings
from backend.dialogue import follow_up, private_headset
from backend.hud.server import allowed_origins, create_app
from backend.memory.store import MemoryStore
from backend.state import State
from tests.test_voice_loop import custom_harness
from tests.test_voice_reliability import SPEECH


async def finish(h):
    await h.loop.task
    await asyncio.sleep(0)


def test_wake_follow_up_and_expiry_use_same_conversation():
    async def scenario():
        h = custom_harness("Эй, Чушпан, какая завтра погода?")
        heard = []

        async def reply(text):
            heard.append(text)
            yield "Ответ."

        h.loop.conv.reply = reply
        await h.frames(20, prob=0.9)
        await h.frames(30)
        await h.loop.wake_task
        await finish(h)
        assert h.loop.session_active
        h.now += 1
        first_deadline = h.loop.session_deadline
        h.stt.text = "А в воскресенье?"
        await h.frames(20, prob=0.9)
        await h.frames(30)
        await finish(h)
        assert heard == ["какая завтра погода?", "А в воскресенье?"]
        assert h.loop.session_deadline > first_deadline
        h.now = h.loop.session_deadline + 1
        h.loop._session_timeout()
        assert h.state is State.SLEEPING
        assert not h.loop.session_active
        h.stt.text = "Какая завтра погода?"  # TV / background after expiry: no wake
        await h.frames(20, prob=0.9)
        await h.frames(30)
        await h.loop.wake_task
        assert len(heard) == 2
        h.loop._close_session()

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["Закончи разговор", "Закрой сессию", "Заверши диалог"])
def test_explicit_close_is_local_and_requires_wake_again(text):
    async def scenario():
        h = custom_harness(text)
        h.loop._wake_up(None)

        async def forbidden(_):
            raise AssertionError("Gemini must not run")
            yield

        h.loop.conv.reply = forbidden
        h.loop.state.to(State.THINKING)
        h.loop.task = asyncio.create_task(h.loop._bounded_turn(lambda: h.loop.handle(SPEECH, 0)))
        h.loop.task.add_done_callback(h.loop._after_turn)
        await finish(h)
        assert h.state is State.SLEEPING
        assert h.loop.speaker.said == ["Хорошо. Позови, когда понадоблюсь."]
        assert h.loop.session_deadline is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "text", ["они вчера снова приехали", "телевизор сейчас громче", "музыка играет"]
)
def test_background_statements_do_not_reach_gemini_or_extend_session(text):
    async def scenario():
        h = custom_harness(text)
        h.loop._wake_up(None)
        before = h.loop.session_deadline
        calls = []

        async def reply(_):
            calls.append(True)
            yield "Нельзя"

        h.loop.conv.reply = reply
        h.now += 1
        h.loop.state.to(State.THINKING)
        h.loop.task = asyncio.create_task(h.loop._bounded_turn(lambda: h.loop.handle(SPEECH, 0)))
        h.loop.task.add_done_callback(h.loop._after_turn)
        await finish(h)
        assert calls == []
        assert h.loop.session_deadline == before
        h.loop._close_session()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode",
    [
        "named",
        "nameless",
        "disabled",
        "no_aec",
        "tv",
        "own_tts",
        "low_confidence",
        "expired",
        "disconnected",
    ],
)
def test_session_barge_safety_and_recovery(monkeypatch, mode):
    async def scenario():
        h = custom_harness("Чушпан, скажи короче" if mode == "named" else "Скажи короче")
        h.loop._wake_up(None)
        h.s.audio.barge_in = True
        h.s.dialogue.nameless_interruptions = mode != "disabled"
        h.loop.echo_ready = mode != "no_aec"
        h.loop.headset_verified = True
        monkeypatch.setattr(voice, "private_headset", lambda _: mode != "disconnected")
        h.stt.last_speech_confident = mode != "low_confidence"
        if mode == "tv":
            h.stt.text = "А сейчас реклама"
        if mode == "own_tts":
            h.loop.speaker.recent_spoken = [h.stt.text]
        if mode == "expired":
            h.now = h.loop.session_deadline + 1
        h.loop.state.to(State.THINKING)
        previous = asyncio.create_task(asyncio.Event().wait())
        h.loop.task = previous
        try:
            await h.loop._validate_barge(SPEECH, previous)
            if mode in {"named", "nameless"}:
                assert previous.cancelled()
                await finish(h)
                assert h.state is State.LISTENING
                assert h.loop.session_active
                assert h.loop.last_interrupted
                assert h.loop.speaker.said == ["Хорошо."]
            else:
                assert not previous.done()
        finally:
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)
            h.loop._close_session()

    asyncio.run(scenario())


def test_noise_and_false_short_stt_do_not_renew_dialogue():
    async def scenario():
        h = custom_harness("Спасибо")
        h.loop._wake_up(None)
        deadline = h.loop.session_deadline
        calls = h.stt.calls
        import numpy as np

        noise = np.zeros(16000, dtype=np.int16)
        noise[2000] = 32000
        await h.loop.handle(noise, 0)
        assert h.stt.calls == calls
        assert h.loop.session_deadline == deadline
        assert h.loop.speaker.said == []
        h.loop._close_session()

    asyncio.run(scenario())


def test_llm_error_preserves_session_and_next_request():
    async def scenario():
        h = custom_harness("Какая погода?")
        h.loop._wake_up(None)

        async def failed(_):
            raise LLMError("mock failure")
            yield

        h.loop.conv.reply = failed
        await h.frames(20, prob=0.9)
        await h.frames(30)
        await finish(h)
        assert h.state is State.LISTENING and h.loop.session_active

        async def restored(_):
            yield "Восстановился."

        h.loop.conv.reply = restored
        h.stt.text = "А завтра?"
        h.now += 1
        await h.frames(20, prob=0.9)
        await h.frames(30)
        await finish(h)
        assert h.loop.speaker.said == ["Восстановился."]
        h.loop._close_session()

    asyncio.run(scenario())


def test_private_device_check_fails_closed_and_does_not_open_stream(monkeypatch):
    for names, expected in [
        (["MacBook Microphone", "AirPods"], False),
        (["AirPods Microphone", "AirPods"], True),
        (["MacBook Microphone", "MacBook Speakers"], False),
    ]:
        monkeypatch.setitem(
            sys.modules,
            "sounddevice",
            SimpleNamespace(
                query_devices=lambda _, kind, names=names: {
                    "name": names[0 if kind == "input" else 1]
                }
            ),
        )
        assert private_headset(AudioConfig()) == expected
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace())
    assert not private_headset(AudioConfig())


def test_runtime_settings_validate_and_acknowledge_over_websocket(tmp_path):
    h = custom_harness("")
    bus = h.loop.bus
    client = TestClient(create_app(bus, allowed_origins("127.0.0.1", 8765), tmp_path))
    with client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:8765"}) as ws:
        while ws.receive_json()["type"] != "transcript":
            pass
        for duration, ok in [(60, True), (0, False), ("120", False)]:
            ws.send_json({"type": "dialogue_settings", "value": {"duration_s": duration}})
            while (event := ws.receive_json())["type"] != "dialogue_settings_result":
                pass
            assert event["ok"] is ok
            assert h.s.dialogue.duration_s == 60


@pytest.mark.parametrize(
    "command",
    [
        "А завтра?",
        "Расскажи подробнее",
        "Нет, я имела в виду другое",
        "Повтори последнее",
        "Скажи короче",
        "Продолжай",
    ],
)
def test_existing_sqlite_context_is_used_and_repeat_never_calls_tools(command):
    class LLM:
        def __init__(self):
            self.calls = []

        async def chat_stream(self, messages, tools=None):
            self.calls.append((messages, tools))
            yield ChatChunk(content="Уточнённый ответ.")

    async def scenario():
        s = Settings(language="ru-RU")
        store = MemoryStore(":memory:")
        llm = LLM()
        conv = Conversation(s, llm, store)
        conv.add_exchange("Какая погода в Таллине?", "Сегодня ясно.")
        try:
            answer = "".join([part async for part in conv.reply(command)])
            if command == "Повтори последнее":
                assert answer == "Сегодня ясно."
                assert llm.calls == []
            else:
                messages, tools = llm.calls[-1]
                assert any(message["content"] == "Сегодня ясно." for message in messages)
                assert messages[-1]["content"].endswith(command)
                if command != "А завтра?":
                    assert tools == []
            assert store.recent(conv.id, 10)[-2]["content"] == command
        finally:
            store.close()

    asyncio.run(scenario())


def test_follow_up_gate_preserves_short_russian_questions_and_explicit_commands():
    for text in [
        "А завтра?",
        "А в воскресенье?",
        "Какая погода?",
        "я спать",
        "подтверждаю 1234",
        "подтверждаю один два три четыре",
        "подтверждаю код один два три четыре",
    ]:
        assert follow_up(text)


def test_utterance_started_before_expiry_can_finish_without_repeating_name():
    async def scenario():
        h = custom_harness("А завтра?")
        h.loop._wake_up(None)
        h.loop.last_accepted_topic = "Какая погода?"
        h.now = h.loop.session_deadline - 0.2
        await h.frames(20, prob=0.9)
        assert h.loop.segmenter.in_speech
        h.loop._session_timeout()
        assert h.state is State.LISTENING
        await h.frames(30)
        await finish(h)
        assert h.loop.speaker.said == ["Хорошо."]
        assert h.loop.session_active
        h.loop._close_session()

    asyncio.run(scenario())


def test_disabled_sessions_require_wake_after_each_reply():
    async def scenario():
        h = custom_harness("Эй, Чушпан, какая погода?")
        h.s.dialogue.enabled = False
        await h.frames(20, prob=0.9)
        await h.frames(30)
        await h.loop.wake_task
        await finish(h)
        assert h.state is State.SLEEPING
        assert not h.loop.session_active

    asyncio.run(scenario())
