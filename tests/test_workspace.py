import asyncio
import json

import pytest

from backend.brain.conversation import Conversation
from backend.brain.llm import ChatChunk, LLMError
from backend.brain.providers import FallbackClient, GeminiRequestError
from backend.config import Settings
from backend.hud.bus import HudBus
from backend.hud.workspace import bind_workspace
from backend.memory.store import MemoryStore
from tests.test_providers import Local, gemini


def test_history_uses_existing_store_and_excludes_technical_events():
    store = MemoryStore(":memory:")
    try:
        conversation_id = store.new_conversation()
        store.add(conversation_id, "user", "Привет")
        store.add(conversation_id, "assistant", "", tool_calls=[{"function": {"name": "test"}}])
        store.add(conversation_id, "tool", "technical event", tool_name="test")
        store.add(conversation_id, "assistant", "Слушаю")
        bus = HudBus()
        events = []
        bus.publish = events.append
        bind_workspace(bus, Settings(), store)
        bus.request("history", conversation_id)
        event = events[-1]
        assert [row["content"] for row in event["messages"]] == ["Привет", "Слушаю"]
        assert all(row["created_at"] for row in event["messages"])
        assert event["conversations"][0]["messages"] == 2
        assert store.count(conversation_id) == 4
    finally:
        store.close()


@pytest.mark.parametrize("code,cooldown", [(503, 30), (504, 30), (429, 60)])
def test_health_circuit_and_recovery_events(code, cooldown):
    async def scenario():
        primary, method = gemini(error=GeminiRequestError(code, transient=True, reason="test"))
        client = FallbackClient(primary, Local())
        events = []
        client.on_status = events.append
        assert [chunk async for chunk in client.chat_stream([])]
        status = client.snapshot()
        assert status["models"][0]["failures"] == 1
        assert status["models"][0]["status"] == "open"
        assert 0 < status["models"][0]["retry_in_s"] <= cooldown
        assert status["provider"] == "ollama"
        assert "test-private-key" not in json.dumps(events)
        client._cooldown_until[primary] = 0
        from tests.test_providers import response

        method.side_effect = None
        method.return_value = response("Восстановилась")
        assert [chunk async for chunk in client.chat_stream([])]
        assert client.last_provider == "gemini"
        assert client.snapshot()["models"][0]["successes"] == 1

    asyncio.run(scenario())


def test_cancelled_request_is_not_counted_as_model_failure():
    async def scenario():
        primary, method = gemini()
        started = asyncio.Event()

        async def blocked(**kwargs):
            started.set()
            await asyncio.Event().wait()

        method.side_effect = blocked
        client = FallbackClient(primary, Local())

        async def consume():
            return [chunk async for chunk in client.chat_stream([])]

        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert client.snapshot()["models"][0]["failures"] == 0
        assert not client._probing

    asyncio.run(scenario())


def test_failed_partial_answer_is_not_stored_as_completed():
    async def scenario():
        class Broken:
            async def chat_stream(self, *args, **kwargs):
                yield ChatChunk(content="Частичный текст. ")
                raise LLMError("network failed")

        store = MemoryStore(":memory:")
        try:
            conv = Conversation(Settings(language="ru-RU"), Broken(), store)
            with pytest.raises(LLMError):
                _ = [text async for text in conv.reply("Сохрани мой вопрос")]
            transcript = store.transcript(conv.id)
            assert [row["role"] for row in transcript] == ["user"]
            assert transcript[0]["content"] == "Сохрани мой вопрос"
            assert not conv._reply_lock.locked()
        finally:
            store.close()

    asyncio.run(scenario())


def test_history_is_delivered_over_existing_websocket_with_origin_guard():
    from fastapi.testclient import TestClient

    from backend.hud.server import create_app

    store = MemoryStore(":memory:")
    try:
        conversation_id = store.new_conversation()
        store.add(conversation_id, "user", "Тестовый вопрос")
        bus = HudBus()
        bind_workspace(bus, Settings(), store)
        with TestClient(create_app(bus, {"http://localhost:9100"})) as client:
            with client.websocket_connect(
                "/ws", headers={"origin": "http://localhost:9100"}
            ) as socket:
                initial = [socket.receive_json(), socket.receive_json()]
                assert {event["type"] for event in initial} == {"settings_info", "music_settings"}
                assert socket.receive_json()["type"] == "transcript"
                socket.send_json({"type": "history", "value": conversation_id})
                report = socket.receive_json()
                assert report["messages"][0]["content"] == "Тестовый вопрос"
                assert report["selected"] == conversation_id
    finally:
        store.close()
