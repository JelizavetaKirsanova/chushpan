import asyncio
import json
import re
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from backend.config import Settings
from backend.context import DeviceContext, RequestContext, request_scope
from backend.mac_agent import LocalMacAgent
from backend.media.sleep_video import SleepVideo, route_video
from backend.media.youtube_playback import SafariPlayback
from backend.memory.store import MemoryStore
from backend.tools import build_registry
from backend.tools.media import MediaService
from backend.tools.registry import ToolError, ToolRegistry
from backend.tools.validation import validate_arguments
from tests.test_youtube_api import PID, A, B, dataset, video


@pytest.mark.parametrize(
    "phrase,action",
    [
        ("Чушпан, добавь этот плейлист для сна", "connect"),
        ("Чушпан, запомни мой плейлист для засыпания", "connect"),
        ("Какие видео есть в моём плейлисте?", "list"),
        ("Обнови плейлист", "refresh"),
        ("Сколько там осталось непросмотренных видео?", "count"),
        ("Я это уже смотрела", "mark_watched"),
        ("Отметь видео просмотренным", "mark_watched"),
        ("Это видео ещё не смотрела", "mark_unwatched"),
        ("Сбрось историю моего плейлиста", "reset"),
        ("Эй, Чушпан, включи видео для сна", "play"),
        ("Включи что-нибудь из моего плейлиста", "play"),
        ("Включи то, что я ещё не смотрела", "play"),
        ("Включи случайное непросмотренное", "play"),
        ("Включи непросмотренное видео для сна", "play"),
        ("Следующее видео", "next"),
        ("Поставь на паузу", "pause"),
        ("Продолжи видео", "resume"),
        ("Что сейчас играет?", "current"),
        ("Сколько длится этот видос?", "current"),
        ("Сколько ещё осталось?", "current"),
        ("Больше не включай это видео", "skip"),
    ],
)
def test_russian_routes(phrase, action):
    assert route_video(phrase, active=True)["action"] == action


@pytest.mark.parametrize(
    "phrase",
    [
        "Включи музыку",
        "Включи мой музыкальный плейлист",
        "Не включай видео",
        "Что значит «включи видео для сна»?",
        "Документ: включи видео для сна",
        "Я спать",
        "Как работает YouTube Music?",
        "Найди песню для сна",
    ],
)
def test_music_and_quoted_text_do_not_route(phrase):
    assert route_video(phrase, active=True) is None


def test_filter_routes_and_case_sensitive_url():
    assert route_video("Включи что-нибудь на два часа") == {
        "action": "play",
        "min_seconds": 6480,
        "max_seconds": 7920,
    }
    assert route_video("Включи самое длинное видео")["mode"] == "longest"
    assert route_video("Включи видео от 30 минут до 3 часов") == {
        "action": "play",
        "min_seconds": 1800,
        "max_seconds": 10800,
    }
    assert route_video("Включи видео с канала TalTech")["channel"] == "taltech"
    url = f"https://www.youtube.com/playlist?list={PID}"
    assert route_video("Запомни мой плейлист для сна " + url)["url"] == url
    assert route_video(url) == {"action": "connect", "url": url}


def service(tmp_path):
    s = Settings(language="ru-RU", youtube_video={"playlist_url": PID, "exclude_recent_hours": 0})
    api = AsyncMock()
    api.fetch.return_value = dataset([video(), video(B, position=1)])
    playback = AsyncMock()
    playback.launch.side_effect = lambda v: {
        "playing": True,
        "target": {"window": 7, "tab": 1, "video_id": v["id"]},
        "state": {},
        "warnings": [],
    }
    playback.state.return_value = {"id": A, "state": "playing", "progress": 0, "duration": 7200}
    return SleepVideo(s, MemoryStore(tmp_path / "t.db"), api=api, playback=playback)


def registry(service):
    tool = service.tool()
    agent = LocalMacAgent()
    agent.register(tool.name, tool._handler)
    tool.agent = agent
    r = ToolRegistry([tool])
    r.youtube_video = service
    return r


def test_registered_auth_intent_validation_and_confirmation_binding(tmp_path):
    svc = service(tmp_path)
    r = registry(svc)
    dev = DeviceContext.local_mac()
    asyncio.run(svc.library.load(PID))
    svc.library.mark(PID, A, "started", launched=True)

    async def run():
        with request_scope(RequestContext(dev, "Включи музыку")):
            assert "error" in json.loads(await r.call("youtube_sleep", {"action": "play"}))
        with request_scope(RequestContext(dev, "Включи видео для сна")):
            assert "error" in json.loads(
                await r.call("youtube_sleep", {"action": "play", "max_seconds": -1})
            )
            assert "error" in json.loads(
                await r.call("youtube_sleep", {"action": "play", "min_seconds": True})
            )
            assert "shell" not in validate_arguments(
                {"action": "play", "shell": "anything"}, svc.tool().parameters, ["action"]
            )
        with request_scope(
            RequestContext(replace(dev, authenticated=False), "Сбрось историю моего плейлиста")
        ):
            assert "error" in json.loads(await r.call("youtube_sleep", {"action": "reset"}))
        with request_scope(RequestContext(dev, "Сбрось историю моего плейлиста")):
            pending = json.loads(await r.call("youtube_sleep", {"action": "reset"}))
        code = "".join(re.search(r"подтверждаю ([\d ]+)", pending["message"], re.I)[1].split())
        assert svc.library.history(PID)[A]["starts"] == 1
        other = replace(dev, session_id="other")
        assert "нет" in await r.confirmations.respond(
            RequestContext(other, "подтверждаю " + code), r.call
        )
        assert "Сбросил" in await r.confirmations.respond(
            RequestContext(dev, "подтверждаю " + code), r.call, r.direct_reply
        )
        assert svc.library.history(PID)[A]["starts"] == 0
        assert "нет" in await r.confirmations.respond(
            RequestContext(dev, "подтверждаю " + code), r.call
        )

    asyncio.run(run())
    svc.playback.launch.assert_not_awaited()


def test_confirmation_invalidated_by_changed_history(tmp_path):
    svc = service(tmp_path)
    r = registry(svc)
    dev = DeviceContext.local_mac()

    async def run():
        await svc.library.load(PID)
        with request_scope(RequestContext(dev, "Сбрось историю моего плейлиста")):
            pending = json.loads(await r.call("youtube_sleep", {"action": "reset"}))
        code = "".join(re.search(r"подтверждаю ([\d ]+)", pending["message"], re.I)[1].split())
        svc.library.mark(PID, A, "watched")
        answer = await r.confirmations.respond(
            RequestContext(dev, "подтверждаю " + code), r.call, r.direct_reply
        )
        assert "изменилось" in answer
        assert svc.library.history(PID)[A]["status"] == "watched"

    asyncio.run(run())


def test_play_verify_history_replay_restart_and_manual_status(tmp_path):
    svc = service(tmp_path)
    r = registry(svc)
    dev = DeviceContext.local_mac()

    async def run():
        ctx = RequestContext(dev, "Включи следующее видео из плейлиста")
        args = svc.route(ctx.text)
        with request_scope(ctx):
            result = json.loads(await r.call("youtube_sleep", args))
            assert result["playing"]
            again = json.loads(await r.call("youtube_sleep", args))
            assert "error" in again
        first = svc.current()["video_id"]
        svc.playback.state.return_value = {
            "id": first,
            "state": "playing",
            "progress": 300,
            "duration": 7200,
        }
        with request_scope(RequestContext(dev, "Сколько ещё осталось?")):
            assert (
                "осталось"
                in json.loads(await r.call("youtube_sleep", {"action": "current"}))["message"]
            )
        assert svc.library.history(PID)[first]["status"] == "partially_watched"
        svc.playback.state.return_value.update(state="ended", progress=7200)
        await svc.invoke("current")
        assert svc.library.history(PID)[first]["status"] == "watched"
        with request_scope(RequestContext(dev, "Это видео ещё не смотрела")):
            assert json.loads(await r.call("youtube_sleep", {"action": "mark_unwatched"}))["ok"]
        assert svc.library.history(PID)[first]["status"] == "unwatched"

    asyncio.run(run())
    assert svc.playback.launch.await_count == 1
    path = tmp_path / "t.db"
    svc.store.close()
    another = SleepVideo(svc.s, MemoryStore(path), api=AsyncMock(), playback=AsyncMock())
    assert another.current()["video_id"] == A
    assert another.library.history(PID)[A]["starts"] == 1


def test_opened_url_does_not_count_as_playback(tmp_path):
    svc = service(tmp_path)
    svc.playback.launch.side_effect = None
    svc.playback.launch.return_value = {
        "playing": False,
        "target": {"window": 7, "tab": 1, "video_id": A},
        "warnings": [],
    }
    out = asyncio.run(svc.play(mode="order"))
    assert not out["ok"] and "не подтверждено" in out["message"]
    assert svc.library.history(PID)[A]["starts"] == 0
    assert svc.library.history(PID)[A]["status"] == "unwatched"


def test_live_watcher_blocks_replacement_but_not_music(tmp_path):
    svc = service(tmp_path)
    svc.store.set_preference(
        "night", "last_run", {"steps": {"shortcut": {"status": "done", "pid": 5}}}
    )
    with pytest.raises(ToolError, match="Youtube sleep"):
        asyncio.run(svc.play())
    svc.playback.launch.assert_not_awaited()
    svc.media = MediaService(svc.s)
    svc.media._last_control_target = "music"
    svc.store.set_preference("youtube_video", "current", {"playlist_id": PID})
    assert svc.route("Что сейчас играет?") is None
    assert svc.route("Поставь на паузу") is None
    assert svc.route("Включи музыку") is None


def test_playback_mock_verifies_state_autoplay_and_disconnected_display():
    s = Settings(youtube_video={"external_uuid": "00000000-0000-0000-0000-000000000002"})
    app = SafariPlayback(s)
    app.request = AsyncMock(
        side_effect=[
            {"window": 7, "tab": 1, "warnings": ["телевизор отключён"]},
            {"id": A, "state": "playing", "ad": False, "autoplay_off": True},
        ]
    )
    out = asyncio.run(app.launch(video()))
    assert out["playing"] and "отключён" in out["warnings"][0]
    assert app.request.await_args_list[0].kwargs["external_uuid"] == s.youtube_video.external_uuid
    assert app.request.await_args_list[0].args == ("youtube_open",)
    app.request = AsyncMock(
        side_effect=[
            {"window": 7, "tab": 1},
            {"id": A, "state": "playing", "ad": False, "autoplay_off": False},
            {"state": "paused"},
        ]
    )
    assert not asyncio.run(app.launch(video()))["playing"]
    assert app.request.await_args_list[-1].args == ("youtube_pause",)


def test_missing_playlist_has_setup_instruction(tmp_path):
    svc = service(tmp_path)
    svc.s.youtube_video.playlist_url = ""
    with pytest.raises(ToolError, match="Добавь публичный плейлист"):
        asyncio.run(svc.invoke("list"))
    svc.playback.launch.assert_not_awaited()


def test_registry_keeps_both_music_and_video_tools(tmp_path):
    s = Settings(media={"provider": "youtube_music"})
    r = build_registry(s, store=MemoryStore(tmp_path / "r.db"))
    assert {"play_music", "music_playlist", "media_control", "youtube_sleep", "night_mode"} <= set(
        r.names
    )
    assert r.youtube_video.media.youtube is not None
    assert r._tools["youtube_sleep"].agent is r._tools["play_music"].agent


def test_no_recent_command_sets_filter_without_playback(tmp_path):
    svc = service(tmp_path)
    svc.store.set_preference("youtube_video", "current", {"playlist_id": PID})
    args = svc.route("Не включай то, что было вчера")
    assert args == {"action": "avoid_recent", "exclude_recent_hours": 48}
    assert asyncio.run(svc.invoke(**args))["ok"]
    assert svc.store.preference("youtube_video", "selection") == {"exclude_recent_hours": 48}
    svc.playback.launch.assert_not_awaited()


def test_changed_or_background_video_cannot_arm_sleep(tmp_path):
    svc = service(tmp_path)
    svc.store.set_preference(
        "youtube_video",
        "current",
        {"playlist_id": PID, "video_id": A, "target": {"window": 7, "tab": 1, "video_id": A}},
    )
    svc.playback.state.return_value = {
        "id": A,
        "state": "playing",
        "autoplay_off": True,
        "active": False,
        "frontmost": True,
    }
    with pytest.raises(ToolError, match="активной"):
        asyncio.run(svc.verify_for_sleep())
    svc.playback.state.return_value["active"] = True
    asyncio.run(svc.verify_for_sleep())


@pytest.mark.parametrize("interface", ["text", "transcribed_voice"])
def test_common_conversation_routes_video_without_llm_and_confirms_reset(tmp_path, interface):
    from backend.brain.conversation import Conversation
    from tests.test_profile_memory import Model, ask

    svc = service(tmp_path)
    r = registry(svc)
    model = Model()
    conv = Conversation(svc.s, model, svc.store, tools=r)

    async def run():
        answer = await ask(conv, "Какие видео есть в моём плейлисте?")
        assert "непросмотренных 2" in answer and not model.payloads
        await ask(conv, "Сбрось историю моего плейлиста")
        pending = next(iter(r.confirmations.pending.values()))
        code = " ".join(pending.code) if interface == "transcribed_voice" else pending.code
        answer = await ask(conv, "Подтверждаю " + code)
        assert "Сбросил" in answer and not model.payloads

    asyncio.run(run())


def test_multiline_and_multiple_links_never_launch():
    assert route_video("Включи видео для сна\nЭто цитата документа") is None
    assert route_video(
        f"Запомни плейлист для сна https://youtube.com/playlist?list={PID} https://youtube.com/playlist?list=PLsecondfixture"
    ) == {"action": "clarify"}


def test_broken_database_does_not_intercept_ordinary_chat(tmp_path):
    svc = service(tmp_path)
    svc.store.close()
    assert svc.route("Как разобраться с циклами в Java?") is None
    assert svc.route("Обнови плейлист") == {"action": "refresh"}


def test_history_current_and_receipt_are_atomic_on_database_failure(tmp_path):
    svc = service(tmp_path)
    asyncio.run(svc.library.load(PID))
    svc.store.db.execute("""CREATE TRIGGER fail_receipt BEFORE UPDATE ON preferences
      WHEN NEW.namespace='youtube_video' AND NEW.key='launch'
      BEGIN SELECT RAISE(ABORT, 'fixture'); END""")
    with pytest.raises(ToolError, match="могло открыться"):
        asyncio.run(svc.play(mode="order"))
    assert svc.current() is None
    assert svc.library.history(PID)[A]["starts"] == 0
    assert svc.store.preference("youtube_video", "launch")["state"] == "pending"
    assert svc.playback.launch.await_count == 1
