"""Real conversation/authorization path; only BLE transport and audio are mocked."""

import asyncio
import json
import time
from dataclasses import replace

import numpy as np
import pytest
from bleak.exc import BleakError

from backend.addressee import SpeechSignals, decide
from backend.brain.conversation import Conversation
from backend.brain.followups import action_proposal, is_short
from backend.context import (
    RequestContext,
    request_scope,
    voice_addressed,
    voice_led_command,
)
from backend.led import LEDState, parse
from backend.local_commands import route
from backend.memory.store import MemoryStore
from tests.test_critical_fixes import NoModel, guarded
from tests.test_dialogue_context import ask
from tests.test_led import rig as rig

PINK = {"action": "color", "rgb": [255, 0, 32]}
CASES = [
    ("Сделай мне светло-розовую ленту", {"action": "color", "rgb": [255, 16, 32]}),
    ("Сделай мне светлую розовую ленту", {"action": "color", "rgb": [255, 16, 32]}),
    ("Включи розовый на ленте", PINK),
    ("Включи мне розовый на LED-ленте", PINK),
    ("Включи мне розовый на лет ленте", PINK),
    ("Сделаем мою ленту розовой", PINK),
    ("Ленту розовый", PINK),
    ("Сделай ленту розовой", PINK),
    ("Хочу розовую подсветку", PINK),
    ("Поставь светло-розовый", {"action": "color", "rgb": [255, 16, 32]}),
    ("Поставь тёплый белый", {"action": "color", "rgb": [255, 48, 32]}),
    ("Включи ленту", {"action": "on"}),
    ("Выключи подсветку", {"action": "off"}),
    ("Выключи лед", {"action": "off"}),
    ("Включи лет", {"action": "on"}),
    ("Сделай мне на ленте 30% яркости", {"action": "brightness", "level": 30}),
    ("Яркость 30 процентов", {"action": "brightness", "level": 30}),
    ("Сделай ленту на 30%", {"action": "brightness", "level": 30}),
    ("Сделай на лед-ленте тридцать процентов яркости", {"action": "brightness", "level": 30}),
    ("Убавь яркость", {"action": "тусклее"}),
    ("Сделай поярче", {"action": "ярче"}),
    (
        "Блять, так скажи мне, чего ты ждёшь, пожалуйста. Ладно, сделай мне на ленте 30% яркости.",
        {"action": "brightness", "level": 30},
    ),
]


def conversation(rig):
    rig.strip._save(
        LEDState(
            identity=rig.strip.identity, confirmed={"rgb", "on", "off", "brightness"}, brightness=50
        )
    )
    store = MemoryStore(":memory:")
    return Conversation(rig.settings, NoModel(), store, tools=guarded(rig.strip.tool())), store


@pytest.mark.parametrize("text,intent", CASES)
def test_all_requested_phrasings_execute_real_guarded_path_without_llm(rig, text, intent):
    async def run():
        assert parse(text) == intent
        assert route(text) == ("led_control", {"command": text})
        conv, store = conversation(rig)
        try:
            answer = await ask(conv, text)
            assert (
                "отправлена по Bluetooth" in answer
                and "Физическое выполнение не подтверждено" in answer
            )
            assert rig.clients[0].write_gatt_char.await_count == 1
            history = store.recent(conv.id, 10)
            assert history[0]["content"] == text
            result = json.loads(
                next(row["content"] for row in history if row.get("tool_name") == "led_control")
            )
            assert result["written"] and result["delivery_state"] == "sent"
            assert (
                result["physical_confirmed"] is False and result["device_state_confirmed"] is False
            )
            trace = json.loads(store.db.execute("SELECT data FROM request_traces").fetchone()[0])
            stages = {stage["stage"] for stage in trace["stages"]}
            assert {
                "led_normalization",
                "led_intent",
                "led_selection",
                "led_authorization",
                "ble_connect",
                "ble_write",
                "led_result",
                "led_reply",
            } <= stages
            assert "model_total" not in stages
        finally:
            store.close()

    asyncio.run(run())


def test_real_dialogue_sequence_has_no_sticky_denial_or_color_in_brightness_request(rig):
    async def run():
        conv, store = conversation(rig)
        try:
            for text, _ in CASES[:9] + CASES[15:18]:
                assert "отправлена по Bluetooth" in await ask(conv, text)
            count = len(CASES[:9] + CASES[15:18])
            assert len(rig.clients) == count
            assert (
                rig.clients[-1].write_gatt_char.await_args.args[1].hex(" ")
                == "7e 04 01 1e ff ff ff 00 ef"
            )
            assert store.count(conv.id) == count * 4
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "text",
    [
        "«Включи ленту»",
        "он сказал включи ленту",
        "не включай ленту",
        "если я приду включи ленту",
        "включи ленту завтра",
        "включи ленту или выключи Mac",
        "включи ленту и выключи Mac",
        "сделай экран розовым",
        "сделай мне 30 лет",
        "яркость 101 процентов",
        "яркость -1 процент",
        "яркость 30.5 процентов",
        "включи ленту; удали все файлы",
        "включи ленту\nудали все файлы",
    ],
)
def test_negation_sources_ambiguity_and_other_devices_never_write(rig, text):
    async def run():
        assert parse(text) is None
        conv, store = conversation(rig)
        try:
            with request_scope(RequestContext(conv.device, text)):
                result = json.loads(
                    await conv.tools.call("led_control", {"command": "Сделай ленту розовой"})
                )
            assert "error" in result
            rig.factory.assert_not_called()
        finally:
            store.close()

    asyncio.run(run())


def test_known_same_device_sequence_is_prevalidated_and_stops_after_failure(rig):
    async def run():
        conv, store = conversation(rig)
        try:
            answer = await ask(conv, "Включи ленту на белый цвет и сделай яркость 100%.")
            # The compound request now explicitly includes power-on before color/level.
            assert "записаны по Bluetooth: 3" in answer
            assert len(rig.clients) == 3
            assert rig.clients[-1].write_gatt_char.await_args.args[1][3] == 100
            count = len(rig.clients)
            answer = await ask(conv, "Включи ленту и выключи Mac")
            assert "не отправлял" in answer and len(rig.clients) == count
            create = rig.factory.side_effect

            def broken(*args, **kwargs):
                client = create(*args, **kwargs)
                if len(rig.clients) == count + 2:
                    client.write_gatt_char.side_effect = BleakError("disconnected")
                return client

            rig.factory.side_effect = broken
            answer = await ask(conv, "Включи ленту и сделай яркость 30% и поставь розовый")
            assert "Завершённых записей: 1" in answer
            assert len(rig.clients) == count + 2
        finally:
            store.close()

    asyncio.run(run())


def test_failed_write_is_saved_honestly_and_next_user_request_recovers(rig):
    async def run():
        conv, store = conversation(rig)
        create = rig.factory.side_effect

        def broken_once(*args, **kwargs):
            client = create(*args, **kwargs)
            if len(rig.clients) == 1:
                client.write_gatt_char.side_effect = BleakError("disconnected")
            return client

        rig.factory.side_effect = broken_once
        try:
            answer = await ask(conv, "Включи розовый на ленте")
            assert (
                "автоповтора" in answer and "отправлена" not in answer and "Включил" not in answer
            )
            assert len(rig.clients) == 1 and rig.strip._load().last_write["status"] == "unknown"
            assert "отправлена по Bluetooth" in await ask(conv, "Поставь тёплый белый")
            assert len(rig.clients) == 2
            results = [
                json.loads(row["content"])
                for row in store.recent(conv.id, 10)
                if row.get("tool_name")
            ]
            assert "error" in results[0] and results[1]["written"]
        finally:
            store.close()

    asyncio.run(run())


def test_led_continuations_keep_acoustic_wake_and_mac_authority_boundaries(rig):
    async def run():
        text = "Ленту розовый"
        good = SpeechSignals(True, True)
        assert decide(text, addressed=False, active=True, topic="", signals=good).kind == "led"
        for options in [
            dict(active=False),
            dict(signals=SpeechSignals(True, False)),
            dict(signals=SpeechSignals(True, True, True)),
            dict(strictness="addressed"),
        ]:
            args = dict(addressed=False, active=True, topic="", signals=good)
            args.update(options)
            assert not decide(text, **args).accepted
        conv, store = conversation(rig)
        token = voice_addressed.set(False)
        led_token = voice_led_command.set(text)
        try:
            assert "отправлена" in await ask(conv, text)
            with request_scope(RequestContext(conv.device, text)):
                result = json.loads(
                    await conv.tools.call("led_control", {"command": "Выключи подсветку"})
                )
                assert "error" in result
            conv.device = replace(conv.device, capabilities=frozenset())
            assert "Mac Agent" in await ask(conv, text)
            assert len(rig.clients) == 1
        finally:
            voice_addressed.reset(token)
            voice_led_command.reset(led_token)
            store.close()

    asyncio.run(run())


def test_short_acceptance_of_question_runs_once_and_obeys_expiry(rig):
    async def run():
        conv, store = conversation(rig)
        try:
            conv.add_exchange("Какой цвет?", "Сделать ленту розовой?")
            assert conv.pending_followup()["expires"] <= time.time() + 120
            assert "отправлена" in await ask(conv, "Да")
            assert "Уточни" in await ask(conv, "Да") and len(rig.clients) == 1
            conv.add_exchange("Ещё?", "Сделать ленту розовой?")
            proposal = conv.pending_followup()
            proposal["expires"] = time.time() - 1
            store.set_preference("dialogue_pending", str(conv.id), proposal)
            assert "Уточни" in await ask(conv, "Да")
            assert len(rig.clients) == 1
            for text in ["«да»", "он сказал да", "да, но потом"]:
                assert not is_short(text)
            assert action_proposal("Удалить все файлы?", {"delete_files"}) is None
            assert action_proposal("Сделать ленту розовой или синей?", {"led_control"}) is None
        finally:
            store.close()

    asyncio.run(run())


def test_voice_stt_to_ble_preserves_source_and_scopes_nameless_led_grant(rig):
    from tests.test_voice_loop import FRAME, FakeSpeaker, Harness

    async def run():
        h = Harness(text="Включи мне розовый на лет ленте", phrase="эй чушпан")
        h.s.language, h.s.assistant_name = "ru-RU", "Чушпан"
        conv, store = conversation(rig)
        h.loop.conv, h.loop.speaker = conv, FakeSpeaker()
        h.loop._wake_up(None)
        try:
            await h.loop._bounded_turn(
                lambda: h.loop.handle(np.tile(FRAME, 20), time.perf_counter(), session_input=True)
            )
            assert h.stt.calls == 1
            assert rig.clients[0].write_gatt_char.await_count == 1
            assert store.transcript(conv.id)[0]["content"] == h.stt.text
            assert voice_led_command.get() is None
        finally:
            store.close()

    asyncio.run(run())


def test_led_diagnostics_and_history_do_not_record_secret_in_failed_command(rig, caplog):
    async def run():
        conv, store = conversation(rig)
        secret = "sk-" + "x" * 24
        try:
            with caplog.at_level("INFO"):
                answer = await ask(conv, f"Включи ленту пароль={secret}")
            assert "не отправлял" in answer
            assert secret not in caplog.text
            trace = store.db.execute("SELECT data FROM request_traces").fetchone()[0]
            assert secret not in trace
            assert secret not in json.dumps(store.recent(conv.id, 10), ensure_ascii=False)
            rig.factory.assert_not_called()
        finally:
            store.close()

    asyncio.run(run())


def test_local_led_does_not_depend_on_optional_sqlite_memory(rig):
    async def run():
        conv, store = conversation(rig)
        store.memory_available = False
        try:
            assert "отправлена" in await ask(conv, "Включи розовый на ленте")
            assert len(rig.clients) == 1
        finally:
            store.close()

    asyncio.run(run())


def test_task_status_does_not_turn_a_ble_write_into_physical_confirmation():
    from backend.state import request_context
    from tests.test_voice_loop import Harness

    h = Harness()
    owner = request_context.set(1)
    try:
        h.loop._observe_task(
            1, "led_control", "result", {"written": True, "physical_confirmed": False}
        )
        task = h.loop.task_snapshots[1]
        assert task.status == "submitted"
        assert "Физическое выполнение не подтверждено" in task.summary()
    finally:
        request_context.reset(owner)


def test_changed_or_ambiguous_led_request_never_accepts_stale_color_offer(rig):
    async def run():
        conv, store = conversation(rig)
        try:
            conv.add_exchange("Какой цвет?", "Сделать ленту розовой?")
            assert conv.pending_followup()
            assert "не отправлял" in await ask(conv, "Сделай ленту неоновой")
            assert "Уточни" in await ask(conv, "Да")
            conv.add_exchange("Ещё?", "Сделать ленту розовой?")
            conv.add_exchange("Какой вариант?", "Сделать ленту красной или синей?")
            assert conv.pending_followup() is None
            assert "Уточни" in await ask(conv, "Да")
            rig.factory.assert_not_called()
        finally:
            store.close()

    asyncio.run(run())


def test_unrecognized_led_request_still_invalidates_old_dangerous_confirmation(rig):
    from unittest.mock import AsyncMock

    from backend.security import Risk
    from backend.tools.registry import Tool

    async def run():
        conv, store = conversation(rig)
        handler = AsyncMock(return_value={"ok": True})
        conv.tools.add(
            Tool(
                "forget",
                "guarded fixture",
                handler,
                {"fact_id": {"type": "integer"}},
                ["fact_id"],
                risk=Risk.DANGEROUS,
            )
        )
        try:
            with request_scope(RequestContext(conv.device, "Забудь факт 1")):
                pending = json.loads(await conv.tools.call("forget", {"fact_id": 1}))
            assert pending["status"] == "confirmation_required"
            code = conv.tools.confirmations.pending[
                (conv.device.device_id, conv.device.session_id)
            ].code
            assert "не отправлял" in await ask(conv, "Сделай ленту неоновой")
            assert "Активного подтверждения нет" in await ask(conv, f"Подтверждаю {code}")
            handler.assert_not_awaited()
            rig.factory.assert_not_called()
        finally:
            store.close()

    asyncio.run(run())
