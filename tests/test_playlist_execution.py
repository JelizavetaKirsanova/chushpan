"""Regressions from 2026-11-01 22:37–22:39 Tallinn. No real music/BLE/LLM."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.brain.conversation import Conversation
from backend.brain.followups import action_proposal, is_short, safe_followup
from backend.brain.llm import ChatChunk
from backend.config import Settings
from backend.context import DeviceContext, voice_addressed
from backend.led import LEDStrip
from backend.led import parse as led_parse
from backend.media.music_safari import MusicBrowserError, SafariMusicTab, SafariYouTubeMusic
from backend.memory.store import MemoryStore
from backend.music import MusicCommands, message, parse
from backend.tools import build_registry
from backend.tools.media import MediaService
from tests.test_critical_fixes import NoModel, guarded
from tests.test_dialogue_context import ask
from tests.test_music_commands import Player

PLAYLIST = {
    "id": "verified-favourite-id",
    "title": "Favourite Songs",
    "href": "https://music.youtube.com/playlist?list=verified-favourite-id",
    "kind": "playlist",
}
CONFIRMED = {
    "ok": True,
    "state": "playing",
    "track": "Verified Song",
    "artist": "Verified Artist",
    "playlist_id": PLAYLIST["id"],
    "playback_confirmed": True,
    "confirmation": {"source": "test_media_api", "position_before": 1, "position_after": 2},
}


def test_page_wait_retries_only_explicit_no_click_results(monkeypatch):
    async def go():
        app = SafariMusicTab(Settings())
        app.call = AsyncMock(
            side_effect=[MusicBrowserError("play_button_missing"), {"requested": True}]
        )
        monkeypatch.setattr("backend.media.music_safari.asyncio.sleep", AsyncMock())
        await app.start_page("Favourite Songs")
        assert app.call.await_count == 2
        assert app.stages["play_requested"] and app.stages["play_submitted"]
        assert not app.stages["playback_confirmed"]

    asyncio.run(go())


def test_explicit_navigation_foregrounds_only_the_validated_music_tab():
    async def go():
        app = SafariMusicTab(Settings())
        app.target = AsyncMock(return_value={"window": 10, "tab": 2})
        app._run = AsyncMock(return_value=PLAYLIST["href"])
        await app.navigate(PLAYLIST["href"])
        script = app._run.call_args.args[0]
        assert "set t to tab 2 of window id 10" in script
        assert script.index('if URL of t does not start with "https://music.youtube.com/"') < (
            script.index("set current tab of window id 10 to t")
        )
        assert script.index("activate") < script.index("set URL of t")

    asyncio.run(go())


@pytest.mark.parametrize("state,confirmed", [("playing", True), ("paused", False)])
def test_spotify_resume_requires_fresh_api_state(state, confirmed):
    async def go():
        settings = Settings(language="ru-RU")
        settings.media.provider = "spotify"
        player = Player()
        player.state["state"] = state
        player.media_control = AsyncMock(return_value={"ok": True, "action": "resume"})
        result = await MusicCommands(settings, player).execute("Продолжи музыку")
        assert result["ok"] is confirmed and result["playback_confirmed"] is confirmed
        assert ("Играет:" in message(result)) is confirmed

    asyncio.run(go())


def test_status_observes_progress_without_sending_another_play():
    async def go():
        settings = Settings(language="ru-RU")
        store = MemoryStore(":memory:")
        try:
            media, app = service(settings, store)
            app.wait_playing.side_effect = MusicBrowserError("playback_blocked")
            result = await MusicCommands(settings, media).execute("Что сейчас играет?")
            assert not result["playback_confirmed"] and not result["ok"]
            assert "Играет:" not in message(result)
            app.call.assert_not_awaited()
        finally:
            store.close()

    asyncio.run(go())


@pytest.mark.parametrize("code", ["service_unavailable", "tab_changed", "automation_denied"])
def test_page_wait_never_retries_ambiguous_or_denied_write(code):
    async def go():
        app = SafariMusicTab(Settings())
        app.call = AsyncMock(side_effect=MusicBrowserError(code))
        with pytest.raises(MusicBrowserError) as exc:
            await app.start_page("Favourite Songs")
        assert exc.value.code == code
        app.call.assert_awaited_once_with("page_play", title="Favourite Songs")
        assert not app.stages["play_submitted"]

    asyncio.run(go())


def test_queue_source_and_page_identity_can_confirm_advancing_playlist(monkeypatch):
    async def go():
        app = SafariMusicTab(Settings())
        state = {
            **CONFIRMED,
            "playlist_id": None,
            "playlist_title": "Favourite Songs",
            "page_playlist_id": PLAYLIST["id"],
            "ready": True,
        }
        app.now_playing = AsyncMock(
            side_effect=[{**state, "position": 1}, {**state, "position": 2}]
        )
        monkeypatch.setattr("backend.media.music_safari.asyncio.sleep", AsyncMock())
        result = await app.wait_playing(playlist=PLAYLIST["id"], playlist_title="Favourite Songs")
        assert result["playback_confirmed"] and result["playlist_id"] == PLAYLIST["id"]
        assert result["confirmation"]["playlist_source"] == "queue_title_and_page_id"

    asyncio.run(go())


@pytest.mark.parametrize(
    "changes",
    [
        {"playlist_title": "Other Playlist"},
        {"page_playlist_id": "other-id"},
        {"playlist_id": "other-id"},
        {"ready": False},
        {"state": "paused"},
    ],
)
def test_open_playlist_page_is_not_proof_of_matching_playback(changes):
    async def go():
        app = SafariMusicTab(Settings())
        app.timeout = 0.001
        app.now_playing = AsyncMock(
            return_value={
                **CONFIRMED,
                "position": 1,
                "playlist_id": None,
                "playlist_title": "Favourite Songs",
                "page_playlist_id": PLAYLIST["id"],
                "ready": True,
                **changes,
            }
        )
        with pytest.raises(MusicBrowserError):
            await app.wait_playing(playlist=PLAYLIST["id"], playlist_title="Favourite Songs")
        assert not app.stages["playback_confirmed"]

    asyncio.run(go())


@pytest.mark.parametrize(
    "text",
    [
        "Включи Favourite Songs",
        "Включи Favorite Songs",
        "Включи феврит сонгс",
        "Включай мой феврит сонгс тогда.",
        "Чушпан, включи плейлист Favourite Songs",
        "Привет, Чушпан. Включи мне Favourite Songs",
    ],
)
def test_favourite_pronunciations_resolve_explicit_playlist(text):
    intent = parse(text)
    assert intent.action == "play" and intent.kind == "playlist"
    assert intent.query == "Favourite Songs"


def service(settings, store, library=None):
    settings.media.provider = "youtube_music"
    settings.media.youtube_music.main_playlist.title = "Favourite Songs"
    app = SimpleNamespace(
        stages={"play_requested": False, "play_submitted": False, "playback_confirmed": False},
        navigate=AsyncMock(),
        call=AsyncMock(return_value={"requested": True}),
        now_playing=AsyncMock(return_value=CONFIRMED),
        wait_playing=AsyncMock(return_value=CONFIRMED),
    )

    async def start_page(title):
        app.stages["play_requested"] = True
        await app.call("page_play", title=title)
        app.stages["play_submitted"] = True

    app.start_page = start_page
    media = MediaService(settings, store=store)
    media.youtube = SafariYouTubeMusic(settings, store, app=app)
    media.youtube.library = AsyncMock(
        return_value=library or {"playlists": [PLAYLIST], "complete": False}
    )
    media.youtube.search = AsyncMock(side_effect=AssertionError("Own playlists must use library"))
    return media, app


def test_real_dialogue_2237_2239_acceptance_calls_actual_play_adapter_once():
    async def go():
        settings = Settings(language="ru-RU", tools={"enabled": ["media"]})
        store = MemoryStore(":memory:")
        try:
            media, app = service(settings, store)
            reg = build_registry(settings, store=store, media=media)
            conv = Conversation(settings, NoModel(), store, tools=reg)
            # Exact assistant offer from the real transcript. It is a proposal, not proof
            # that the configured playlist exists; the adapter must resolve it afresh.
            conv.add_exchange(
                "Спасибо. А ты можешь мне включить какие-нибудь мои плейлисты? Какие у тебя есть?",
                "Плейлистов нет. Только твой основной — Favourite Songs. "
                "Хочешь включить его или выбрать что-то другое?",
            )
            pending = conv.pending_followup()
            assert pending["tool"] == "music_command"
            token = voice_addressed.set(False)
            try:
                answer = await ask(conv, "Давай, включай.")
            finally:
                voice_addressed.reset(token)
            assert "Играет:" in answer and "Verified Song" in answer
            app.call.assert_awaited_once_with("page_play", title="Favourite Songs")
            app.navigate.assert_awaited_once_with(PLAYLIST["href"])
            app.wait_playing.assert_awaited_once_with("", "", PLAYLIST["id"], "Favourite Songs")
            again = await ask(conv, "Включай мой феврит сонгс тогда.")
            assert "Играет:" in again
            assert app.call.await_count == 1
            results = [
                json.loads(m["content"])
                for m in store.recent(conv.id, 30)
                if m.get("tool_name") == "music_command"
            ]
            assert results[0]["execution_status"] == "confirmed"
            assert results[0]["execution_events"] == ["requested", "submitted", "confirmed"]
            assert not conv.pending_followup()
        finally:
            store.close()

    asyncio.run(go())


def test_favourite_and_my_favourite_resolve_live_library_without_catalog_search():
    async def go():
        settings = Settings(language="ru-RU")
        store = MemoryStore(":memory:")
        try:
            media, app = service(settings, store)
            controller = MusicCommands(settings, media)
            result = await controller.execute("Включи мой любимый плейлист")
            assert result["ok"] and result["playback_confirmed"]
            media.youtube.library.assert_awaited_once()
            media.youtube.search.assert_not_awaited()
            app.navigate.assert_awaited_once_with(PLAYLIST["href"])
        finally:
            store.close()

    asyncio.run(go())


def test_configured_title_is_not_evidence_of_existing_playlist():
    async def go():
        settings = Settings(language="ru-RU")
        store = MemoryStore(":memory:")
        try:
            media, app = service(settings, store, {"playlists": [], "complete": False})
            result = await MusicCommands(settings, media).execute("Включи Favourite Songs")
            assert not result["ok"] and result["execution_status"] == "failed"
            assert result["execution_events"] == ["requested", "failed"]
            assert "не удалось найти" in message(result)
            app.call.assert_not_awaited()
            app.wait_playing.assert_not_awaited()
        finally:
            store.close()

    asyncio.run(go())


@pytest.mark.parametrize(
    "result",
    [
        {"ok": True, "state": "playing", "track": "Old Song", "message": "Включил!"},
        {"ok": True, "action": "opened", "message": "Включил!"},
        {"message": "Включил. Favourite Songs играет."},
    ],
)
def test_no_confirmation_never_allows_success_text(result):
    async def go():
        settings = Settings(language="ru-RU")
        settings.media.provider = "youtube_music"
        player = Player()
        player.play_music = AsyncMock(return_value=result)
        response = await MusicCommands(settings, player).execute("Включи Favourite Songs")
        assert response["ok"] is False and response["execution_status"] == "failed"
        assert "Включил" not in message(response)
        assert "подтвержд" in message(response)

    asyncio.run(go())


def test_submitted_but_blocked_is_recorded_as_failure_and_not_replayed():
    async def go():
        settings = Settings(language="ru-RU", tools={"enabled": ["media"]})
        store = MemoryStore(":memory:")
        try:
            media, app = service(settings, store)
            app.wait_playing.side_effect = MusicBrowserError("autoplay_blocked")
            conv = Conversation(
                settings, NoModel(), store, tools=build_registry(settings, store=store, media=media)
            )
            answer = await ask(conv, "Включи Favourite Songs")
            assert "Safari заблокировал" in answer and "Играет" not in answer
            result = next(
                json.loads(m["content"])
                for m in store.recent(conv.id, 20)
                if m.get("tool_name") == "music_command"
            )
            assert result["execution_events"] == ["requested", "submitted", "failed"]
            assert result["code"] == "autoplay_blocked"
            assert result["playback_confirmed"] is False
        finally:
            store.close()

    asyncio.run(go())


def test_permission_error_is_specific_and_preserved_in_history():
    async def go():
        settings = Settings(language="ru-RU", tools={"enabled": ["media"]})
        store = MemoryStore(":memory:")
        try:
            media, app = service(settings, store)
            media.youtube.library.side_effect = MusicBrowserError("automation_unknown")
            conv = Conversation(
                settings, NoModel(), store, tools=build_registry(settings, store=store, media=media)
            )
            answer = await ask(conv, "Включи Favourite Songs")
            assert "Apple Events" in answer and "не определён" in answer
            assert app.call.await_count == 0
            assert any(
                '"automation_unknown"' in m["content"]
                for m in store.recent(conv.id, 20)
                if m["role"] == "tool"
            )
        finally:
            store.close()

    asyncio.run(go())


def test_partial_empty_library_result_is_answered_without_llm_fiction():
    class ListModel:
        async def chat_stream(self, *args, **kwargs):
            yield ChatChunk(
                tool_calls=[
                    {"function": {"name": "music_playlist", "arguments": {"action": "list"}}}
                ],
                done=True,
            )

    async def go():
        settings = Settings(language="ru-RU", tools={"enabled": ["media"]})
        store = MemoryStore(":memory:")
        try:
            media, _ = service(settings, store, {"playlists": [], "complete": False})
            conv = Conversation(
                settings,
                ListModel(),
                store,
                tools=build_registry(settings, store=store, media=media),
            )
            answer = await ask(conv, "Какие у меня есть плейлисты?")
            assert "доступной части библиотеки" in answer
            assert "Favourite Songs" not in answer and "Плейлистов нет" not in answer
        finally:
            store.close()

    asyncio.run(go())


@pytest.mark.parametrize(
    "text",
    [
        "Сделай мне светлую розовую ленту",
        "Сделай мне светло-розовую ленту",
    ],
)
def test_light_pink_adjective_forms_have_identical_safe_intent(text):
    assert led_parse(text) == {"action": "color", "rgb": [255, 16, 32]}

    async def go():
        settings = Settings(language="ru-RU", led={"enabled": True})
        strip = LEDStrip(settings)
        strip.execute = AsyncMock(
            return_value={"message": "Команда отправлена; результат не подтверждён."}
        )
        store = MemoryStore(":memory:")
        try:
            conv = Conversation(settings, NoModel(), store, tools=guarded(strip.tool()))
            assert "Команда отправлена" in await ask(conv, text)
            strip.execute.assert_awaited_once_with(action="color", rgb=[255, 16, 32])
            assert not conv.tools.confirmations.pending
        finally:
            store.close()

    asyncio.run(go())


def test_last_saved_fact_uses_confirmed_sqlite_result_and_survives_restart(tmp_path):
    path = tmp_path / "history.db"
    device = DeviceContext.local_mac("remember-session")
    fact = "Любимый цвет пользователя — розовый."

    class RememberModel:
        async def chat_stream(self, *args, **kwargs):
            yield ChatChunk(
                tool_calls=[
                    {
                        "function": {
                            "name": "remember",
                            "arguments": {"fact": fact, "category": "preference"},
                        }
                    }
                ],
                done=True,
            )

    async def save():
        settings = Settings(language="ru-RU", tools={"enabled": ["memory"]})
        store = MemoryStore(path)
        try:
            conv = Conversation(
                settings,
                RememberModel(),
                store,
                tools=build_registry(settings, store=store),
                device=device,
            )
            assert "подтверждение" in await ask(
                conv, "Чушпан, запомни, что мой любимый цвет розовый"
            )
            code = next(iter(conv.tools.confirmations.pending.values())).code
            assert "Запомнил" in await ask(conv, "Подтверждаю " + code)
            conv.llm = NoModel()
            assert fact in await ask(conv, "Что ты только что запомнил?")
        finally:
            store.close()

    asyncio.run(save())

    async def after():
        settings = Settings(language="ru-RU", tools={"enabled": ["memory"]})
        store = MemoryStore(path)
        try:
            conv = Conversation(
                settings,
                NoModel(),
                store,
                tools=build_registry(settings, store=store),
                device=device,
            )
            assert fact in await ask(conv, "Что-то только что запомнил.")
            other = replace(device, session_id="other-session")
            separate = Conversation(
                settings,
                NoModel(),
                store,
                tools=build_registry(settings, store=store),
                device=other,
            )
            assert fact not in await ask(separate, "Что ты только что запомнил?")
        finally:
            store.close()

    asyncio.run(after())


def test_pending_memory_request_is_never_reported_as_saved():
    async def go():
        settings = Settings(language="ru-RU", tools={"enabled": ["memory"]})
        store = MemoryStore(":memory:")
        try:
            conv = Conversation(
                settings, NoModel(), store, tools=build_registry(settings, store=store)
            )
            conv._persist(
                "Запомни факт",
                [
                    {
                        "role": "tool",
                        "tool_name": "remember",
                        "content": '{"status":"confirmation_required"}',
                    }
                ],
                "Ожидаю подтверждения.",
            )
            assert "ещё не подтверждено" in await ask(conv, "Что ты только что запомнил?")
            assert store.facts() == []
        finally:
            store.close()

    asyncio.run(go())


def test_music_offer_cannot_assign_playlist_or_bypass_device_permissions():
    assert is_short("Давай, включай.")
    assert safe_followup("music_command", {"command": "Включи плейлист Favourite Songs"})
    assert not safe_followup("music_command", {"command": "Включи музыку"})
    assert not safe_followup("music_playlist", {"action": "assign", "playlist_id": PLAYLIST["id"]})
    assert (
        action_proposal("Могу включить Favourite Songs или открыть Safari.", {"music_command"})
        is None
    )

    async def go():
        settings = Settings(language="ru-RU", tools={"enabled": ["media"]})
        store = MemoryStore(":memory:")
        try:
            media, app = service(settings, store)
            device = replace(DeviceContext.local_mac(), capabilities=frozenset())
            conv = Conversation(
                settings,
                NoModel(),
                store,
                tools=build_registry(settings, store=store, media=media),
                device=device,
            )
            conv.add_exchange("Что включить?", "Могу включить плейлист Favourite Songs.")
            assert "Нет разрешённого" in await ask(conv, "Давай, включай")
            app.call.assert_not_awaited()
        finally:
            store.close()

    asyncio.run(go())
