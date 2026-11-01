import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend import night
from backend.config import Settings
from backend.context import DeviceContext, RequestContext, request_scope
from backend.mac_agent import LocalMacAgent
from backend.memory.store import MemoryStore
from backend.night import BetterDisplay, NightRoutine, night_requested
from backend.shortcuts import ShortcutsService
from backend.tools.registry import ToolError, ToolRegistry

INTERNAL = "00000000-0000-0000-0000-000000000001"
EXTERNAL = "00000000-0000-0000-0000-000000000002"


def routine(tmp_path):
    s = Settings(night={"external_uuid": EXTERNAL}, youtube_video={"night_autoplay": False})
    service = NightRoutine(s, MemoryStore(tmp_path / "t.db"))
    service.display.inventory = AsyncMock(
        return_value=[{"uuid": INTERNAL, "builtin": True}, {"uuid": EXTERNAL, "builtin": False}]
    )
    service.display.read = AsyncMock(return_value=0.8)
    service.display.set = AsyncMock(return_value={"previous": 0.8, "actual": 0.1})
    service.shortcuts.verify_sleep = AsyncMock()
    service.shortcuts.start_sleep = AsyncMock(return_value={"pid": None})
    service.preflight.check = AsyncMock(return_value={"ok": True})
    service.volume = AsyncMock(return_value={"ok": True, "volume_percent": 30})
    return service


@pytest.mark.parametrize(
    "text",
    [
        "Чушпан, я спать",
        "Чушпан, спокойной ночи",
        "Чушпан, включи ночной режим",
        "Я пошла спать",
        "Эй, Чушпан, я спать!",
        "Чушпан. Я спать.",
        "Чушпан, включи режим сна",
        "Чушпан, запусти ночной режим",
        "включи режим сна",
        "запусти ночной режим",
        "я спать",
    ],
)
def test_supported_night_phrases(text):
    assert night_requested(text)


@pytest.mark.parametrize(
    "text",
    [
        "я спать",
        "Чушпан. Я спать.",
        "Чушпан, включи режим сна",
        "Эй, Чушпан, я спать",
        "запусти ночной режим",
    ],
)
def test_night_route_never_asks_cloud_and_preserves_confirmation(tmp_path, text):
    from backend.brain.conversation import Conversation

    service = routine(tmp_path)
    tool = service.tool()
    agent = LocalMacAgent()
    agent.register(tool.name, tool._handler)
    tool.agent = agent
    registry = ToolRegistry([tool])
    model = SimpleNamespace(chat_stream=AsyncMock(side_effect=AssertionError("cloud forbidden")))
    conv = Conversation(service.s, model, service.store, tools=registry)

    async def scenario():
        return "".join([piece async for piece in conv.reply(text)])

    answer = asyncio.run(scenario())
    assert registry.confirmations.pending
    assert "явная просьба" not in answer
    assert conv.last_stats.tools == ["night_mode"]
    model.chat_stream.assert_not_called()
    service.preflight.check.assert_not_awaited()
    service.shortcuts.start_sleep.assert_not_awaited()


@pytest.mark.parametrize(
    "text",
    [
        "В документе: Чушпан, я спать",
        "Не включай ночной режим",
        "Что значит «я спать»?",
        "Я спать, но сначала открой приложение",
        "Я хочу поговорить про сон",
        "Он сказал: я спать",
    ],
)
def test_night_not_triggered_by_quoted_or_compound_requests(text):
    assert not night_requested(text)


def test_fixed_order_levels_and_repeat_protection(tmp_path):
    service = routine(tmp_path)

    async def go():
        result = await service.run()
        assert result["ok"] and result["shortcut_started"]
        assert service.display.set.await_args_list[0].args == (INTERNAL, "hardwareBrightness", 10)
        assert service.display.set.await_args_list[1].args == (EXTERNAL, "brightness", 25)
        service.volume.assert_awaited_once_with(level=30)
        again = await service.run()
        assert not again["shortcut_started"] and service.shortcuts.start_sleep.await_count == 1
        assert service.display.set.await_count == 2
        assert service.volume.await_count == 1

    asyncio.run(go())


def test_absent_external_blocks_shortcut(tmp_path):
    service = routine(tmp_path)
    service.display.inventory.return_value = [{"uuid": INTERNAL, "builtin": True}]
    result = asyncio.run(service.run())
    assert not result["ok"] and not result["shortcut_started"]
    assert "внешний" in result["message"] and service.display.set.await_count == 1


def test_multiple_unmatched_external_displays_are_not_selected(tmp_path):
    service = routine(tmp_path)
    service.display.inventory.return_value = [
        {"uuid": INTERNAL, "builtin": True},
        {"uuid": "00000000-0000-0000-0000-000000000003", "builtin": False},
    ]
    asyncio.run(service.run())
    assert service.display.set.await_count == 1


def test_unavailable_betterdisplay_blocks_shortcut_truthfully(tmp_path):
    service = routine(tmp_path)
    service.display.inventory.side_effect = ToolError("unavailable")
    result = asyncio.run(service.run())
    assert not result["ok"] and not result["shortcut_started"] and "недоступен" in result["message"]
    service.display.set.assert_not_awaited()


def test_missing_shortcut_does_not_claim_success_or_retry_launch(tmp_path):
    service = routine(tmp_path)
    service.shortcuts.verify_sleep.side_effect = ToolError("missing")
    result = asyncio.run(service.run())
    assert not result["ok"] and not result["shortcut_started"]
    service.shortcuts.start_sleep.assert_not_awaited()


def test_partial_brightness_failure_keeps_previous_value_and_never_repeats(tmp_path):
    service = routine(tmp_path)
    service.display.set.side_effect = [ToolError("unknown"), {"previous": 0.8, "actual": 0.25}]

    async def go():
        result = await service.run()
        assert not result["ok"] and result["steps"]["builtin"]["previous"] == 0.8
        await service.run()
        assert service.display.set.await_count == 2

    asyncio.run(go())


def test_unknown_shortcut_launch_is_not_retried(tmp_path):
    service = routine(tmp_path)
    service.shortcuts.start_sleep.side_effect = ToolError("timeout")

    async def go():
        assert not (await service.run())["ok"]
        await service.run()
        assert service.shortcuts.start_sleep.await_count == 1

    asyncio.run(go())


def test_entire_scenario_requires_device_bound_single_use_confirmation(tmp_path):
    service = routine(tmp_path)
    service.s.youtube_video.night_autoplay = True
    service.video = SimpleNamespace(
        pid=lambda: "PLSuZQXqwb2Uw",
        play=AsyncMock(
            return_value={"playing": True, "video_id": "aaaaaaaaaaa", "message": "Играет."}
        ),
        verify_for_sleep=AsyncMock(),
    )
    tool = service.tool()
    agent = LocalMacAgent()
    tool.agent = agent
    agent.register(tool.name, tool._handler)
    reg = ToolRegistry([tool])
    owner = DeviceContext.local_mac()

    async def go():
        with request_scope(RequestContext(owner, "Чушпан, я спать")):
            result = json.loads(await reg.call("night_mode", {}))
        assert result["status"] == "confirmation_required"
        assert result["message"].index("непросмотренное") < result["message"].index("10%")
        assert result["message"].index("30%") < result["message"].index("последней")
        service.video.play.assert_not_awaited()
        service.volume.assert_not_awaited()
        service.display.set.assert_not_awaited()
        service.video.play.assert_not_awaited()
        service.volume.assert_not_awaited()
        code = next(iter(reg.confirmations.pending.values())).code
        await reg.confirmations.respond(
            RequestContext(replace(owner, session_id="alien"), "Подтверждаю " + code),
            reg.call,
            reg.direct_reply,
        )
        service.display.set.assert_not_awaited()
        answer = await reg.confirmations.respond(
            RequestContext(owner, "Подтверждаю " + code), reg.call, reg.direct_reply
        )
        assert "10%" in answer and "25%" in answer
        service.video.play.assert_awaited_once_with(
            source="night", unwatched=True, allow_repeat=False
        )
        service.volume.assert_awaited_once_with(level=30)
        await reg.confirmations.respond(RequestContext(owner, "Подтверждаю " + code), reg.call)
        assert service.shortcuts.start_sleep.await_count == 1

    asyncio.run(go())


def test_unauthenticated_or_shortcuts_capability_missing_does_not_run(tmp_path):
    service = routine(tmp_path)
    service.s.youtube_video.night_autoplay = True
    service.video = SimpleNamespace(pid=lambda: "PLSuZQXqwb2Uw")
    tool = service.tool()
    tool.agent = LocalMacAgent()
    owner = DeviceContext.local_mac()
    for device in [
        replace(owner, authenticated=False),
        replace(owner, capabilities=owner.capabilities - {"mac.shortcuts"}),
        replace(owner, capabilities=owner.capabilities - {"mac.media"}),
    ]:
        with request_scope(RequestContext(device, "я спать")):
            assert "error" in json.loads(asyncio.run(ToolRegistry([tool]).call("night_mode", {})))
    service.display.set.assert_not_awaited()


def test_betterdisplay_uses_uuid_fixed_argv_and_verifies_value(monkeypatch):
    command = AsyncMock(side_effect=["0.8", "", "0.10"])
    monkeypatch.setattr(night, "display_command", command)
    display = BetterDisplay(SimpleNamespace())
    result = asyncio.run(display.set(INTERNAL, "hardwareBrightness", 10))
    assert result["previous"] == 0.8 and result["actual"] == 0.1
    assert command.await_args_list[1].args == (
        "set",
        "-UUID=" + INTERNAL,
        "-hardwareBrightness=0.10",
    )


def test_betterdisplay_inventory_matches_cg_builtin_and_ignores_groups(monkeypatch):
    raw = (
        json.dumps({"UUID": INTERNAL, "deviceType": "Display", "name": "Builtin"})
        + ","
        + json.dumps({"deviceType": "DisplayGroup"})
    )
    monkeypatch.setattr(night, "display_command", AsyncMock(return_value=raw))
    bridge = SimpleNamespace(
        request=AsyncMock(return_value={"displays": [{"uuid": INTERNAL, "builtin": True}]})
    )
    result = asyncio.run(BetterDisplay(SimpleNamespace(bridge=bridge)).inventory())
    assert len(result) == 1 and result[0]["builtin"]


def test_background_sleep_port_cannot_bypass_confirmed_agent(monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(ToolError):
        asyncio.run(ShortcutsService(Settings()).start_sleep())
    spawn.assert_not_awaited()


def test_sqlite_failure_before_action_runs_nothing(tmp_path):
    service = routine(tmp_path)
    service.store.close()
    with pytest.raises(ToolError, match="Журнал"):
        asyncio.run(service.run())
    service.display.set.assert_not_awaited()
    service.shortcuts.start_sleep.assert_not_awaited()


def test_concurrent_instances_use_sqlite_lease_not_just_python_lock(tmp_path):
    first = routine(tmp_path)
    second = routine(tmp_path)

    async def go():
        entered = asyncio.Event()
        release = asyncio.Event()
        original = first.display.inventory.return_value

        async def inventory():
            entered.set()
            await release.wait()
            return original

        first.display.inventory.side_effect = inventory
        task = asyncio.create_task(first.run())
        await entered.wait()
        result = await second.run()
        assert "уже выполняется" in result["message"]
        second.display.set.assert_not_awaited()
        release.set()
        assert (await task)["ok"]
        assert first.shortcuts.start_sleep.await_count == 1

    asyncio.run(go())


def test_voice_uses_same_confirmation_and_never_needs_model(tmp_path):
    from backend.brain.conversation import Conversation
    from backend.voice import reply_aloud

    service = routine(tmp_path)
    tool = service.tool()
    agent = LocalMacAgent()
    tool.agent = agent
    agent.register(tool.name, tool._handler)
    reg = ToolRegistry([tool])

    class Model:
        async def chat_stream(self, *args, **kwargs):
            raise AssertionError("Night must be deterministic")
            yield

    class Speaker:
        def __init__(self):
            self.text = []

        async def speak(self, sentences, on_start=None):
            async for sentence in sentences:
                self.text.append(sentence)

    conv = Conversation(Settings(language="ru-RU"), Model(), service.store, tools=reg)
    speaker = Speaker()

    async def go():
        await reply_aloud(conv, speaker, "Чушпан, я спать", 20)
        service.display.set.assert_not_awaited()
        code = next(iter(reg.confirmations.pending.values())).code
        await reply_aloud(conv, speaker, "Подтверждаю " + code, 20)
        assert service.shortcuts.start_sleep.await_count == 1
        assert any("10%" in text for text in speaker.text)

    asyncio.run(go())


def test_background_launch_uses_fixed_reviewed_uuid_and_sanitized_env(monkeypatch):
    from backend.mac_agent import authorized_operation

    s = Settings(
        shortcuts={
            "allowed": {
                "Youtube sleep": {
                    "identifier": INTERNAL,
                    "description": "Тест",
                    "input_type": "none",
                }
            }
        }
    )
    service = ShortcutsService(s)
    service.verify_sleep = AsyncMock()
    proc = SimpleNamespace(pid=12345, wait=AsyncMock(return_value=0))
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-pass-to-shortcut")
    c = RequestContext(DeviceContext.local_mac(), "я спать")

    async def go():
        with request_scope(c):
            token = authorized_operation.set(("night_mode", {}, c.request_id))
            try:
                assert (await service.start_sleep())["started"]
            finally:
                authorized_operation.reset(token)

    asyncio.run(go())
    assert spawn.await_args.args == ("/usr/bin/shortcuts", "run", INTERNAL)
    assert "GEMINI_API_KEY" not in spawn.await_args.kwargs["env"]
    assert spawn.await_args.kwargs["start_new_session"] is True


def test_display_cli_pty_reader_is_bounded_and_has_no_api_key_env(monkeypatch):
    proc = SimpleNamespace(returncode=0, wait=AsyncMock())
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(night.os, "read", lambda fd, n: next(chunks))
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-pass-to-display")
    chunks = iter([b"0.10\r\n", b""])
    assert asyncio.run(night.display_command("get", "-hardwareBrightness")) == "0.10"
    assert spawn.await_args.args[0] == night.APP
    assert "GEMINI_API_KEY" not in spawn.await_args.kwargs["env"]
    chunks = iter([b"x" * 65537])
    with pytest.raises(ToolError, match="слишком большой"):
        asyncio.run(night.display_command("get", "-identifiers"))


def test_vendor_requires_uppercase_uuid_even_if_config_was_normalized(monkeypatch):
    command = AsyncMock(return_value="0.25")
    monkeypatch.setattr(night, "display_command", command)
    uid = "4f1704fb-5a9b-458d-9173-f5ce3727cf9c"
    assert asyncio.run(BetterDisplay(SimpleNamespace()).read(uid, "brightness")) == 0.25
    assert command.await_args.args == ("get", "-UUID=" + uid.upper(), "-brightness")


def test_video_brightness_volume_then_single_sleep_watcher(tmp_path):
    service = routine(tmp_path)
    service.s.youtube_video.night_autoplay = True
    service.video = SimpleNamespace(
        play=AsyncMock(
            return_value={
                "playing": True,
                "video_id": "aaaaaaaaaaa",
                "message": "Играет выбранное видео.",
            }
        )
    )
    order = []

    async def play(**kwargs):
        order.append("video")
        assert kwargs == {"source": "night", "unwatched": True, "allow_repeat": False}
        return {"playing": True, "video_id": "aaaaaaaaaaa", "message": "Играет."}

    async def watch():
        order.append("watcher")
        return {"pid": None}

    async def brightness(uuid, control, level):
        order.append("builtin" if uuid == INTERNAL else "external")
        assert (control, level) == (
            ("hardwareBrightness", 10) if uuid == INTERNAL else ("brightness", 25)
        )
        return {"previous": 0.8, "actual": level / 100}

    async def volume(**kwargs):
        order.append("volume")
        assert kwargs == {"level": 30}
        return {"ok": True, "volume_percent": 30}

    service.video.verify_for_sleep = AsyncMock()
    service.video.play.side_effect = play
    service.display.set.side_effect = brightness
    service.volume.side_effect = volume
    service.shortcuts.start_sleep.side_effect = watch

    async def run():
        assert (await service.run())["ok"]
        assert (await service.run())["ok"]

    asyncio.run(run())
    assert order == ["video", "builtin", "external", "volume", "watcher"]
    assert service.display.set.await_count == 2
    service.volume.assert_awaited_once_with(level=30)


def test_unverified_video_does_not_start_sleep_watcher_or_retry(tmp_path):
    service = routine(tmp_path)
    service.s.youtube_video.night_autoplay = True
    service.video = SimpleNamespace(
        play=AsyncMock(return_value={"playing": False, "message": "autoplay blocked"})
    )

    async def run():
        for _ in range(2):
            out = await service.run()
            assert not out["ok"] and not out["shortcut_started"]

    asyncio.run(run())
    assert service.video.play.await_count == 1
    service.shortcuts.start_sleep.assert_not_awaited()
    service.display.set.assert_not_awaited()
    service.volume.assert_not_awaited()


def test_enabling_video_never_replaces_a_prior_sleep_watchers_video(tmp_path):
    service = routine(tmp_path)
    asyncio.run(service.run())
    service.s.youtube_video.night_autoplay = True
    service.video = SimpleNamespace(play=AsyncMock())
    out = asyncio.run(service.run())
    assert not out["ok"]
    service.video.play.assert_not_awaited()
    assert service.shortcuts.start_sleep.await_count == 1


@pytest.mark.parametrize(
    "failure", [ToolError("fixed HDMI volume"), {"ok": True, "volume_percent": 50}]
)
def test_volume_failure_is_partial_and_never_retried(tmp_path, failure):
    service = routine(tmp_path)
    if isinstance(failure, Exception):
        service.volume.side_effect = failure
    else:
        service.volume.return_value = failure

    async def run():
        out = await service.run()
        assert not out["ok"] and not out["shortcut_started"]
        assert out["steps"]["volume"]["status"] == "unknown"
        assert "30% не подтверждён" in out["message"]
        assert not (await service.run())["ok"]

    asyncio.run(run())
    service.volume.assert_awaited_once_with(level=30)
    service.shortcuts.start_sleep.assert_not_awaited()


def test_missing_tv_stops_video_night_before_any_effects(tmp_path):
    service = routine(tmp_path)
    service.s.youtube_video.night_autoplay = True
    service.display.inventory.return_value = [{"uuid": INTERNAL, "builtin": True}]
    service.video = SimpleNamespace(play=AsyncMock())
    out = asyncio.run(service.run())
    assert not out["ok"] and not out["shortcut_started"]
    assert "внешний экран" in out["message"]
    service.video.play.assert_not_awaited()
    service.display.set.assert_not_awaited()
    service.volume.assert_not_awaited()
    service.shortcuts.start_sleep.assert_not_awaited()


def test_changed_front_tab_after_settings_blocks_sleep_without_replaying_video(tmp_path):
    service = routine(tmp_path)
    service.s.youtube_video.night_autoplay = True
    service.video = SimpleNamespace(
        play=AsyncMock(
            return_value={"playing": True, "video_id": "aaaaaaaaaaa", "message": "Играет."}
        ),
        verify_for_sleep=AsyncMock(side_effect=ToolError("Вкладка сменилась")),
    )

    async def run():
        for _ in range(2):
            out = await service.run()
            assert not out["ok"] and not out["shortcut_started"]

    asyncio.run(run())
    service.video.play.assert_awaited_once()
    service.volume.assert_awaited_once_with(level=30)
    service.shortcuts.start_sleep.assert_not_awaited()


def test_old_watcher_journal_never_changes_volume_after_shortcut_started(tmp_path):
    service = routine(tmp_path)
    service.store.set_preference(
        "night",
        "last_run",
        {"started": night.time.time(), "steps": {"shortcut": {"status": "done"}}},
    )
    out = asyncio.run(service.run())
    assert not out["ok"] and not out["shortcut_started"]
    assert "старый запуск" in out["message"]
    service.display.set.assert_not_awaited()
    service.volume.assert_not_awaited()
    service.shortcuts.start_sleep.assert_not_awaited()
