"""Only in-memory settings/store and fake players; no app, network or audio access."""

import asyncio

import pytest

from backend.addressee import SpeechSignals, decide
from backend.brain.conversation import Conversation
from backend.config import Settings
from backend.context import voice_addressed, voice_music_command
from backend.hud.bus import HudBus
from backend.memory.store import MemoryStore
from backend.music import (
    MusicCommands,
    bind_settings,
    classify_error,
    load_provider,
    message,
    music_tool,
    parse,
)
from backend.tools import ToolRegistry
from backend.tools.registry import ToolError


class Player:
    def __init__(self):
        self.calls = []
        self.state = {"state": "paused", "track": "Hello", "artist": "Adele"}
        self._last_control_target = "music"

    async def play_music(self, query="", **kwargs):
        self.calls.append(("play", query, kwargs))
        self.state["state"] = "playing"
        return {"ok": True, **self.state, "playback_confirmed": self.state["state"] == "playing"}

    async def now_playing(self):
        return dict(self.state)

    async def media_control(self, action, target="music"):
        self.calls.append((action, target))
        self.state["state"] = "paused" if action == "pause" else "playing"
        if action == "next":
            self.state["track"] = "Next song"
        return {"ok": True, **self.state, "playback_confirmed": self.state["state"] == "playing"}

    async def media_volume(self, direction):
        self.calls.append(("volume", direction))
        return {"ok": True, "volume": 20}


@pytest.mark.parametrize(
    "command,action",
    [
        ("Чушпан, включи музыку", "play"),
        ("Включи что-нибудь спокойное", "calm"),
        ("Поставь Нервы", "play"),
        ("Включи последнюю песню", "resume"),
        ("Поставь мой плейлист", "playlist"),
        ("Следующий трек", "next"),
        ("Пауза", "pause"),
        ("Продолжай", "resume"),
        ("Сделай потише", "quieter"),
        ("Что сейчас играет?", "status"),
    ],
)
def test_natural_commands_have_deterministic_routes(command, action):
    assert parse(command).action == action
    assert parse("Обсуждаем, как включить музыку") is None


def test_search_keeps_original_names_and_uses_artist_or_song_kind():
    async def scenario():
        s = Settings()
        s.media.provider = "youtube_music"
        player = Player()
        controller = MusicCommands(s, player)
        await controller.execute("Чушпан, поставь Нервы")
        await controller.execute("Включи трек Blinding Lights")
        assert player.calls[0] == ("play", "Нервы", {"kind": "artist"})
        assert player.calls[1] == ("play", "Blinding Lights", {"kind": "song"})

    asyncio.run(scenario())


def test_controls_verify_state_and_volume_without_restarting_music():
    async def scenario():
        s, player = Settings(), Player()
        s.media.provider = "youtube_music"
        controller = MusicCommands(s, player)
        for command in ("Включи музыку", "Пауза", "Продолжай", "Следующий трек", "Сделай потише"):
            result = await controller.execute(command)
            assert result["ok"]
            assert "не подтверждён" not in message(result)
        assert sum(call[0] == "play" for call in player.calls) == 1
        assert (await controller.execute("Что сейчас играет?"))["track"] == "Next song"

    asyncio.run(scenario())


def test_duplicate_play_is_serialized_and_deduplicated_and_pause_resumes_same_track():
    async def scenario():
        s, player = Settings(), Player()
        s.media.provider = "youtube_music"
        controller = MusicCommands(s, player)
        results = await asyncio.gather(*(controller.execute("Включи музыку") for _ in range(3)))
        assert all(result["ok"] for result in results)
        assert sum(call[0] == "play" for call in player.calls) == 1
        assert results[-1]["deduplicated"]
        await controller.execute("Пауза")
        await controller.execute("Включи музыку")
        assert sum(call[0] == "play" for call in player.calls) == 1
        assert player.calls[-1] == ("resume", "music")

    asyncio.run(scenario())


def test_open_page_is_not_playback_and_uncertain_launch_is_not_repeated():
    async def scenario():
        s, player = Settings(), Player()
        s.media.provider = "youtube_music"
        player.state = {"state": "unknown"}

        async def opened(query, **kwargs):
            player.calls.append(("opened", query))
            return {"ok": True, "action": "opened"}

        player.play_music = opened
        controller = MusicCommands(s, player)
        for _ in range(2):
            result = await controller.execute("Включи музыку")
            assert not result["ok"] and result["code"] == "playback_unconfirmed"
        assert len(player.calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error,code",
    [
        ("Spotify is not installed", "app_not_installed"),
        ("Войдите в аккаунт", "authorization_required"),
        ("Accessibility denied", "permission_denied"),
        ("Screen Recording", "permission_denied"),
        ("Automation -1743", "permission_denied"),
        ("Трек не найден", "track_not_found"),
        ("Каталог недоступен", "catalog_unavailable"),
        ("Воспроизведение не подтверждено", "playback_unconfirmed"),
        ("Unsupported command", "unsupported"),
        ("network timeout", "service_unavailable"),
    ],
)
def test_error_categories_are_safe_and_specific(error, code):
    assert classify_error(ToolError(error))[0] == code


def test_no_provider_disabled_provider_and_apple_music_never_fall_back():
    async def scenario():
        s, player = Settings(), Player()
        controller = MusicCommands(s, player)
        s.media.provider = None
        assert (await controller.execute("Включи музыку"))["code"] == "provider_required"
        s.media.provider = "youtube_music"
        s.media.youtube_music.enabled = False
        assert (await controller.execute("Включи музыку"))["code"] == "service_disabled"
        assert (await controller.execute("Включи музыку в Apple Music"))["code"] == "unsupported"
        assert parse("Расскажи про Apple Music") is None
        assert (await controller.execute("Включи музыку через Spotify"))[
            "code"
        ] == "provider_mismatch"
        assert player.calls == []

    asyncio.run(scenario())


def test_local_music_pipeline_does_not_call_gemini_or_tts_normalizer():
    class ForbiddenLLM:
        async def chat_stream(self, *args, **kwargs):
            raise AssertionError("no Gemini for music commands")
            yield

    async def scenario():
        s, store, player = Settings(language="ru-RU"), MemoryStore(":memory:"), Player()
        s.media.provider = "youtube_music"
        registry = ToolRegistry([music_tool(s, player)])
        conversation = Conversation(s, ForbiddenLLM(), store, tools=registry)
        try:
            for command in ("Чушпан, включи трек Blinding Lights", "Пауза", "Продолжай"):
                answer = "".join([part async for part in conversation.reply(command)])
                assert "не подтверждён" not in answer
            assert player.calls[0][1] == "Blinding Lights"
            assert store.transcript(conversation.id)[0]["content"].endswith("Blinding Lights")
        finally:
            store.close()

    asyncio.run(scenario())


def test_music_context_grant_does_not_relax_other_system_permissions():
    decision = decide(
        "Пауза",
        addressed=False,
        active=True,
        topic="Включи музыку",
        signals=SpeechSignals(True, True),
    )
    assert decision.accepted and decision.kind == "music"
    assert not decide(
        "Пауза", addressed=False, active=True, topic="Python", signals=SpeechSignals(True, True)
    ).accepted

    async def scenario():
        s, player, store = Settings(language="ru-RU"), Player(), MemoryStore(":memory:")
        s.media.provider = "youtube_music"
        registry = ToolRegistry([music_tool(s, player)])
        conv = Conversation(s, None, store, tools=registry)
        token = voice_addressed.set(False)
        grant = voice_music_command.set("Пауза")
        try:
            assert "паузе" in "".join([part async for part in conv.reply("Пауза")])
            # The grant is bound to this exact command, not every future media call.
            result = "".join([part async for part in conv.reply("Включи музыку")])
            assert "явное обращение" in result
            assert not any(call[0] == "play" for call in player.calls)
        finally:
            voice_addressed.reset(token)
            voice_music_command.reset(grant)
            store.close()

    asyncio.run(scenario())


def test_provider_choice_is_saved_without_changing_current_service_until_restart():
    store, s, bus = MemoryStore(":memory:"), Settings(), HudBus()
    s.media.provider = "youtube_music"
    try:
        bind_settings(bus, s, store)
        bus.request("music_settings", "spotify")
        assert s.media.provider == "youtube_music"
        assert store.preference("media", "provider") == {"provider": "spotify"}
        load_provider(s, store)
        assert s.media.provider == "spotify"
        bus.request("music_settings", "unselected")
        load_provider(s, store)
        assert s.media.provider is None
        bus.request("music_settings", "apple_music")
        assert s.media.provider is None
    finally:
        store.close()


def test_no_internal_playlist_instructions_or_false_success_in_direct_reply():
    result = {
        "status": "choose",
        "playlists": [{"title": "Спокойное"}],
        "message": "Попроси выбрать плейлист; не выбирай сама.",
    }
    assert message(result) == "Выбери плейлист или композицию: Спокойное."
    assert "не найдена" in message({"status": "not_found"})


def test_production_registry_and_mac_agent_dispatch_to_existing_spotify_mock():
    from backend.tools import build_registry
    from backend.tools.media import MediaService
    from tests.test_media import FakeSpotify

    class ForbiddenLLM:
        async def chat_stream(self, *args, **kwargs):
            raise AssertionError("local route must not call a model")
            yield

    async def scenario():
        s, store = Settings(language="ru-RU"), MemoryStore(":memory:")
        s.media.provider = "spotify"
        spotify = FakeSpotify()
        media = MediaService(s, spotify, store=store)
        registry = build_registry(s, store=store, media=media)
        try:
            assert registry._tools["music_command"].required_capability == "mac.media"
            conv = Conversation(s, ForbiddenLLM(), store, tools=registry)
            answer = "".join([part async for part in conv.reply("Чушпан, включи трек Maniac")])
            assert "Играет" in answer
            assert spotify.searched == ["Maniac"]
        finally:
            if media.ducker._tasks:
                await asyncio.gather(*media.ducker._tasks)
            store.close()

    asyncio.run(scenario())


def test_music_selection_is_saved_via_real_local_websocket_without_playback(tmp_path):
    from fastapi.testclient import TestClient

    from backend.hud.server import create_app

    store, s, bus = MemoryStore(":memory:"), Settings(), HudBus()
    s.media.provider = "youtube_music"
    bind_settings(bus, s, store)
    try:
        with TestClient(create_app(bus, {"http://localhost:9100"}, tmp_path)) as client:
            with client.websocket_connect("/ws", headers={"origin": "http://localhost:9100"}) as ws:
                while ws.receive_json()["type"] != "transcript":
                    pass
                ws.send_json({"type": "music_settings", "value": "spotify"})
                event = ws.receive_json()
                assert event["saved"] == "spotify" and event["restart_required"]
                assert event["current"] == "youtube_music"
                ws.send_json({"type": "music_settings", "value": "apple_music"})
                assert ws.receive_json()["error"]
        assert store.preference("media", "provider") == {"provider": "spotify"}
    finally:
        store.close()
