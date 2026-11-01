"""Persistent conversational follow-ups, without real browser/network/audio actions."""

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.addressee import SpeechSignals, decide
from backend.brain.conversation import Conversation
from backend.brain.followups import bounded_history
from backend.brain.llm import ChatChunk
from backend.config import Settings
from backend.context import DeviceContext, voice_addressed
from backend.memory.store import MemoryStore
from backend.music import music_tool
from backend.tools.registry import Tool, ToolRegistry
from backend.voice import reply_aloud
from tests.test_voice_loop import FakeSpeaker


class Model:
    cloud_context = True

    def __init__(self):
        self.requests = []

    async def chat_stream(self, messages, tools=None):
        self.requests.append((messages, tools))
        yield ChatChunk(content="Ответ готов.", done=True)


def setup(store, device=None, result=None):
    settings = Settings(language="ru-RU")
    settings.media.provider = "youtube_music"
    playlist = AsyncMock(
        return_value=result
        or {"playlists": [{"id": "verified-id", "title": "Favourite Songs"}], "complete": False}
    )
    media = SimpleNamespace(
        youtube=SimpleNamespace(playlist=playlist),
        play_music=AsyncMock(return_value={"status": "not_found"}),
    )
    registry = ToolRegistry(
        [
            music_tool(settings, media),
            Tool(
                "music_playlist",
                "test",
                playlist,
                {"action": {"type": "string", "enum": ["list", "assign"]}},
                ["action"],
            ),
        ]
    )
    conv = Conversation(settings, Model(), store, tools=registry, device=device)
    return conv, playlist, media


async def ask(conv, text):
    return "".join([part async for part in conv.reply(text)])


@pytest.mark.parametrize("answer", ["да", "давай", "давай, посмотри", "сделай", "попробуй ещё раз"])
def test_failed_favourite_then_accept_offer_runs_exact_read_action(answer):
    async def scenario():
        store = MemoryStore(":memory:")
        conv, playlist, media = setup(store)
        try:
            first = await ask(conv, "Открой мой любимый плейлист")
            assert "Могу посмотреть доступные плейлисты" in first
            assert conv.pending_followup()
            # A contextual, nameless continuation is not a broad system-action grant.
            token = voice_addressed.set(False)
            try:
                second = await ask(conv, answer)
            finally:
                voice_addressed.reset(token)
            assert "Favourite Songs" in second and "неполным" in second
            playlist.assert_awaited_once_with(action="list")
            media.play_music.assert_awaited_once_with("", kind="playlist", open_only=True)
            assert not conv.llm.requests and not conv.pending_followup()
            assert store.transcript(conv.id)[-2]["content"] == answer
            assert any(
                row.get("tool_name") == "music_playlist" for row in store.recent(conv.id, 24)
            )
            assert store.preference("youtube_music", "default") is None
        finally:
            store.close()

    asyncio.run(scenario())


def test_failed_read_then_explicit_retry_uses_stored_proposal():
    async def scenario():
        store = MemoryStore(":memory:")
        conv, playlist, _ = setup(store)
        playlist.side_effect = [
            {"error": "Safari недоступен"},
            {"playlists": [{"title": "Verified"}], "complete": True},
        ]
        try:
            await ask(conv, "Открой мой любимый плейлист")
            failed = await ask(conv, "давай")
            assert "Safari недоступен" in failed and "Verified" not in failed
            answer = await ask(conv, "попробуй ещё раз")
            assert "Verified" in answer and playlist.await_count == 2
        finally:
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["да", "сделай", "открой", "попробуй ещё раз"])
def test_unbound_short_reply_clarifies_without_guessing(text):
    async def scenario():
        store = MemoryStore(":memory:")
        conv, playlist, media = setup(store)
        try:
            assert "Уточни" in await ask(conv, text)
            playlist.assert_not_called()
            media.play_music.assert_not_called()
            assert not conv.llm.requests
        finally:
            store.close()

    asyncio.run(scenario())


def test_session_restores_history_and_pending_offer_across_restart_and_devices(tmp_path):
    path = tmp_path / "history.db"
    mac = DeviceContext.local_mac("server-issued-shared-session")

    async def before():
        store = MemoryStore(path)
        conv, _, _ = setup(store, mac)
        store.add_fact("Существующий факт")
        await ask(conv, "Открой мой любимый плейлист")
        identity = conv.id
        store.close()
        return identity

    identity = asyncio.run(before())

    async def after():
        store = MemoryStore(path)
        # Device descriptors alone grant no new capability: this is a trusted backend fixture.
        iphone = replace(mac, device_id="phone", device_type="iphone")
        conv, playlist, _ = setup(store, iphone)
        try:
            assert conv.id == identity and conv.pending_followup()
            assert "Favourite Songs" in await ask(conv, "Давай, посмотри")
            playlist.assert_awaited_once_with(action="list")
            assert len(store.facts()) == 1
            assert len(store.conversation_list()) == 1
            assert [row["role"] for row in store.transcript(identity)] == [
                "user",
                "assistant",
                "user",
                "assistant",
            ]
        finally:
            store.close()

    asyncio.run(after())


def test_different_or_unauthenticated_session_never_consumes_offer():
    async def scenario():
        store = MemoryStore(":memory:")
        mac = DeviceContext.local_mac("one")
        first, _, _ = setup(store, mac)
        await ask(first, "Открой мой любимый плейлист")
        for device in [replace(mac, session_id="two"), replace(mac, authenticated=False)]:
            other, playlist, _ = setup(store, device)
            assert other.id != first.id
            assert "Уточни" in await ask(other, "давай")
            playlist.assert_not_called()
        assert first.pending_followup()
        store.close()

    asyncio.run(scenario())


def test_duplicate_accepts_cannot_execute_proposal_twice():
    async def scenario():
        store = MemoryStore(":memory:")
        device = DeviceContext.local_mac("shared")
        first, playlist, _ = setup(store, device)
        second, other, _ = setup(store, device)
        await ask(first, "Открой мой любимый плейлист")
        results = await asyncio.gather(ask(first, "давай"), ask(second, "давай"))
        assert sum("Favourite Songs" in answer for answer in results) == 1
        assert playlist.await_count + other.await_count == 1
        store.close()

    asyncio.run(scenario())


def test_expired_or_ambiguous_offer_cannot_be_used():
    async def scenario():
        store = MemoryStore(":memory:")
        conv, playlist, _ = setup(store)
        await ask(conv, "Открой мой любимый плейлист")
        proposal = conv.pending_followup()
        proposal["expires"] = time.time() - 1
        store.set_preference("dialogue_pending", str(conv.id), proposal)
        assert "Уточни" in await ask(conv, "давай")
        conv.add_exchange("Что дальше?", "Могу посмотреть плейлисты или открыть папку.")
        assert "Уточни" in await ask(conv, "давай")
        playlist.assert_not_called()
        store.close()

    asyncio.run(scenario())


def test_full_tool_results_stored_but_outbound_context_bounded():
    store = MemoryStore(":memory:")
    conv, _, _ = setup(store)
    result = {"playlists": [{"id": str(i), "title": "Playlist " + str(i)} for i in range(80)]}
    trace = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "music_playlist", "arguments": {"action": "list"}}}
            ],
        },
        {"role": "tool", "tool_name": "music_playlist", "content": json.dumps(result)},
    ]
    conv._persist("Покажи мои плейлисты", trace, "Список получен.")
    raw = store.recent(conv.id, 24)
    assert json.loads(raw[2]["content"]) == result
    prompt = bounded_history(raw, 4000, "stale")
    assert len(json.loads(prompt[2]["content"])["playlists"]) == 20
    assert json.loads(store.recent(conv.id, 24)[2]["content"]) == result
    store.close()


def test_cloud_keeps_history_after_tool_and_sends_twenty_four_messages():
    async def scenario():
        class ToolModel(Model):
            async def chat_stream(self, messages, tools=None):
                self.requests.append(([dict(m) for m in messages], tools))
                if len(self.requests) == 1:
                    yield ChatChunk(
                        tool_calls=[{"function": {"name": "read_test", "arguments": {}}}], done=True
                    )
                else:
                    yield ChatChunk(content="Связный ответ.", done=True)

        store = MemoryStore(":memory:")
        model = ToolModel()
        conv = Conversation(
            Settings(language="ru-RU"),
            model,
            store,
            tools=ToolRegistry([Tool("read_test", "test", AsyncMock(return_value={"ok": True}))]),
        )
        for i in range(20):
            conv.add_exchange("Вопрос " + str(i), "Ответ " + str(i))
        await ask(conv, "Как связаны предыдущие вопросы?")
        assert "Вопрос 8" in str(model.requests[0]) and "Вопрос 7" not in str(model.requests[0])
        assert "Вопрос 8" in str(model.requests[1])
        store.close()

    asyncio.run(scenario())


def test_explanation_stream_starts_audio_before_generation_completes():
    async def scenario():
        release, started = asyncio.Event(), asyncio.Event()

        class Streaming(Model):
            async def chat_stream(self, messages, tools=None):
                assert tools == []
                yield ChatChunk(content="Первое предложение. ")
                await release.wait()
                yield ChatChunk(content="Продолжение.", done=True)

        store = MemoryStore(":memory:")
        conv = Conversation(Settings(language="ru-RU"), Streaming(), store)
        task = asyncio.create_task(
            reply_aloud(conv, FakeSpeaker(), "Что такое радуга?", 1, on_start=started.set)
        )
        await asyncio.wait_for(started.wait(), 1)
        assert not task.done()
        release.set()
        assert await task
        store.close()

    asyncio.run(scenario())


def test_pending_followup_requires_acoustic_confidence_and_active_session():
    good = SpeechSignals(True, True, False)
    assert decide(
        "давай посмотри",
        addressed=False,
        active=True,
        signals=good,
        followup_pending=True,
        topic="Плейлист",
    ).accepted
    for active, signals in [
        (False, good),
        (True, SpeechSignals(False, True)),
        (True, SpeechSignals(True, False)),
        (True, SpeechSignals(True, True, True)),
    ]:
        assert not decide(
            "давай посмотри",
            addressed=False,
            active=active,
            signals=signals,
            followup_pending=True,
            topic="Плейлист",
        ).accepted


def test_desktop_upgrade_and_restart_resume_existing_conversation(tmp_path):
    path = tmp_path / "resume.db"
    store = MemoryStore(path)
    identity = store.new_conversation()
    store.add(identity, "user", "Существующий разговор")
    session = store.desktop_session()
    assert store.desktop_conversation() == identity
    store.close()
    store = MemoryStore(path)
    assert store.desktop_session() == session
    assert store.desktop_conversation() == identity
    assert store.transcript(identity)[0]["content"] == "Существующий разговор"
    store.close()


def test_new_conversation_explicitly_clears_old_proposal_and_session_binding():
    async def scenario():
        store = MemoryStore(":memory:")
        conv, playlist, _ = setup(store)
        await ask(conv, "Открой мой любимый плейлист")
        previous = conv.id
        conv.reset()
        assert conv.id != previous and not conv.pending_followup()
        assert store.session_conversation(conv.device.session_id) == conv.id
        assert store.preference("dialogue_pending", str(previous)) is None
        assert "Уточни" in await ask(conv, "давай")
        playlist.assert_not_called()
        store.close()

    asyncio.run(scenario())


def test_pending_read_does_not_bypass_mac_capabilities():
    async def scenario():
        store = MemoryStore(":memory:")
        conv, playlist, _ = setup(
            store, replace(DeviceContext.local_mac(), capabilities=frozenset())
        )
        conv.tools._tools["music_playlist"].required_capability = "mac.media"
        conv.add_exchange("Что дальше?", "Могу посмотреть доступные плейлисты.")
        text = await ask(conv, "давай")
        assert "Нет разрешённого" in text
        playlist.assert_not_called()
        store.close()

    asyncio.run(scenario())


def test_tampered_proposal_cannot_assign_a_playlist():
    async def scenario():
        store = MemoryStore(":memory:")
        conv, playlist, _ = setup(store)
        store.set_preference(
            "dialogue_pending",
            str(conv.id),
            {
                "tool": "music_playlist",
                "arguments": {"action": "assign"},
                "expires": time.time() + 60,
            },
        )
        assert "Уточни" in await ask(conv, "давай")
        playlist.assert_not_called()
        store.close()

    asyncio.run(scenario())


def test_shared_session_new_conversation_propagates_to_other_device():
    async def scenario():
        store = MemoryStore(":memory:")
        device = DeviceContext.local_mac("shared-session")
        mac, _, _ = setup(store, device)
        phone, _, _ = setup(store, replace(device, device_id="phone", device_type="iphone"))
        await ask(mac, "Открой мой любимый плейлист")
        old_id = mac.id
        mac.reset()
        assert "Уточни" in await ask(phone, "давай")
        assert phone.id == mac.id != old_id
        store.close()

    asyncio.run(scenario())


def test_two_voice_turns_keep_same_conversation_and_execute_pending_offer():
    import numpy as np

    from backend.state import State
    from tests.test_voice_loop import Harness

    async def scenario():
        store = MemoryStore(":memory:")
        conv, playlist, _ = setup(store)
        h = Harness(wake=False)
        h.s.language = "ru-RU"
        h.loop.conv, h.loop.speaker = conv, FakeSpeaker()
        identity = conv.id
        for text in ("Открой мой любимый плейлист", "Давай, посмотри"):
            h.loop.state.to(State.THINKING)
            h.loop.task = asyncio.create_task(
                h.loop._bounded_turn(
                    lambda text=text: h.loop.handle(
                        np.zeros(16000, dtype=np.int16), 0, transcript=text
                    )
                )
            )
            await h.loop.task
            h.loop._after_turn(h.loop.task)
            assert conv.id == identity and h.loop.state.state is State.LISTENING
        playlist.assert_awaited_once_with(action="list")
        assert any("Favourite Songs" in sentence for sentence in h.loop.speaker.said)
        assert len(store.conversation_list()) == 1
        store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "answer",
    [
        "Не могу посмотреть доступные плейлисты.",
        "Ты сказала: «Могу посмотреть доступные плейлисты».",
        "Могу посмотреть доступные плейлисты или открыть другой сайт.",
    ],
)
def test_denial_quote_or_ambiguous_alternative_is_not_an_offer(answer):
    store = MemoryStore(":memory:")
    conv, _, _ = setup(store)
    conv.add_exchange("Что дальше?", answer)
    assert conv.pending_followup() is None
    store.close()


def test_unrelated_failed_request_does_not_leave_previous_offer_active():
    from backend.brain.llm import LLMError

    async def scenario():
        store = MemoryStore(":memory:")
        conv, playlist, _ = setup(store)
        await ask(conv, "Открой мой любимый плейлист")

        async def failed(*args, **kwargs):
            raise LLMError("test outage")
            yield

        conv.llm.chat_stream = failed
        with pytest.raises(LLMError):
            await ask(conv, "Объясни другую тему")
        assert not conv.pending_followup()
        assert "Уточни" in await ask(conv, "давай")
        playlist.assert_not_called()
        store.close()

    asyncio.run(scenario())
