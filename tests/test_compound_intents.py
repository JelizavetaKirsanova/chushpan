"""Production conversation/registry/LED/Mac/music functions; only external transports mocked."""

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bleak.exc import BleakError
from pydantic import ValidationError

from backend.context import (
    RequestContext,
    planned_operation,
    request_scope,
    voice_addressed,
    voice_control_command,
)
from backend.intents import ControlPlan, ControlSession, compile_plan
from backend.led import LEDState
from backend.mac import MacDesktop
from backend.mac_agent import LocalMacAgent
from backend.music import music_tool
from backend.state import State
from backend.tools.mac import mac_tools
from backend.tools.registry import ToolError
from tests.test_dialogue_context import ask
from tests.test_led import rig as rig
from tests.test_music_commands import Player
from tests.test_smart_home import conversation
from tests.test_voice_loop import custom_harness
from tests.test_voice_reliability import SPEECH

CASES = [
    ("Чушпан, сделай ленту светло-розовой и яркость на сто процентов", "#FF1020", 100),
    ("Сделай ленту розовой на максимум", "#FF0020", 100),
    ("Вруби светло-розовый на сто процентов", "#FF1020", 100),
    ("Ленту светло-розовую, яркость сотка", "#FF1020", 100),
    ("Хочу нежно-розовую подсветку на полную", "#FF1020", 100),
    ("Включи LED-ленту на 100%, цвет светло-розовый", "#FF1020", 100),
    ("Яркость на пятьдесят процентов, ленту розовую", "#FF0020", 50),
    ("Сделай лет ленту светло розовой наполовину", "#FF1020", 50),
    ("Включи лед ленту и поставь розовый и яркость тридцать пять процентов", "#FF0020", 35),
]


def setup(rig, monkeypatch):
    conv, store = conversation(rig)
    desktop = MacDesktop(rig.settings)

    async def request(operation, **kwargs):
        assert operation == "desktop_brightness"
        return {"level": kwargs["level"]}

    desktop.bridge = SimpleNamespace(request=AsyncMock(side_effect=request))
    brightness = next(
        tool for tool in mac_tools(rig.settings, desktop) if tool.name == "set_brightness"
    )
    agent = LocalMacAgent()
    agent.register(brightness.name, brightness._handler)
    brightness.agent = agent
    conv.tools.add(brightness)
    player = Player()
    conv.tools.add(music_tool(rig.settings, player))
    from backend.tools import system as system_module

    opened = AsyncMock(return_value=(0, ""))
    monkeypatch.setattr(system_module, "run", opened)
    website = next(
        tool for tool in system_module.system_tools(rig.settings) if tool.name == "open_website"
    )
    conv.tools.add(website)
    return conv, store, desktop.bridge.request, player, opened


@pytest.mark.parametrize("text,color,brightness", CASES)
def test_phrases_use_one_typed_device_action_and_no_model(rig, text, color, brightness):
    async def run():
        plan = compile_plan(text)
        assert len(plan.actions) == 1
        params = plan.actions[0].parameters
        assert (params.color, params.brightness, params.power) == (color, brightness, True)
        conv, store = conversation(rig)
        try:
            answer = await ask(conv, text)
            assert "яркость" in answer and "Физическое выполнение не подтверждено" in answer
            assert len(rig.clients) == 3
            assert rig.clients[-1].write_gatt_char.await_args.args[1][3] == brightness
            tools = [r for r in store.recent(conv.id, 20) if r["role"] == "tool"]
            assert len(tools) == 1 and tools[0]["tool_name"] == "led_control"
            assert json.loads(tools[0]["content"])["delivery_state"] == "sent"
            assert store.recent(conv.id, 20)[0]["content"] == text
        finally:
            store.close()

    asyncio.run(run())


def test_relative_level_uses_existing_requested_brightness_under_lock(rig):
    async def run():
        conv, store = conversation(rig)
        try:
            answer = await ask(conv, "Поставь розовый и сделай поярче")
            assert "яркость 60%" in answer
            assert rig.strip._load().brightness == 60
        finally:
            store.close()

    asyncio.run(run())


def test_subjective_level_requires_a_preference_or_question_before_any_write(rig):
    async def run():
        conv, store = conversation(rig)
        text = "Сделай мне розовый свет, только не слишком яркий"
        try:
            assert "в процентах" in await ask(conv, text)
            rig.factory.assert_not_called()
            rig.settings.led.comfortable_brightness = 37
            rig.strip.cfg = rig.strip.cfg.model_copy(update={"comfortable_brightness": 37})
            assert "яркость 37%" in await ask(conv, text)
            assert rig.strip._load().brightness == 37
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "text,level,site",
    [
        ("Убавь яркость экрана до 20%, включи тёплый свет и открой YouTube", 20, True),
        ("Выключи подсветку и поставь Mac на минимальную яркость", 0, False),
    ],
)
def test_multiple_existing_integrations_share_the_original_request(
    rig, monkeypatch, text, level, site
):
    async def run():
        conv, store, screen, player, opened = setup(rig, monkeypatch)
        try:
            answer = await ask(conv, text)
            screen.assert_awaited_once_with("desktop_brightness", level=level)
            assert opened.await_count == int(site)
            assert f"Яркость {level} процентов" in answer
            assert not player.calls
            assert len(rig.clients) >= 1
            assert "Физическое выполнение не подтверждено" in answer
            assert not any(
                "Всё выполнено" in message["content"] for message in store.recent(conv.id, 20)
            )
        finally:
            store.close()

    asyncio.run(run())


def test_unavailable_tv_never_changes_mac_volume_and_music_still_runs(rig, monkeypatch):
    async def run():
        conv, store, screen, player, opened = setup(rig, monkeypatch)
        try:
            answer = await ask(conv, "Сделай ленту розовой, телевизор потише, а музыку включи")
            assert "телевизора не подключено" in answer
            assert player.calls and "Играет:" in answer
            screen.assert_not_awaited()
            opened.assert_not_awaited()
            assert len(rig.clients) == 1
            assert conv.controls.device is None  # Multiple devices cannot bind a short correction.
            assert "Для какого устройства" in await ask(conv, "Нет, лучше на 80")
        finally:
            store.close()

    asyncio.run(run())


def test_failure_of_one_device_preserves_independent_results_and_history(rig, monkeypatch):
    async def run():
        conv, store, screen, player, opened = setup(rig, monkeypatch)
        screen.side_effect = ToolError("Экран не поддерживает изменение яркости")
        try:
            answer = await ask(
                conv, "Убавь яркость экрана до 20%, включи тёплый свет и открой YouTube"
            )
            assert "Экран не поддерживает" in answer and "Bluetooth" in answer
            opened.assert_awaited_once()
            history = store.recent(conv.id, 30)
            assert any("error" in json.loads(r["content"]) for r in history if r["role"] == "tool")
            assert [r["tool_name"] for r in history if r["role"] == "tool"] == [
                "set_brightness",
                "led_control",
                "open_website",
            ]
        finally:
            store.close()

    asyncio.run(run())


def test_failed_bluetooth_batch_stops_that_device_but_not_other_devices(rig, monkeypatch):
    async def run():
        conv, store, screen, player, opened = setup(rig, monkeypatch)
        create = rig.factory.side_effect

        def broken(*args, **kwargs):
            client = create(*args, **kwargs)
            if len(rig.clients) == 2:
                client.write_gatt_char.side_effect = BleakError("gone")
            return client

        rig.factory.side_effect = broken
        try:
            answer = await ask(conv, "Сделай ленту розовой на максимум и открой YouTube")
            assert "Завершённых записей: 1" in answer
            assert len(rig.clients) == 2
            opened.assert_awaited_once()
            tool = next(r for r in store.recent(conv.id, 20) if r.get("tool_name") == "led_control")
            result = json.loads(tool["content"])
            assert result["delivery_state"] == "partial" and result["skipped_commands"]
            assert not result["physical_confirmed"]
        finally:
            store.close()

    asyncio.run(run())


def test_unsupported_firmware_mode_prevents_all_writes_of_the_led_transaction(rig):
    async def run():
        conv, store = conversation(rig)
        rig.strip._save(LEDState(identity=rig.strip.identity, confirmed={"rgb", "on"}))
        try:
            answer = await ask(conv, "Сделай ленту розовой на максимум")
            assert "непроверенный режим" in answer
            rig.factory.assert_not_called()
        finally:
            store.close()

    asyncio.run(run())


def test_context_correction_and_second_utterance_only_change_brightness(rig):
    async def run():
        conv, store = conversation(rig)
        try:
            await ask(conv, "Сделай ленту розовой на 50%")
            count = len(rig.clients)
            assert "80%" in await ask(conv, "Нет, лучше на 80")
            assert len(rig.clients) == count + 1
            assert rig.clients[-1].write_gatt_char.await_args.args[1][2:4] == bytes([1, 80])
            await ask(conv, "Сделай светло-розовой")
            assert "100%" in await ask(conv, "И яркость на максимум")
            assert rig.strip._load().brightness == 100
        finally:
            store.close()

    asyncio.run(run())


def test_context_expires_is_session_bound_and_clears_after_ambiguity(rig):
    async def run():
        conv, store = conversation(rig)
        now = [0.0]
        conv.controls = ControlSession(ttl_s=20, clock=lambda: now[0])
        try:
            await ask(conv, "Сделай ленту розовой на 50%")
            count = len(rig.clients)
            now[0] = 21
            assert "Для какого устройства" in await ask(conv, "Нет, лучше на 80")
            assert len(rig.clients) == count
            await ask(conv, "Сделай ленту розовой")
            conv.device = replace(conv.device, session_id="another-trusted-session")
            assert "Для какого устройства" in await ask(conv, "И яркость на максимум")
            await ask(conv, "Сделай ленту розовой")
            await ask(conv, "Сделай ленту красной и синей")
            assert "Для какого устройства" in await ask(conv, "Нет, лучше на 80")
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "text",
    [
        "Сделай ленту розовой и синей",
        "Включи ленту и выключи ленту",
        "Сделай ленту розовой на 30% и на 80%",
        "Выключи ленту и поставь розовый",
        "Сделай ленту розовой и удали все файлы",
        "Сделай ленту розовой и включи стробоскоп",
        "Сделай ленту розовой на 101%",
        "Сделай ленту розовой на минус пять процентов",
        "Сделай ленту розовой, а яркость не меняй",
        "Сделай ленту розовой или выключи Mac",
    ],
)
def test_conflicts_unknown_parameters_and_dangerous_compounds_never_partially_execute(rig, text):
    async def run():
        conv, store = conversation(rig)
        try:
            await ask(conv, text)
            rig.factory.assert_not_called()
        finally:
            store.close()

    asyncio.run(run())


def test_plan_schema_disallows_invented_devices_tools_parameters_and_numeric_booleans():
    for action in [
        {"device": "oven", "parameters": {"power": True}},
        {"device": "led_strip", "parameters": {"effect": "strobe"}},
        {"device": "led_strip", "tool": "run_shortcut", "parameters": {"power": True}},
        {"device": "led_strip", "parameters": {"brightness": True}},
        {"device": "led_strip", "parameters": {"brightness": 101}},
        {"device": "led_strip", "parameters": {}},
    ]:
        with pytest.raises(ValidationError):
            ControlPlan.model_validate({"actions": [action]})


def test_plan_is_revalidated_against_original_and_remote_capabilities(rig):
    async def run():
        conv, store = conversation(rig)
        try:
            text = "Сделай ленту розовой на максимум"
            plan = compile_plan(text)
            with request_scope(RequestContext(conv.device, "Сделай ленту синей на максимум")):
                result = await conv.controls.execute(plan, conv)
            assert "error" in result
            rig.factory.assert_not_called()
            conv.device = replace(conv.device, capabilities=frozenset())
            assert "Mac Agent" in await ask(conv, text)
            rig.factory.assert_not_called()
        finally:
            store.close()

    asyncio.run(run())


def test_contextual_plan_grant_does_not_authorize_an_unrelated_or_dangerous_action(rig):
    async def run():
        conv, store = conversation(rig)
        try:
            context = RequestContext(conv.device, "Сделай ленту розовой на максимум")
            plan = compile_plan(context.text)
            addressed = voice_addressed.set(False)
            try:
                with request_scope(context):
                    assert "error" in await conv.controls.execute(plan, conv)
                    grant = voice_control_command.set(context.text)
                    try:
                        assert (await conv.controls.execute(plan, conv))["status"] == "sent"
                    finally:
                        voice_control_command.reset(grant)
                    denied = json.loads(
                        await conv.tools.call("led_control", {"command": "Выключи ленту"})
                    )
                    assert "error" in denied
            finally:
                voice_addressed.reset(addressed)
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("fail", [False, True])
def test_duplicate_request_id_never_replays_a_plan_even_after_a_partial_failure(rig, fail):
    async def run():
        conv, store = conversation(rig)
        if fail:
            create = rig.factory.side_effect

            def broken(*args, **kwargs):
                client = create(*args, **kwargs)
                if len(rig.clients) == 2:
                    client.write_gatt_char.side_effect = BleakError("disconnected")
                return client

            rig.factory.side_effect = broken
        try:
            text = "Сделай ленту розовой на 50%"
            context = RequestContext(conv.device, text, request_id="stable-audio-event")
            plan = compile_plan(text)
            with request_scope(context):
                first = await conv.controls.execute(plan, conv)
                count = len(rig.clients)
                second = await conv.controls.execute(plan, conv)
            assert second["replayed"] and len(rig.clients) == count
            with request_scope(RequestContext(conv.device, text)):
                third = await conv.controls.execute(plan, conv)
            assert not third.get("replayed") and len(rig.clients) == count + 3
            assert first["steps"][0]["result"]["written"] is (not fail)
        finally:
            store.close()

    asyncio.run(run())


def test_device_lock_keeps_two_batches_contiguous(rig):
    async def run():
        rig.strip._save(
            LEDState(identity=rig.strip.identity, confirmed={"rgb", "on", "brightness"})
        )
        create = rig.factory.side_effect
        writes = []

        def tracked(*args, **kwargs):
            client = create(*args, **kwargs)

            async def write(char, packet, response):
                writes.append(packet)
                await asyncio.sleep(0.001)

            client.write_gatt_char.side_effect = write
            return client

        rig.factory.side_effect = tracked
        await asyncio.gather(
            rig.strip.command("Сделай ленту розовой на 20%"),
            rig.strip.command("Сделай ленту синей на 80%"),
        )
        assert len(writes) == 6
        assert [writes[2][3], writes[5][3]] == [20, 80]
        assert writes[1][4:7] == bytes([255, 0, 32]) and writes[4][4:7] == bytes([0, 0, 255])

    asyncio.run(run())


def test_cancelled_transaction_is_not_replayed_and_releases_device_lock(rig):
    async def run():
        conv, store = conversation(rig)
        started = asyncio.Event()
        create = rig.factory.side_effect

        def hang(*args, **kwargs):
            client = create(*args, **kwargs)
            if len(rig.clients) == 1:

                async def wait(*args, **kwargs):
                    started.set()
                    await asyncio.Event().wait()

                client.write_gatt_char.side_effect = wait
            return client

        rig.factory.side_effect = hang
        context = RequestContext(conv.device, "Сделай ленту розовой на максимум")
        plan = compile_plan(context.text)
        conv.controls.device = "mac_display"
        conv.controls.expires = conv.controls.clock() + 60
        conv.controls.binding = (context.binding, conv.id)
        try:
            with request_scope(context):
                task = asyncio.create_task(conv.controls.execute(plan, conv))
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                receipt = await conv.controls.execute(plan, conv)
            assert receipt["status"] == "unverified" and len(rig.clients) == 1
            assert conv.controls.device is None
            journal = rig.strip._load().last_transaction
            assert journal["steps"][0]["delivery_state"] == "unverified"
            assert journal["steps"][1]["delivery_state"] == "not_started"
            assert "отменено" in receipt["message"]
            assert "отправлена" in await ask(conv, "Выключи ленту")
            assert len(rig.clients) == 2
        finally:
            store.close()

    asyncio.run(run())


def test_repeated_stt_event_is_dropped_but_a_fresh_identical_request_is_accepted():
    async def run():
        h = custom_harness("Сделай ленту розовой на 50%")
        h.loop._wake_up(None)
        calls = []

        async def reply(text):
            calls.append(text)
            yield "Команда отправлена."

        h.loop.conv.reply = reply
        stamp = time.perf_counter()
        for event in [stamp, stamp, stamp + 0.5]:
            h.loop.state.to(State.THINKING)
            h.loop.task = asyncio.create_task(
                h.loop._bounded_turn(
                    lambda event=event: h.loop.handle(SPEECH, event, wake_validated=True)
                )
            )
            await h.loop.task
            h.loop.state.to(State.LISTENING)
        assert len(calls) == 2
        h.loop._close_session()

    asyncio.run(run())


@pytest.mark.parametrize(
    "text",
    [
        "Чушпан, запомни, что мой любимый цвет розовый",
        "Сделай заметку, что мой любимый цвет розовый",
        "Поставь таймер на пять минут, название цвет розовый",
        "Почему подсветка иногда выглядит розовой?",
    ],
)
def test_descriptive_color_does_not_hijack_memory_notes_timers_or_questions(text):
    assert compile_plan(text) is None


def test_subjective_screen_request_does_not_let_the_model_invent_a_percentage(rig, monkeypatch):
    async def run():
        conv, store, screen, player, opened = setup(rig, monkeypatch)
        try:
            assert "процент" in await ask(conv, "Уменьши яркость Mac немного")
            screen.assert_not_awaited()
            rig.factory.assert_not_called()
        finally:
            store.close()

    asyncio.run(run())


def test_existing_unqualified_mac_brightness_route_is_preserved(rig, monkeypatch):
    async def run():
        conv, store, screen, player, opened = setup(rig, monkeypatch)
        try:
            assert "Яркость 25 процентов" in await ask(conv, "Установи яркость 25%")
            screen.assert_awaited_once_with("desktop_brightness", level=25)
            rig.factory.assert_not_called()
        finally:
            store.close()

    asyncio.run(run())


def test_music_never_claims_playback_when_the_api_cannot_confirm_it(rig, monkeypatch):
    async def run():
        conv, store, screen, player, opened = setup(rig, monkeypatch)
        player.play_music = AsyncMock(
            return_value={"ok": True, "state": "paused", "track": "Hello"}
        )
        try:
            answer = await ask(conv, "Сделай ленту розовой и включи музыку")
            assert "Играет:" not in answer and "не подтвержден" in answer
            assert "Физическое выполнение не подтверждено" in answer
            assert len(rig.clients) == 1
        finally:
            store.close()

    asyncio.run(run())


def test_plan_scope_cannot_bypass_dangerous_action_confirmation(rig):
    async def run():
        conv, store = conversation(rig)
        desktop = MacDesktop(rig.settings)
        power = next(tool for tool in mac_tools(rig.settings, desktop) if tool.name == "mac_power")
        handler = AsyncMock(side_effect=AssertionError("must not run a real power action"))
        agent = LocalMacAgent()
        agent.register(power.name, handler)
        power.agent = agent
        conv.tools.add(power)
        context = RequestContext(conv.device, "Выключи Mac")
        try:
            with request_scope(context):
                grant = planned_operation.set(
                    (
                        context.request_id,
                        "mac_power",
                        json.dumps({"action": "shutdown"}, sort_keys=True),
                    )
                )
                try:
                    result = json.loads(await conv.tools.call("mac_power", {"action": "shutdown"}))
                finally:
                    planned_operation.reset(grant)
            assert result["status"] == "confirmation_required"
            handler.assert_not_awaited()
            rig.factory.assert_not_called()
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("reply", ["30", "тридцать процентов", "На 30%"])
def test_numeric_answer_to_brightness_question_finishes_only_the_bound_request(rig, reply):
    async def run():
        conv, store = conversation(rig)
        try:
            text = "Сделай мне розовый свет, только не слишком яркий"
            assert "в процентах" in await ask(conv, text)
            rig.factory.assert_not_called()
            assert "яркость 30%" in await ask(conv, reply)
            assert rig.strip._load().brightness == 30
            assert len(rig.clients) == 3
            assert conv.controls.pending_brightness is None
        finally:
            store.close()

    asyncio.run(run())


def test_numeric_clarification_cannot_cross_expiry_or_new_device_request(rig):
    async def run():
        conv, store = conversation(rig)
        now = [0.0]
        conv.controls = ControlSession(ttl_s=10, clock=lambda: now[0])
        try:
            await ask(conv, "Сделай ленту розовой, только не слишком ярко")
            now[0] = 11
            assert "истекло" in await ask(conv, "30")
            rig.factory.assert_not_called()
            await ask(conv, "Сделай ленту розовой, только не слишком ярко")
            await ask(conv, "Выключи ленту")
            assert conv.controls.pending_brightness is None
            assert len(rig.clients) == 1
        finally:
            store.close()

    asyncio.run(run())
