"""Editor documents, backwards compatibility and guarded execution; all effects are mocked."""

import asyncio
import json
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


def independent(rig, **changes):
    values = dict(
        name="Я спать",
        phrases=["Время тихого вечера"],
        version_policy="fallback",
        timeout_s=3600,
        actions=[Block(capability="mac.brightness", parameters={"level": 10})],
        variants=[
            Variant(
                name="Общага",
                place_id="dorm",
                actions=[Block(capability="mac.brightness", parameters={"level": 20})],
            ),
            Variant(
                name="Дом",
                place_id="home",
                actions=[Block(capability="mac.brightness", parameters={"level": 30})],
            ),
        ],
    )
    values.update(changes)
    return rig.service.store.save(Scenario(**values))


def test_voice_local_version_replaces_general_and_unknown_uses_fallback(rig):
    scenario = independent(rig)

    async def run():
        assert rig.service.locations.current()["status"] == "unknown"
        await ask(rig.conv, "Время тихого вечера")
        rig.service.locations.select("manual", "dorm")
        await ask(rig.conv, "Время тихого вечера")
        rig.service.locations.select("manual", "home")
        await ask(rig.conv, "Время тихого вечера")
        assert [c.kwargs["level"] for c in rig.brightness.await_args_list] == [10, 20, 30]
        assert all(h["status"] == "confirmed" for h in rig.service.store.history(scenario.id))
        assert not rig.conv.llm.requests

    asyncio.run(run())


def test_missing_place_uses_general_but_disabled_version_is_never_bypassed(rig):
    scenario = independent(rig)
    rig.service.locations.save(Place(id="work", name="Работа"))
    rig.service.locations.select("manual", "work")
    effective, _ = rig.service.engine.select(scenario)
    assert effective.actions == scenario.actions
    rig.service.locations.select("manual", "home")
    scenario.variants[1].enabled = False
    with pytest.raises(ValueError, match="выключена"):
        rig.service.engine.select(scenario)
    scenario.general_enabled = False
    rig.service.locations.select("auto")
    with pytest.raises(ValueError, match="Место неизвестно"):
        rig.service.engine.select(scenario)
    with pytest.raises(ValueError, match="нет такой версии"):
        rig.service.engine.select(scenario, "f" * 32)


def test_preview_general_and_local_matches_execution_order_without_any_effect(rig, monkeypatch):
    scenario = independent(rig, inter_block_delay_s=0.5)
    scenario.variants[1].actions.append(Block(kind="delay", seconds=30))
    rig.service.locations.select("manual", "home")
    no_sleep = AsyncMock(side_effect=AssertionError("Simulation must never wait"))
    monkeypatch.setattr("backend.automation.engine.asyncio.sleep", no_sleep)
    general = asyncio.run(preview(rig.service.engine, scenario, variant="general"))
    local = asyncio.run(preview(rig.service.engine, scenario, variant=scenario.variants[1].id))
    assert [s["parameters"]["level"] for s in general["steps"]] == [10]
    assert local["steps"][0]["parameters"]["level"] == 30
    assert local["steps"][1]["delay_before_s"] == 0.5
    assert local["steps"][0]["device"] == "mac"
    assert local["timeout_s"] == 3600
    no_sleep.assert_not_awaited()
    rig.brightness.assert_not_awaited()
    assert rig.service.store.history(scenario.id) == []


def test_preview_reports_invalid_parameters_and_absent_device_in_place(rig):
    scenario = independent(
        rig,
        variants=[],
        actions=[
            Block(capability="mac.brightness", parameters={"level": 101}),
            Block(capability="mac.brightness", parameters={"level": 20}),
        ],
    )
    rig.service.engine.device_state["mac"] = {"connected": False}
    plan = asyncio.run(preview(rig.service.engine, scenario))
    assert plan["errors"]
    assert plan["steps"][0]["status"] == "invalid"
    assert plan["steps"][1]["availability"] == "unavailable"
    rig.brightness.assert_not_awaited()


def test_legacy_json_hydration_never_rewrites_source_and_revision_is_a_backup(rig, tmp_path):
    scenario = independent(rig)
    raw = scenario.model_dump()
    for key in (
        "version_policy",
        "general_enabled",
        "inter_block_delay_s",
        "show_status",
        "error_policy",
        "timeout_s",
    ):
        raw.pop(key)
    original = json.dumps(raw, ensure_ascii=False)
    rig.memory.db.execute("UPDATE scenarios SET data=? WHERE id=?", (original, scenario.id))
    rig.memory.db.execute(
        "UPDATE scenario_revisions SET data=? WHERE scenario_id=?", (original, scenario.id)
    )
    rig.memory.db.commit()
    old = rig.service.store.get(scenario.id)
    assert old.version_policy == "append"
    assert old.timeout_s is None
    rig.service.locations.select("manual", "home")
    effective, _ = rig.service.engine.select(old)
    assert [b.parameters["level"] for b in effective.actions] == [10, 30]
    assert (
        rig.memory.db.execute("SELECT data FROM scenarios WHERE id=?", (old.id,)).fetchone()[0]
        == original
    )
    saved = rig.service.store.save(
        old.model_copy(
            update={
                "version_policy": "fallback",
                "inter_block_delay_s": 0.5,
                "show_status": False,
                "error_policy": "continue",
                "timeout_s": 60,
            }
        )
    )
    assert rig.service.store.revision(old.id, old.revision).version_policy == "append"
    assert rig.service.store.get(saved.id) == saved
    restored = rig.service.store.restore(saved.id, old.revision, saved.revision)
    assert restored.version_policy == "append"
    assert restored.variants == old.variants
    # Reopen a separate database through the actual store, including all new fields.
    memory = MemoryStore(tmp_path / "editor.sqlite")
    try:
        store = ScenarioStore(memory)
        copied = store.save(saved.model_copy(update={"id": "", "revision": 0}))
        assert store.get(copied.id).model_dump() == copied.model_dump()
    finally:
        memory.close()


def test_reordered_saved_document_runs_in_exact_order_and_honours_spacing(rig, monkeypatch):
    scenario = independent(
        rig,
        variants=[],
        inter_block_delay_s=0.25,
        actions=[Block(capability="mac.brightness", parameters={"level": n}) for n in [30, 10, 20]],
    )
    waits = AsyncMock()
    monkeypatch.setattr("backend.automation.engine.asyncio.sleep", waits)
    asyncio.run(ask(rig.conv, "Время тихого вечера"))
    assert [c.kwargs["level"] for c in rig.brightness.await_args_list] == [30, 10, 20]
    assert [c.args for c in waits.await_args_list] == [(0.25,), (0.25,)]
    assert [s["path"] for s in rig.service.store.history(scenario.id)[0]["steps"]] == [
        "1",
        "2",
        "3",
    ]


def test_timeout_cancels_wait_before_later_device_step(rig, monkeypatch):
    independent(
        rig,
        variants=[],
        timeout_s=1,
        actions=[
            Block(kind="delay", seconds=30),
            Block(capability="mac.brightness", parameters={"level": 10}),
        ],
    )

    async def waiting(_seconds):
        await asyncio.Event().wait()

    monkeypatch.setattr("backend.automation.engine.asyncio.sleep", waiting)
    asyncio.run(ask(rig.conv, "Время тихого вечера"))
    rig.brightness.assert_not_awaited()
    run = rig.service.store.history()[0]
    assert run["status"] == "interrupted"
    assert run["steps"][0]["status"] == "interrupted"
    assert "истекло" in run["error"]


def test_global_continue_records_failure_and_does_not_bypass_confirmation(rig):
    independent(
        rig,
        variants=[],
        error_policy="continue",
        actions=[
            Block(capability="mac.brightness", parameters={"level": 20}),
            Block(capability="mac.brightness", parameters={"level": 30}),
        ],
    )
    rig.brightness.side_effect = [{"error": "Нет ответа"}, {"level": 30}]
    asyncio.run(ask(rig.conv, "Время тихого вечера"))
    run = rig.service.store.history()[0]
    assert run["status"] == "failed"
    assert [s["status"] for s in run["steps"]] == ["failed", "confirmed"]
    sensitive = independent(
        rig,
        phrases=["Особое выключение"],
        variants=[],
        confirmation="always",
        error_policy="continue",
        actions=[Block(capability="mac.power", parameters={"action": "shutdown"})],
    )
    asyncio.run(ask(rig.conv, "Особое выключение"))
    rig.power.assert_not_awaited()
    assert not rig.service.store.history(sensitive.id)


def test_if_and_sequence_receive_actual_receipts_matching_simulation_paths(rig):
    scenario = independent(
        rig,
        variants=[],
        actions=[
            Block(
                kind="sequence",
                children=[
                    Block(
                        kind="if",
                        condition=Condition(source="time", key="hour", operator="gt", value=-1),
                        children=[Block(capability="mac.brightness", parameters={"level": 15})],
                    )
                ],
            )
        ],
    )
    plan = asyncio.run(preview(rig.service.engine, scenario))
    asyncio.run(ask(rig.conv, "Время тихого вечера"))
    steps = rig.service.store.history(scenario.id)[0]["steps"]
    assert [s["path"] for s in steps] == [s["path"] for s in plan["steps"]]
    assert all(s["status"] == "confirmed" for s in steps)


def test_dispatch_keeps_settings_and_reports_selected_version_after_manual_run(rig):
    scenario = independent(rig)
    rig.service.bus = HudBus()
    queue = rig.service.bus.subscribe()
    rig.service.conv = rig.conv

    async def run():
        await rig.service.dispatch(
            {
                "op": "run",
                "id": scenario.id,
                "revision": scenario.revision,
                "variant": scenario.variants[0].id,
            }
        )
        rig.brightness.assert_awaited_once_with(level=20)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert events[-1]["runs"][0]["variant_name"] == "Общага"
        assert (
            next(s for s in events[-1]["scenarios"] if s["id"] == scenario.id)["version_policy"]
            == "fallback"
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    "changes",
    [
        {"inter_block_delay_s": -1},
        {"timeout_s": 0},
        {"timeout_s": 3601},
        {"error_policy": "ignore"},
    ],
)
def test_execution_options_are_bounded(changes):
    with pytest.raises(ValidationError):
        Scenario(name="Проверка", **changes)


def test_editor_websocket_save_reopen_and_simulate_without_device_effects(rig, tmp_path):
    from fastapi.testclient import TestClient

    from backend.hud.server import allowed_origins, create_app

    bus = HudBus()
    rig.service.bus, rig.service.conv = bus, rig.conv
    bus.handle("automations", rig.service.request)
    client = TestClient(create_app(bus, allowed_origins("127.0.0.1", 8765), tmp_path))
    draft = Scenario(
        name="Из редактора",
        phrases=["Моя проверка редактора"],
        version_policy="fallback",
        inter_block_delay_s=0.5,
        show_status=False,
        timeout_s=60,
        actions=[Block(capability="mac.brightness", parameters={"level": 15})],
        variants=[Variant(name="Дом", place_id="home", actions=[Block(kind="delay", seconds=2)])],
    )
    with client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:8765"}) as ws:
        assert ws.receive_json()["type"] == "transcript"
        ws.send_json(
            {"type": "automations", "value": {"op": "save", "scenario": draft.model_dump()}}
        )
        reply = ws.receive_json()
        assert reply["editor_version"] == 2
        saved = next(s for s in reply["scenarios"] if s["id"] == reply["saved"])
        assert saved["timeout_s"] == 60 and saved["inter_block_delay_s"] == 0.5
        ws.send_json(
            {
                "type": "automations",
                "value": {
                    "op": "preview",
                    "scenario": saved,
                    "variant": saved["variants"][0]["id"],
                },
            }
        )
        plan = ws.receive_json()["preview"]
        assert plan["selection"]["variant_name"] == "Дом"
        assert len(plan["steps"]) == 1 and plan["steps"][0]["seconds"] == 2
        assert rig.service.store.get(saved["id"]).show_status is False
    rig.brightness.assert_not_awaited()
    rig.power.assert_not_awaited()
    assert not rig.service.store.history()
