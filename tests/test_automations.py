"""Automation acceptance paths use the production conversation/guards and mocked devices."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from backend.automation.models import Block, Condition, Scenario, Variable
from backend.automation.service import Automations
from backend.automation.store import ScenarioStore
from backend.brain.conversation import Conversation
from backend.config import Settings
from backend.context import (
    RequestContext,
    request_scope,
    voice_addressed,
    voice_control_command,
)
from backend.hud.bus import HudBus
from backend.intents import compile_plan
from backend.memory.store import MemoryStore
from backend.security import Risk
from backend.tools.registry import Tool, ToolRegistry
from tests.test_dialogue_context import Model, ask
from tests.test_voice_loop import custom_harness
from tests.test_voice_reliability import SPEECH


@pytest.fixture
def rig(tmp_path):
    settings = Settings(language="ru-RU", assistant_name="Чушпан")
    memory = MemoryStore(tmp_path / "test.sqlite")
    brightness = AsyncMock(side_effect=lambda level: {"level": level})
    led = AsyncMock(
        return_value={"written": True, "delivery_state": "sent", "physical_confirmed": False}
    )
    power = AsyncMock(return_value={"submitted": True})
    registry = ToolRegistry(
        [
            Tool(
                "set_brightness",
                "test",
                brightness,
                {"level": {"type": "integer", "minimum": 0, "maximum": 100}},
                ["level"],
                risk=Risk.CHANGE,
            ),
            Tool(
                "led_control",
                "test",
                led,
                {"command": {"type": "string"}},
                ["command"],
                risk=Risk.CHANGE,
            ),
            Tool(
                "mac_power",
                "test",
                power,
                {"action": {"type": "string", "enum": ["sleep", "shutdown"]}},
                ["action"],
                risk=Risk.DANGEROUS,
            ),
        ]
    )
    service = Automations(registry, settings, memory)
    registry.automations = service
    conv = Conversation(settings, Model(), memory, tools=registry)
    value = SimpleNamespace(
        s=settings,
        memory=memory,
        tools=registry,
        service=service,
        conv=conv,
        brightness=brightness,
        led=led,
        power=power,
    )
    yield value
    memory.close()


def saved(rig, **kwargs):
    values = dict(
        name="Вечер",
        phrases=["Пора отдыхать"],
        actions=[Block(capability="mac.brightness", parameters={"level": 30})],
    )
    values.update(kwargs)
    return rig.service.store.save(Scenario(**values))


def test_saved_phrase_voice_to_action_without_confirmation_or_model(rig):
    scenario = saved(rig)

    async def run():
        answer = await ask(rig.conv, "Чушпан, пора отдыхать!")
        assert "выполнен и проверен" in answer
        rig.brightness.assert_awaited_once_with(level=30)
        assert not rig.conv.llm.requests
        runs = rig.service.store.history(scenario.id)
        assert runs[0]["steps"][0]["status"] == "confirmed"
        trace = json.loads(
            rig.memory.db.execute("SELECT data FROM request_traces ORDER BY rowid DESC").fetchone()[
                0
            ]
        )
        stages = {s["stage"] for s in trace["stages"]}
        assert {
            "scenario_search",
            "intent_detection",
            "action_planning",
            "scenario_execution",
            "result_verification",
        } <= stages

    asyncio.run(run())


@pytest.mark.parametrize(
    "text",
    [
        "Чушпан, сделай ленту розовой",
        "Поставь розовый свет",
        "Вруби розовенький",
        "Хочу розовую подсветку",
        "Поменяй цвет ленты на розовый",
    ],
)
def test_flexible_local_phrases(text):
    plan = compile_plan(text)
    assert plan and not plan.clarification
    assert plan.actions[0].parameters.color == "#FF0020"


def test_three_requested_actions_are_fully_extracted():
    plan = compile_plan(
        "Чушпан, сделай ленту светло-розовой, яркость на 100 процентов, а телевизор потише"
    )
    assert plan and not plan.clarification
    assert [a.device for a in plan.actions] == ["led_strip", "tv"]
    assert plan.actions[0].parameters.brightness == 100
    assert plan.actions[1].call()[0] is None  # No invented TV-volume API.


def test_unlimited_phrases_durability_edit_delete_and_conflicts(rig):
    scenario = saved(rig, phrases=[f"Мой особый ритуал номер {i}" for i in range(1500)])
    restarted = ScenarioStore(rig.memory)
    assert len(restarted.get(scenario.id).phrases) == 1500
    edited = restarted.teach(scenario.id, "Мой тихий вечер", old=scenario.phrases[0])
    assert "Мой тихий вечер" in edited.phrases
    with pytest.raises(ValueError, match="уже принадлежит"):
        saved(rig, name="Другое", phrases=["Чушпан, мой тихий вечер!"])
    restarted.teach(scenario.id, None, old="Мой тихий вечер")
    assert "Мой тихий вечер" not in restarted.get(scenario.id).phrases
    restarted.delete(scenario.id, restarted.get(scenario.id).revision)
    with pytest.raises(ValueError):
        restarted.get(scenario.id)


def test_duplicate_normalized_phrases_are_rejected():
    with pytest.raises(ValidationError):
        Scenario(name="test", phrases=["Мой вечер", "Чушпан, МОЙ вечер!"])


def test_revision_protects_from_overwriting_another_window(rig):
    original = saved(rig)
    rig.service.store.save(original.model_copy(update={"name": "Изменено"}))
    with pytest.raises(ValueError, match="другом окне"):
        rig.service.store.save(original)


def test_smart_semantic_mode_and_ambiguity(rig):
    night = saved(rig, phrases=["Я спать"])
    assert rig.service.recognize("Пора ложиться")["scenario_id"] == night.id
    other = saved(rig, name="Другое", phrases=["Готовь ко сну"])
    assert rig.service.recognize("Давай готовиться ко сну")["scenario_id"] == other.id
    third = saved(rig, name="Третье", phrases=["Пора засыпать"])
    result = rig.service.recognize("Пора ложиться")
    assert result["scenario_id"] is None and result["method"] == "ambiguous"
    assert {c["id"] for c in result["candidates"]} >= {night.id, third.id}


def test_exact_mode_never_runs_semantic_variation(rig):
    scenario = saved(rig, mode="exact", phrases=["Я спать"])
    assert rig.service.recognize("Чушпан, я спать!")["scenario_id"] == scenario.id
    assert rig.service.recognize("Пора ложиться")["scenario_id"] is None


@pytest.mark.parametrize(
    "text",
    [
        "Не пора отдыхать",
        "Если пора отдыхать",
        "Он сказал пора отдыхать",
        "«Пора отдыхать»",
        "Пора отдыхать?",
        "Пора отдыхать или работать",
        "Спасибо за просмотр",
        "ээээ",
    ],
)
def test_negation_quotes_tv_noise_and_alternatives_cannot_trigger(rig, text):
    saved(rig)
    assert rig.service.recognize(text)["scenario_id"] is None
    assert not rig.brightness.await_count


def test_contextual_voice_only_with_trusted_audio_grant(rig):
    saved(rig)

    async def run():
        token = voice_addressed.set(False)
        grant = voice_control_command.set("Пора отдыхать")
        try:
            assert "выполнен и проверен" in await ask(rig.conv, "Пора отдыхать")
        finally:
            voice_control_command.reset(grant)
            voice_addressed.reset(token)
        rig.brightness.assert_awaited_once()

    asyncio.run(run())


def test_disabled_scenario_and_unavailable_capability(rig):
    scenario = saved(rig, enabled=False)
    assert rig.service.recognize("Пора отдыхать")["scenario_id"] is None
    with pytest.raises(ValueError, match="не поддерживается"):
        rig.service.capabilities.validate(
            Scenario(
                name="test",
                actions=[Block(capability="tv.nonexistent", parameters={"shell": "false"})],
            )
        )

    async def run():
        context = RequestContext(rig.conv.device, "Пора отдыхать")
        with request_scope(context):
            result = json.loads(
                await rig.tools.call(
                    "run_scenario", {"id": scenario.id, "revision": scenario.revision}
                )
            )
        assert "error" in result
        rig.brightness.assert_not_awaited()

    asyncio.run(run())


def test_failure_stops_successors_and_is_never_success(rig):
    rig.brightness.return_value = {"error": "Нет Accessibility"}
    rig.brightness.side_effect = None
    saved(
        rig,
        actions=[
            Block(capability="mac.brightness", parameters={"level": 30}),
            Block(capability="led.set", parameters={"power": True}),
        ],
    )

    async def run():
        answer = await ask(rig.conv, "Пора отдыхать")
        assert "ошибкой" in answer and "Accessibility" in answer
        rig.led.assert_not_awaited()
        assert rig.service.store.history()[0]["status"] == "failed"

    asyncio.run(run())


def test_continue_error_records_failed_run_but_executes_next(rig):
    rig.brightness.side_effect = None
    rig.brightness.return_value = {"error": "Устройство недоступно"}
    saved(
        rig,
        actions=[
            Block(capability="mac.brightness", parameters={"level": 30}, on_error="continue"),
            Block(capability="led.set", parameters={"power": True}),
        ],
    )

    async def run():
        await ask(rig.conv, "Пора отдыхать")
        rig.led.assert_awaited_once()
        assert rig.service.store.history()[0]["status"] == "failed"

    asyncio.run(run())


@pytest.mark.parametrize(
    "receipt, expected",
    [({"ok": True}, "unverified"), ({"level": 31}, "unverified"), ({"level": 30}, "confirmed")],
)
def test_actual_result_must_match_requested_level(rig, receipt, expected):
    rig.brightness.side_effect = None
    rig.brightness.return_value = receipt
    saved(rig)
    asyncio.run(ask(rig.conv, "Пора отдыхать"))
    assert rig.service.store.history()[0]["status"] == expected


def test_led_delivery_is_not_claimed_physical_confirmation(rig):
    saved(
        rig,
        actions=[Block(capability="led.set", parameters={"color": "#FF0020", "brightness": 10})],
    )
    answer = asyncio.run(ask(rig.conv, "Пора отдыхать"))
    assert "физический результат не подтверждён" in answer
    assert rig.service.store.history()[0]["status"] == "sent"


def test_dangerous_scenario_confirms_complete_version_before_any_step(rig):
    saved(
        rig,
        actions=[
            Block(capability="mac.brightness", parameters={"level": 30}),
            Block(capability="mac.power", parameters={"action": "sleep"}),
        ],
    )

    async def run():
        answer = await ask(rig.conv, "Пора отдыхать")
        assert "Подтверждаю" in answer
        rig.brightness.assert_not_awaited()
        rig.power.assert_not_awaited()
        pending = next(iter(rig.tools.confirmations.pending.values()))
        result = await ask(rig.conv, f"Подтверждаю {pending.code}")
        assert "отправлены" in result
        rig.brightness.assert_awaited_once()
        rig.power.assert_awaited_once()
        await ask(rig.conv, f"Подтверждаю {pending.code}")
        rig.power.assert_awaited_once()

    asyncio.run(run())


def test_edit_after_confirmation_request_prevents_execution(rig):
    scenario = saved(rig, confirmation="always")

    async def run():
        await ask(rig.conv, "Пора отдыхать")
        pending = next(iter(rig.tools.confirmations.pending.values()))
        rig.service.store.save(
            scenario.model_copy(
                update={"actions": [Block(capability="mac.brightness", parameters={"level": 90})]}
            )
        )
        answer = await ask(rig.conv, f"Подтверждаю {pending.code}")
        assert "изменился" in answer
        rig.brightness.assert_not_awaited()

    asyncio.run(run())


def test_variables_delay_if_else_and_parallel_different_devices(rig):
    scenario = saved(
        rig,
        variables=[Variable(name="level", default=25)],
        actions=[
            Block(kind="delay", seconds=0),
            Block(
                kind="if",
                condition=Condition(source="variable", key="level", operator="lt", value=50),
                children=[
                    Block(
                        kind="parallel",
                        children=[
                            Block(
                                capability="mac.brightness",
                                parameters={"level": {"variable": "level"}},
                            ),
                            Block(capability="led.set", parameters={"power": True}),
                        ],
                    )
                ],
                otherwise=[Block(capability="mac.brightness", parameters={"level": 100})],
            ),
        ],
    )
    asyncio.run(ask(rig.conv, "Пора отдыхать"))
    rig.brightness.assert_awaited_once_with(level=25)
    rig.led.assert_awaited_once()
    assert rig.service.store.history(scenario.id)[0]["status"] == "sent"


def test_same_device_parallel_branches_are_rejected(rig):
    scenario = saved(
        rig,
        actions=[
            Block(
                kind="parallel",
                children=[
                    Block(capability="mac.brightness", parameters={"level": 10}),
                    Block(capability="mac.brightness", parameters={"level": 90}),
                ],
            )
        ],
    )
    asyncio.run(ask(rig.conv, "Пора отдыхать"))
    rig.brightness.assert_not_awaited()
    assert rig.service.store.history(scenario.id)[0]["status"] == "failed"


def test_conflicting_scenarios_and_direct_control_cannot_race(rig):
    first = saved(
        rig,
        actions=[
            Block(kind="delay", seconds=0.04),
            Block(capability="mac.brightness", parameters={"level": 30}),
        ],
    )
    second = saved(rig, name="Второй", phrases=["Пора работать"])

    async def run():
        async def execute(scenario):
            with request_scope(RequestContext(rig.conv.device, "test")):
                return await rig.service.engine.run(scenario)

        task = asyncio.create_task(execute(first))
        await asyncio.sleep(0.005)
        result = await execute(second)
        assert result["status"] == "failed" and "уже выполняет" in result["error"]
        with request_scope(RequestContext(rig.conv.device, "Поставь яркость Mac 50")):
            direct = json.loads(await rig.tools.call("set_brightness", {"level": 50}))
        assert "занято" in direct["error"]
        await task
        rig.brightness.assert_awaited_once_with(level=30)

    asyncio.run(run())


def test_run_event_is_idempotent_and_interrupted_work_is_not_replayed(rig):
    scenario = saved(
        rig,
        actions=[
            Block(kind="delay", seconds=1),
            Block(capability="mac.brightness", parameters={"level": 30}),
        ],
    )

    async def run():
        context = RequestContext(rig.conv.device, "test")
        with request_scope(context):
            task = asyncio.create_task(rig.service.engine.run(scenario))
            await asyncio.sleep(0.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            replay = await rig.service.engine.run(scenario)
        assert replay["replayed"] and replay["status"] == "interrupted"
        rig.brightness.assert_not_awaited()

    asyncio.run(run())


def test_ui_save_teach_recognize_run_copy_delete_use_same_service(rig):
    async def run():
        bus = HudBus()
        rig.service.bind(bus, rig.conv)
        queue = bus.subscribe()
        try:
            scenario = Scenario(
                name="UI",
                phrases=["Мой тест интерфейса"],
                actions=[Block(capability="mac.brightness", parameters={"level": 20})],
            )
            await rig.service.dispatch({"op": "save", "scenario": scenario.model_dump(mode="json")})
            event = await queue.get()
            assert event["saved"]
            identifier = event["saved"]
            await rig.service.dispatch(
                {"op": "teach", "id": identifier, "phrase": "Моя новая фраза", "consent": True}
            )
            await rig.service.dispatch({"op": "recognize", "text": "Моя новая фраза"})
            edited = rig.service.store.get(identifier)
            await rig.service.dispatch({"op": "run", "id": identifier, "revision": edited.revision})
            rig.brightness.assert_awaited_once_with(level=20)
            await rig.service.dispatch({"op": "copy", "id": identifier})
            copies = [s for s in rig.service.store.list() if s.name.endswith("(копия)")]
            assert len(copies) == 1 and not copies[0].phrases and not copies[0].enabled
            await rig.service.dispatch(
                {"op": "delete", "id": identifier, "revision": edited.revision}
            )
        finally:
            await rig.service.close()

    asyncio.run(run())


def test_training_requires_consent_and_conversation_correction_is_only_a_proposal(rig):
    async def run():
        saved(rig)
        await rig.service.dispatch(
            {"op": "teach", "id": rig.service.store.list()[-1].id, "phrase": "Без согласия"}
        )
        assert not rig.service.store.training()
        await ask(rig.conv, "Сделай нормальный вечерний свет")
        answer = await ask(rig.conv, "Нет, я имела в виду тёплый белый на 30 процентов")
        assert "Сохранить" in answer
        assert not any(
            s.name == "Сделай нормальный вечерний свет" for s in rig.service.store.list()
        )
        await ask(rig.conv, "Сохрани предпочтение")
        learned = next(
            s for s in rig.service.store.list() if s.name == "Сделай нормальный вечерний свет"
        )
        assert learned.actions[0].parameters["brightness"] == 30
        rig.led.assert_not_awaited()

    asyncio.run(run())


def test_voice_capture_diverts_next_utterance_and_never_runs_it(rig):
    async def run():
        bus = HudBus()
        rig.service.bind(bus, rig.conv)
        queue = bus.subscribe()
        try:
            await rig.service.dispatch({"op": "capture", "consent": True})
            assert rig.service.capture("Включи ленту", True)
            events = []
            while not queue.empty():
                events.append(queue.get_nowait())
            assert any(e.get("captured") == "Включи ленту" for e in events)
            rig.led.assert_not_awaited()
            assert not rig.service.store.training()
        finally:
            await rig.service.close()

    asyncio.run(run())


def test_permissions_never_infer_unobserved_grants(rig, monkeypatch):
    from backend.automation import permissions

    probe = AsyncMock(
        return_value={
            "microphone": "denied",
            "accessibility": "granted",
            "automation": {"Safari": "unknown"},
        }
    )
    monkeypatch.setattr(permissions, "diagnose", probe)
    result = asyncio.run(permissions.report(rig.s))
    states = {r["id"]: r["status"] for r in result["items"]}
    assert states["microphone"] == "denied" and states["accessibility"] == "granted"
    assert states["automation"] == "unknown" and states["bluetooth"] == "manual"
    assert states["speech"] == "unsupported"


def test_permission_or_agent_revocation_blocks_saved_scenario(rig):
    saved(rig)
    device = replace(rig.conv.device, authenticated=False)
    rig.conv.device = device
    asyncio.run(ask(rig.conv, "Пора отдыхать"))
    rig.brightness.assert_not_awaited()


def test_structured_model_plan_rejects_invented_parameters_and_numbers(rig):
    async def run():
        for params in ({"level": 95}, {"level": 30, "shell": "false"}):
            with request_scope(RequestContext(rig.conv.device, "Сделай яркость Mac 30 процентов")):
                receipt = json.loads(
                    await rig.tools.call(
                        "control_devices",
                        {"actions": [{"capability": "mac.brightness", "parameters": params}]},
                    )
                )
            assert "error" in receipt
        rig.brightness.assert_not_awaited()

    asyncio.run(run())


def test_structured_model_plan_runs_only_declared_capabilities(rig):
    async def run():
        with request_scope(
            RequestContext(rig.conv.device, "Сделай яркость Mac 30 процентов и включи ленту")
        ):
            receipt = json.loads(
                await rig.tools.call(
                    "control_devices",
                    {
                        "actions": [
                            {"capability": "mac.brightness", "parameters": {"level": 30}},
                            {"capability": "led.set", "parameters": {"power": True}},
                        ]
                    },
                )
            )
        assert receipt["status"] == "sent"
        rig.brightness.assert_awaited_once_with(level=30)
        rig.led.assert_awaited_once()

    asyncio.run(run())


def test_real_voice_loop_passes_custom_phrase_to_scenario_engine(rig):
    saved(rig)

    async def run():
        harness = custom_harness("Чушпан, пора отдыхать")
        harness.loop.conv = rig.conv
        harness.loop._wake_up(None)
        try:
            await harness.loop.handle(SPEECH, 0, wake_validated=True)
            rig.brightness.assert_awaited_once_with(level=30)
        finally:
            harness.loop._close_session()

    asyncio.run(run())
