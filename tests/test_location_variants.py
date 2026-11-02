"""One command with independent location variants, through existing guards and engine."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from backend.automation.models import Block, Condition, Place, Scenario, Variant
from backend.automation.preview import preview
from backend.automation.store import ScenarioStore
from backend.hud.bus import HudBus
from backend.memory.store import MemoryStore
from tests.test_automations import rig as rig
from tests.test_dialogue_context import ask


def command(rig):
    return rig.service.store.save(
        Scenario(
            name="Одна команда",
            phrases=["Мой ритуал сна"],
            actions=[Block(capability="mac.brightness", parameters={"level": 10})],
            variants=[
                Variant(
                    name="Общага",
                    place_id="dorm",
                    actions=[Block(capability="mac.brightness", parameters={"level": 25})],
                ),
                Variant(
                    name="Дом",
                    place_id="home",
                    actions=[Block(capability="mac.brightness", parameters={"level": 40})],
                ),
            ],
        )
    )


def test_one_phrase_chooses_one_independent_place_version(rig):
    scenario = command(rig)

    async def run():
        rig.service.locations.select("manual", "dorm")
        assert "проверен" in await ask(rig.conv, "Мой ритуал сна")
        rig.service.locations.select("manual", "home")
        assert "проверен" in await ask(rig.conv, "Мой ритуал сна")
        assert [c.kwargs["level"] for c in rig.brightness.await_args_list] == [25, 40]
        history = rig.service.store.history(scenario.id)
        assert [h["variant_name"] for h in history] == ["Дом", "Общага"]
        assert len({h["scenario_id"] for h in history}) == 1
        assert not rig.conv.llm.requests

    asyncio.run(run())


def test_unknown_place_missing_version_and_disabled_version_use_general(rig):
    scenario = command(rig)

    async def run():
        assert "проверен" in await ask(rig.conv, "Мой ритуал сна")
        rig.service.locations.select("manual", "home")
        edited = rig.service.store.save(
            scenario.model_copy(update={"variants": [scenario.variants[0]]})
        )
        assert "проверен" in await ask(rig.conv, "Мой ритуал сна")
        disabled = edited.variants[0].model_copy(update={"enabled": False})
        rig.service.store.save(edited.model_copy(update={"variants": [disabled]}))
        rig.service.locations.select("manual", "dorm")
        assert "проверен" in await ask(rig.conv, "Мой ритуал сна")
        assert [call.kwargs["level"] for call in rig.brightness.await_args_list] == [10, 10, 10]

    asyncio.run(run())


def test_auto_uses_fresh_existing_city_and_custom_user_aliases(rig):
    observed = {
        "city": "Tallinn",
        "status": "ready",
        "source": "macos",
        "measured_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
        "latitude": 59.4,
        "longitude": 24.7,
        "accuracy_m": 100,
    }
    rig.memory.set_preference("dashboard", "location_enabled", True)
    rig.service.locations.provider = lambda: observed
    assert rig.service.locations.current()["place_id"] == "dorm"
    custom = rig.service.locations.save(Place(name="Дача", cities=["Новый город"]))
    observed["city"] = "Новый город"
    assert rig.service.locations.current()["place_id"] is None
    rig.service.locations.select("manual", custom.id)
    assert rig.service.locations.current()["place_id"] == custom.id
    rig.service.locations.select("auto")
    observed["measured_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert rig.service.locations.current()["place_id"] is None
    observed.update(status="stale", updated_at=datetime.now(UTC).isoformat())
    assert rig.service.locations.current()["place_id"] is None
    rig.service.locations.select("manual", "home")
    assert rig.service.locations.current()["place_id"] == "home"


def test_custom_places_add_edit_delete_and_conflict_detection(rig):
    locations = rig.service.locations
    place = locations.save(Place(name="Работа", cities=["Тестоград"]))
    updated = locations.save(place.model_copy(update={"name": "Офис"}))
    assert updated.revision == 2
    with pytest.raises(ValueError, match="другом окне"):
        locations.save(place)
    with pytest.raises(ValueError, match="другому месту"):
        locations.save(Place(name="Второе", cities=["Тестоград"]))
    scenario = rig.service.store.save(
        Scenario(name="Офисный", variants=[Variant(name="Офис", place_id=place.id)])
    )
    with pytest.raises(ValueError, match="Сначала"):
        locations.delete(place.id, updated.revision, [scenario])
    rig.service.store.delete(scenario.id, scenario.revision)
    locations.delete(place.id, updated.revision, rig.service.store.list())
    assert not any(p.id == place.id for p in locations.list())


def test_duplicate_version_for_one_place_and_invalid_step_parameters_rejected(rig):
    with pytest.raises(ValidationError, match="одного места"):
        Scenario(
            name="test",
            variants=[
                Variant(name="Дом", place_id="home"),
                Variant(name="Ещё дом", place_id="home"),
            ],
        )
    bad = Scenario(
        name="test",
        variants=[
            Variant(
                name="Дом",
                place_id="home",
                actions=[Block(capability="mac.brightness", parameters={"level": 101})],
            )
        ],
    )
    with pytest.raises(ValueError):
        rig.service.validate_document(bad)


def test_disabled_step_skips_validation_and_execution_but_keeps_data(rig):
    scenario = rig.service.store.save(
        Scenario(
            name="test",
            phrases=["Отключённый шаг"],
            actions=[
                Block(capability="not.installed", parameters={"level": 1000}, enabled=False),
                Block(capability="mac.brightness", parameters={"level": 15}),
            ],
        )
    )
    rig.service.capabilities.validate(scenario)
    asyncio.run(ask(rig.conv, "Отключённый шаг"))
    rig.brightness.assert_awaited_once_with(level=15)
    assert rig.service.store.history(scenario.id)[0]["steps"][0]["reason"] == "disabled"
    assert rig.service.store.get(scenario.id).actions[0].parameters["level"] == 1000


def test_optional_absent_device_skips_but_required_missing_blocks_before_any_write(rig):
    scenario = rig.service.store.save(
        Scenario(
            name="test",
            phrases=["Необязательный экран"],
            actions=[
                Block(capability="external.not_installed", required=False),
                Block(capability="mac.brightness", parameters={"level": 15}),
            ],
        )
    )
    asyncio.run(ask(rig.conv, "Необязательный экран"))
    rig.brightness.assert_awaited_once_with(level=15)
    assert rig.service.store.history(scenario.id)[0]["steps"][0]["reason"] == "unavailable"
    rig.brightness.reset_mock()
    mandatory = scenario.actions[0].model_copy(update={"required": True})
    rig.service.store.save(
        scenario.model_copy(update={"actions": [mandatory, scenario.actions[1]]})
    )
    response = asyncio.run(ask(rig.conv, "Необязательный экран"))
    assert "не поддерживается" in response
    rig.brightness.assert_not_awaited()


def test_optional_does_not_swallow_arbitrary_runtime_error(rig):
    scenario = rig.service.store.save(
        Scenario(
            name="test",
            phrases=["Ошибка устройства"],
            actions=[Block(capability="mac.brightness", parameters={"level": 15}, required=False)],
        )
    )
    rig.brightness.side_effect = None
    rig.brightness.return_value = {"error": "После отправки результат неизвестен"}
    asyncio.run(ask(rig.conv, "Ошибка устройства"))
    assert rig.service.store.history(scenario.id)[0]["status"] == "failed"


def test_safe_preview_uses_selected_variant_conditions_and_never_calls_device_or_sleep(
    rig, monkeypatch
):
    scenario = command(rig)
    version = scenario.variants[1]
    version.actions.append(Block(kind="delay", seconds=3600))
    version.actions.append(
        Block(
            kind="if",
            condition=Condition(source="time", key="hour", operator="gt", value=-1),
            children=[Block(capability="led.set", parameters={"power": True})],
            otherwise=[Block(capability="mac.power", parameters={"action": "sleep"})],
        )
    )
    sleep = AsyncMock(side_effect=AssertionError("Preview must not sleep"))
    monkeypatch.setattr("backend.automation.engine.asyncio.sleep", sleep)
    result = asyncio.run(preview(rig.service.engine, scenario, variant=version.id))
    assert result["status"] == "dry_run" and result["selection"]["variant_name"] == "Дом"
    assert [
        s["parameters"]["level"] for s in result["steps"] if s.get("parameters", {}).get("level")
    ] == [40]
    rig.brightness.assert_not_awaited()
    rig.led.assert_not_awaited()
    rig.power.assert_not_awaited()
    sleep.assert_not_awaited()
    assert not rig.service.store.history(scenario.id)


def test_versions_and_history_survive_reopen_restore_delete_and_conflicts(rig, tmp_path):
    original = command(rig)
    edited = rig.service.store.save(original.model_copy(update={"name": "Изменённая команда"}))
    restored = rig.service.store.restore(original.id, original.revision, edited.revision)
    assert restored.name == original.name and restored.revision == 3
    assert len(restored.variants) == 2
    with pytest.raises(ValueError):
        rig.service.store.restore(original.id, 1, edited.revision)
    rig.service.store.delete(restored.id, restored.revision)
    undeleted = rig.service.store.restore(restored.id, 3, 0)
    assert undeleted.revision == 4
    reopened = MemoryStore(tmp_path / "test.sqlite")
    try:
        stored = ScenarioStore(reopened).get(undeleted.id)
        assert stored.variants[0].place_id == "dorm"
        assert len(ScenarioStore(reopened).revisions(stored.id)) == 4
    finally:
        reopened.close()


def test_apply_updates_voice_immediately_without_restart_or_modifying_other_place(rig):
    scenario = command(rig)
    rig.service.locations.select("manual", "home")

    async def run():
        await ask(rig.conv, "Мой ритуал сна")
        edited = scenario.model_copy(deep=True)
        edited.variants[1].actions[0].parameters["level"] = 60
        bus = HudBus()
        rig.service.bind(bus, rig.conv)
        try:
            await rig.service.dispatch({"op": "save", "scenario": edited.model_dump(mode="json")})
            await ask(rig.conv, "Мой ритуал сна")
            rig.service.locations.select("manual", "dorm")
            await ask(rig.conv, "Мой ритуал сна")
            assert [c.kwargs["level"] for c in rig.brightness.await_args_list] == [40, 60, 25]
            assert not rig.conv.llm.requests
        finally:
            await rig.service.close()

    asyncio.run(run())


def test_location_change_invalidates_pending_approval_before_shared_steps(rig):
    scenario = command(rig)
    rig.service.store.save(scenario.model_copy(update={"confirmation": "always"}))
    rig.service.locations.select("manual", "home")

    async def run():
        await ask(rig.conv, "Мой ритуал сна")
        code = next(iter(rig.tools.confirmations.pending.values())).code
        rig.service.locations.select("manual", "dorm")
        result = await ask(rig.conv, "Подтверждаю " + code)
        assert "изменилось" in result
        rig.brightness.assert_not_awaited()

    asyncio.run(run())


def test_websocket_apply_preview_place_switch_voice_and_restore_use_same_command(rig):
    from contextlib import asynccontextmanager

    from fastapi.testclient import TestClient

    from backend.hud.server import allowed_origins, create_app
    from tests.test_voice_loop import custom_harness
    from tests.test_voice_reliability import SPEECH

    bus = HudBus()
    app = create_app(bus, allowed_origins("127.0.0.1", 8765))

    @asynccontextmanager
    async def lifespan(app):
        rig.service.bind(bus, rig.conv)
        harness = custom_harness("")
        harness.loop.conv = rig.conv
        harness.loop.bus = bus
        harness.loop._wake_up(None)

        async def voice(value):
            harness.stt.text = value["text"]
            await harness.loop.handle(SPEECH, 0, wake_validated=True)

        bus.handle("chat_message", lambda value: rig.service.spawn(voice(value)))
        try:
            yield
        finally:
            harness.loop._close_session()
            await rig.service.close()

    app.router.lifespan_context = lifespan
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:8765"}) as ws,
    ):

        def until(predicate):
            for _ in range(100):
                event = ws.receive_json()
                if predicate(event):
                    return event
            raise AssertionError("Missing expected event")

        def send(op, **value):
            ws.send_json({"type": "automations", "value": {"op": op, **value}})

        draft = Scenario(
            name="Одна команда из редактора",
            phrases=["Мой единый ритуал"],
            actions=[Block(capability="mac.brightness", parameters={"level": 10})],
            variants=[
                Variant(
                    name="Общага",
                    place_id="dorm",
                    actions=[Block(capability="mac.brightness", parameters={"level": 25})],
                ),
                Variant(
                    name="Дом",
                    place_id="home",
                    actions=[Block(capability="mac.brightness", parameters={"level": 40})],
                ),
            ],
        )
        send("preview", scenario=draft.model_dump(mode="json"), variant=draft.variants[1].id)
        tested = until(lambda e: bool(e.get("preview")))
        assert tested["preview"]["selection"]["variant_name"] == "Дом"
        rig.brightness.assert_not_awaited()
        send("location", mode="manual", place_id="dorm")
        until(lambda e: e.get("location", {}).get("place_id") == "dorm")
        send("save", scenario=draft.model_dump(mode="json"))
        first = until(lambda e: bool(e.get("saved")))
        identifier = first["saved"]
        send("run", id=identifier, revision=1)
        until(
            lambda e: any(
                r["scenario_id"] == identifier and r["status"] == "confirmed"
                for r in e.get("runs", [])
            )
        )
        assert [c.kwargs["level"] for c in rig.brightness.await_args_list] == [25]
        document = next(s for s in first["scenarios"] if s["id"] == identifier)
        document["variants"][1]["actions"][0]["parameters"]["level"] = 60
        send("save", scenario=document)
        until(lambda e: e.get("saved") == identifier)
        send("location", mode="manual", place_id="home")
        until(lambda e: e.get("location", {}).get("place_id") == "home")
        ws.send_json({"type": "chat_message", "value": {"text": "Чушпан, мой единый ритуал"}})
        until(
            lambda e: any(
                r["scenario_id"] == identifier
                and r["revision"] == 2
                and r["variant_name"] == "Дом"
                and r["status"] == "confirmed"
                for r in e.get("runs", [])
            )
        )
        assert [c.kwargs["level"] for c in rig.brightness.await_args_list] == [25, 60]
        send("restore", id=identifier, from_revision=1, expected_revision=2)
        restored = until(lambda e: e.get("saved") == identifier)
        restored_doc = next(s for s in restored["scenarios"] if s["id"] == identifier)
        assert restored_doc["revision"] == 3
        assert restored_doc["variants"][1]["actions"][0]["parameters"]["level"] == 40
        send("delete", id=identifier, revision=3)
        until(lambda e: e.get("deleted") == identifier)
        send("restore", id=identifier, from_revision=3, expected_revision=0)
        undeleted = until(lambda e: e.get("saved") == identifier)
        assert next(s for s in undeleted["scenarios"] if s["id"] == identifier)["revision"] == 4


def test_optional_device_absence_before_write_is_skipped_without_hiding_failure(rig):
    from backend.tools.registry import DeviceUnavailable

    rig.led.side_effect = DeviceUnavailable("Устройство не найдено до отправки")
    scenario = rig.service.store.save(
        Scenario(
            name="test",
            phrases=["Пропусти отсутствующую ленту"],
            actions=[
                Block(capability="led.set", parameters={"power": True}, required=False),
                Block(capability="mac.brightness", parameters={"level": 20}),
            ],
        )
    )
    asyncio.run(ask(rig.conv, "Пропусти отсутствующую ленту"))
    run = rig.service.store.history(scenario.id)[0]
    assert run["status"] == "confirmed" and run["steps"][0]["reason"] == "unavailable"
    rig.brightness.assert_awaited_once_with(level=20)
    profile = json.loads(
        rig.memory.db.execute("SELECT data FROM request_traces ORDER BY rowid DESC").fetchone()[0]
    )
    assert profile["status"] == "completed"
