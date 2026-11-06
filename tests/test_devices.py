"""Real device registry, guarded tool routing and SQLite; hardware transports mocked."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from bleak.exc import BleakError

from backend.automation.models import Block, Scenario
from backend.brain.conversation import Conversation
from backend.context import RequestContext, request_scope
from backend.hud.bus import HudBus
from backend.led import LEDState
from backend.mac import MacDesktop
from backend.mac_agent import LocalMacAgent
from backend.memory.store import MemoryStore
from backend.tools import build_registry
from tests.test_critical_fixes import NoModel
from tests.test_dialogue_context import ask
from tests.test_led import rig as rig


@pytest.fixture
def devices(rig, tmp_path):
    rig.settings.led.settle_s = 0
    rig.strip._save(
        LEDState(
            identity=rig.strip.identity, confirmed={"on", "off", "rgb", "brightness"}, brightness=50
        )
    )
    store = MemoryStore(tmp_path / "devices.db")
    desktop = MacDesktop(rig.settings)
    desktop.bridge = SimpleNamespace(
        request=AsyncMock(side_effect=lambda op, **kw: {"level": kw.get("level", 55)})
    )
    agent = LocalMacAgent()
    led = rig.strip.tool()
    led.agent = agent
    agent.register(led.name, led._handler)
    tools = build_registry(rig.settings, store=store, desktop=desktop, mac_agent=agent, extra=[led])
    handlers = {}
    for name, result in [
        ("set_volume", {"ok": True, "volume_percent": 30, "muted": False}),
        ("mac_power", {"submitted": True}),
        ("media_control", {"ok": True, "state": "paused"}),
        ("get_system_status", {"battery": {"percent": 80, "on_power_adapter": True}}),
        ("now_playing", {"state": "paused"}),
    ]:
        handler = AsyncMock(return_value=result)
        tools._tools[name].handler = handler
        agent.register(name, handler)
        handlers[name] = handler
    conv = Conversation(rig.settings, NoModel(), store, tools=tools)
    bus, events = HudBus(), []
    publish = bus.publish
    bus.publish = lambda event: (events.append(event), publish(event))[-1]
    tools.devices.bind(bus, conv)
    value = SimpleNamespace(
        **vars(rig),
        tools=tools,
        service=tools.devices,
        store=store,
        conv=conv,
        bus=bus,
        events=events,
        handlers=handlers,
        desktop=desktop,
        agent=agent,
    )
    yield value
    store.close()


async def request(r, **payload):
    payload.setdefault("request_id", uuid4().hex)
    await r.service.handle(payload)
    events = [
        e
        for e in r.events
        if e.get("operation_id") == payload["request_id"]
        and (e.get("result") or e.get("error") or e.get("saved") or e.get("message"))
    ]
    return events[-1] if events else r.events[-1]


def test_catalogue_startup_has_no_hardware_reads_or_fictitious_state(devices):
    data = devices.service.snapshot()
    assert {d["kind"] for d in data["items"]} == {"led_strip", "mac"}
    assert all(
        d["connection"] == "unknown" and not d["state"] and d["last_contact"] is None
        for d in data["items"]
    )
    devices.factory.assert_not_called()
    devices.desktop.bridge.request.assert_not_awaited()
    assert (
        devices.service.features(devices.service.store.get("led-configured"))["warm_white"]
        == "rgb_approximation"
    )


def test_rename_room_location_favorites_and_delete_survive_restart(devices):
    async def run():
        r = devices
        await request(
            r,
            op="update",
            id="led-configured",
            revision=1,
            name="Свет у стола",
            place_id="dorm",
            room="Спальня",
        )
        await request(
            r,
            op="favorites",
            id="led-configured",
            revision=2,
            colors=["#ff1020", "#FF1020", "#0000FF"],
        )
        from backend.devices.store import DeviceStore

        reopened = DeviceStore(r.store).get("led-configured")
        assert (reopened.name, reopened.place_id, reopened.room) == (
            "Свет у стола",
            "dorm",
            "Спальня",
        )
        assert reopened.favorites == ["#FF1020", "#0000FF"]
        assert "error" in await request(r, op="delete", id=reopened.id, revision=1)
        assert "error" not in await request(r, op="delete", id=reopened.id, revision=3)
        r.service.seed()
        assert not any(d.kind == "led_strip" for d in r.service.store.list())
        r.factory.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "socket", "endpoint": "local"},
        {"kind": "led_strip", "endpoint": "not-a-uuid"},
        {"kind": "led_strip", "endpoint": "https://token@example.test"},
        {"kind": "mac", "endpoint": "another-computer"},
        {"kind": "led_strip", "endpoint": str(uuid4()), "name": ""},
        {"kind": "led_strip", "endpoint": str(uuid4()), "place_id": "missing"},
    ],
)
def test_invalid_add_never_connects_or_saves(devices, payload):
    async def run():
        before = len(devices.service.store.list())
        assert "error" in await request(
            devices,
            op="add",
            name=payload.get("name", "Новая"),
            **{k: v for k, v in payload.items() if k != "name"},
        )
        assert len(devices.service.store.list()) == before
        devices.factory.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize(
    "parameters",
    [
        {"power": True},
        {"power": False},
        {"color": "#FF0020"},
        {"brightness": 30},
        {"color": "#FFFFFF", "brightness": 100},
    ],
)
def test_manual_led_uses_existing_guarded_transport_and_truthful_state(devices, parameters):
    async def run():
        r = devices
        answer = await request(
            r, op="act", id="led-configured", action="led", parameters=parameters
        )
        assert answer["result"]["result_status"] == "unverified" and "error" not in answer["result"]
        d = r.service.store.get("led-configured")
        assert not d.state and d.last_contact and d.result_status == "unverified"
        assert all(d.sent[k] == v for k, v in parameters.items())
        assert r.factory.call_count == (3 if len(parameters) == 2 else 1)
        assert d.revision == 1  # Device telemetry does not invalidate metadata edits.

    asyncio.run(run())


def test_manual_voice_and_scenario_share_led_state_and_same_handler(devices):
    async def run():
        r = devices
        await request(r, op="act", id="led-configured", action="led", parameters={"brightness": 30})
        assert r.conv.controls.device == "led_strip"
        await ask(r.conv, "Сделай её поярче")
        assert r.strip._load().brightness == 40
        assert r.service.store.get("led-configured").sent["brightness"] == 40
        scenario = r.tools.automations.store.save(
            Scenario(
                name="Вечер",
                phrases=["Пора включать вечерний свет"],
                actions=[Block(capability="led.set", parameters={"brightness": 65})],
            )
        )
        await ask(r.conv, scenario.phrases[0])
        assert r.strip._load().brightness == 65
        assert r.service.store.get("led-configured").sent["brightness"] == 65
        await request(r, op="act", id="led-configured", action="led", parameters={"brightness": 20})
        await ask(r.conv, "А теперь чуть темнее")
        assert r.strip._load().brightness == 10
        assert not r.service.store.get("led-configured").state

    asyncio.run(run())


def test_wrong_city_blocks_manual_voice_scenario_and_direct_night_write(devices):
    async def run():
        r = devices
        await request(r, op="update", id="led-configured", revision=1, place_id="home")
        await request(r, op="location", place_id="dorm")
        assert not next(d for d in r.service.snapshot()["items"] if d["id"] == "led-configured")[
            "usable"
        ]
        assert "error" in await request(
            r, op="act", id="led-configured", action="led", parameters={"brightness": 30}
        )
        assert "другому" in await ask(r.conv, "Сделай яркость ленты 30%")
        scenario = r.tools.automations.store.save(
            Scenario(
                name="Домашний свет",
                phrases=["Включи домашний свет"],
                actions=[Block(capability="led.set", parameters={"power": True})],
            )
        )
        assert "ошиб" in (await ask(r.conv, scenario.phrases[0])).lower()
        from backend.tools.registry import ToolError

        with pytest.raises(ToolError, match="другому"):
            await r.strip.execute(action="off")
        r.factory.assert_not_called()
        await request(r, op="location", place_id="home")
        assert (
            "error"
            not in (
                await request(
                    r, op="act", id="led-configured", action="led", parameters={"power": True}
                )
            )["result"]
        )

    asyncio.run(run())


def test_failed_bluetooth_does_not_claim_success_and_other_devices_still_work(devices):
    async def run():
        r = devices
        r.factory.side_effect = lambda *a, **kw: SimpleNamespace(
            connect=AsyncMock(side_effect=BleakError("private transport text")),
            disconnect=AsyncMock(),
        )
        result = (
            await request(
                r, op="act", id="led-configured", action="led", parameters={"brightness": 30}
            )
        )["result"]
        assert result["result_status"] == "failed" and "private transport" not in result["error"]
        assert r.service.store.get("led-configured").last_contact is None
        assert r.service.connections["led-configured"] == "error"
        result = (
            await request(
                r, op="act", id="mac-local", action="brightness", parameters={"level": 42}
            )
        )["result"]
        assert result["result_status"] == "success"
        assert r.service.store.get("mac-local").state["brightness"] == 42

    asyncio.run(run())


@pytest.mark.parametrize(
    "action,parameters",
    [
        ("led", {"brightness": 101}),
        ("led", {"brightness": True}),
        ("led", {"power": False, "color": "#FF0000"}),
        ("led", {"color": "red"}),
        ("led", {"shell": "rm"}),
        ("volume", {"level": 50}),
        ("shutdown", {}),
    ],
)
def test_invalid_or_unsupported_led_actions_have_no_effect(devices, action, parameters):
    async def run():
        assert "error" in await request(
            devices, op="act", id="led-configured", action=action, parameters=parameters
        )
        devices.factory.assert_not_called()

    asyncio.run(run())


def test_fresh_mac_lock_confirmation_is_required_and_cannot_confirm_voice_shutdown(devices):
    async def run():
        r = devices
        result = (await request(r, op="act", id="mac-local", action="lock", parameters={}))[
            "result"
        ]
        assert result["result_status"] == "requires_confirmation"
        r.handlers["mac_power"].assert_not_awaited()
        pending = r.tools.confirmations.pending[(r.conv.device.device_id, r.conv.device.session_id)]
        await request(r, op="confirm", id="mac-local", code=pending.code)
        r.handlers["mac_power"].assert_awaited_once_with(action="lock")
        await request(r, op="act", id="mac-local", action="lock", parameters={})
        with request_scope(RequestContext(r.conv.device, "Выключи Mac")):
            await r.tools.call("mac_power", {"action": "shutdown"})
        pending = r.tools.confirmations.pending[(r.conv.device.device_id, r.conv.device.session_id)]
        assert "error" in await request(r, op="confirm", id="mac-local", code=pending.code)
        assert r.handlers["mac_power"].await_count == 1

    asyncio.run(run())


def test_conflicts_duplicates_and_metadata_changes_do_not_replay_writes(devices):
    async def run():
        r = devices
        started, release = asyncio.Event(), asyncio.Event()
        original = r.factory.side_effect

        def create(*a, **kw):
            client = original(*a, **kw)

            async def connect():
                started.set()
                await release.wait()

            client.connect = AsyncMock(side_effect=connect)
            return client

        r.factory.side_effect = create
        nonce = uuid4().hex
        task = asyncio.create_task(
            request(
                r,
                op="act",
                id="led-configured",
                action="led",
                parameters={"brightness": 30},
                request_id=nonce,
            )
        )
        await started.wait()
        assert "error" in await request(
            r, op="act", id="led-configured", action="led", parameters={"brightness": 40}
        )
        assert "error" in await request(r, op="delete", id="led-configured", revision=1)
        # A separate Mac action remains usable while BLE waits.
        assert (
            await request(
                r, op="act", id="mac-local", action="brightness", parameters={"level": 20}
            )
        )["result"]["result_status"] == "success"
        release.set()
        await task
        await request(
            r,
            op="act",
            id="led-configured",
            action="led",
            parameters={"brightness": 30},
            request_id=nonce,
        )
        assert r.factory.call_count == 1
        assert "error" in await request(
            r,
            op="act",
            id="led-configured",
            action="led",
            parameters={"brightness": 40},
            request_id=nonce,
        )

    asyncio.run(run())


def test_new_led_requires_explicit_probe_and_retains_firmware_evidence(devices):
    async def run():
        r = devices
        endpoint = str(uuid4())
        saved = await request(
            r,
            op="add",
            kind="led_strip",
            endpoint=endpoint,
            name="Новая лента",
            place_id="general",
            room="Стол",
        )
        identifier = saved["saved"]
        r.factory.assert_not_called()
        assert (
            "error"
            in (
                await request(r, op="act", id=identifier, action="led", parameters={"power": True})
            )["result"]
        )
        r.factory.assert_not_called()
        assert "error" in await request(
            r, op="probe_led", id=identifier, parameters={"power": True}, consent=False
        )
        # An unnamed controller no longer inherits the primary strip's driver.
        assert "error" in await request(
            r, op="probe_led", id=identifier, parameters={"power": True}, consent=True
        )
        await request(r, op="ble_profile", id=identifier, driver="elk-bledob-legacy", consent=True)
        probe = (
            await request(
                r, op="probe_led", id=identifier, parameters={"power": True}, consent=True
            )
        )["result"]
        assert probe["probe_id"]
        assert not r.service.features(r.service.store.get(identifier))["power_on"]
        await request(
            r, op="confirm_probe", id=identifier, probe_id=probe["probe_id"], observed=True
        )
        assert r.service.features(r.service.store.get(identifier))["power_on"]
        assert not r.service.features(r.service.store.get(identifier))["color"]
        await request(r, op="act", id=identifier, action="led", parameters={"power": True})
        assert r.factory.call_args.args[0] == endpoint
        r.conv.close_interaction()
        with request_scope(RequestContext(r.conv.device, "Включи ленту")):
            result = json.loads(await r.tools.call("led_control", {"command": "Включи ленту"}))
        assert result["result_status"] == "failed"  # Two local strips need an explicit choice.

    asyncio.run(run())


def test_restart_keeps_last_sent_and_metadata_but_never_live_connection(devices):
    async def run():
        r = devices
        await request(
            r,
            op="act",
            id="led-configured",
            action="led",
            parameters={"color": "#FF0020", "brightness": 35},
        )
        from backend.devices.service import Devices

        # The existing instance's bound dispatcher is not a physical LEDStrip.
        original = r.tools._tools["led_control"]._handler
        r.tools._tools["led_control"].handler = r.strip.command
        restarted = Devices(r.tools, r.settings, r.store)
        r.tools._tools["led_control"].handler = original
        item = next(d for d in restarted.snapshot()["items"] if d["id"] == "led-configured")
        assert item["sent"]["brightness"] == 35 and item["sent"]["color"] == "#FF0020"
        assert not item["state"] and item["connection"] == "unknown" and item["last_contact"]

    asyncio.run(run())


def test_manual_discovery_and_permission_boundaries(devices, monkeypatch):
    async def run():
        r = devices
        discover = AsyncMock(
            return_value={"candidates": [{"name": "ELK-BLEDOB", "address": str(uuid4())}]}
        )
        monkeypatch.setattr("backend.led.LEDStrip.discover", discover)
        r.service.snapshot()
        discover.assert_not_awaited()
        event = await request(r, op="discover", kind="led_strip")
        assert len(event["candidates"]) == 1 and "address" not in event["candidates"][0]
        await request(
            r,
            op="add",
            token=event["candidates"][0]["token"],
            name="Локальная лента",
            place_id="dorm",
            room="У стола",
        )
        discover.assert_awaited_once()
        r.conv.device = replace(r.conv.device, authenticated=False)
        assert "error" in await request(r, op="discover", kind="led_strip")
        assert discover.await_count == 1

    asyncio.run(run())


def test_disabled_tool_and_foreign_agent_do_not_allow_gui_probe(devices):
    async def run():
        r = devices
        r.store.set_preference("settings", "access", {"disabled_tools": ["led_control"]})
        r.tools._tools["led_control"].settings_store = r.store
        assert "error" in await request(
            r, op="probe_led", id="led-configured", parameters={"power": True}, consent=True
        )
        assert "error" in await request(
            r, op="act", id="led-configured", action="led", parameters={"power": True}
        )
        r.factory.assert_not_called()

    asyncio.run(run())


def test_external_display_manual_and_voice_share_exact_uuid_without_mac_fallback(
    devices, monkeypatch
):
    async def run():
        r = devices
        endpoint = str(uuid4())
        await request(
            r, op="add", kind="external_display", endpoint=endpoint, name="Домашний экран"
        )
        identifier = next(d.id for d in r.service.store.list() if d.kind == "external_display")
        inventory = AsyncMock(return_value=[{"uuid": endpoint, "builtin": False, "name": "Экран"}])
        write = AsyncMock(
            side_effect=lambda uid, control, level: {"actual": level / 100, "previous": 0.5}
        )
        monkeypatch.setattr("backend.night.BetterDisplay.inventory", inventory)
        monkeypatch.setattr("backend.night.BetterDisplay.set_level", write)
        result = (
            await request(r, op="act", id=identifier, action="brightness", parameters={"level": 35})
        )["result"]
        assert result["result_status"] == "success"
        write.assert_awaited_once_with(endpoint, r.settings.night.external_control, 35)
        assert r.conv.controls.device == "external_display"
        await ask(r.conv, "Теперь яркость 40%")
        assert write.await_args.args == (endpoint, r.settings.night.external_control, 40)
        r.desktop.bridge.request.assert_not_awaited()
        assert r.service.store.get(identifier).state["brightness"] == 40
        result = await request(
            r, op="act", id=identifier, action="volume", parameters={"level": 20}
        )
        assert "error" in result and write.await_count == 2

    asyncio.run(run())


def test_dynamic_led_registration_survives_restart_without_config_edits(tmp_path, monkeypatch):
    async def run():
        from backend.config import Settings

        settings = Settings(language="ru-RU", data_dir=tmp_path)
        settings.led.enabled = False
        memory = MemoryStore(tmp_path / "memory.db")
        tools = build_registry(settings, store=memory)
        conv = Conversation(settings, NoModel(), memory, tools=tools)
        service = tools.devices
        service.bind(HudBus(), conv)
        try:
            assert "led_control" not in tools
            await service.handle(
                {
                    "op": "add",
                    "kind": "led_strip",
                    "endpoint": str(uuid4()),
                    "name": "Новая лента",
                    "request_id": uuid4().hex,
                }
            )
            assert "led_control" in tools and "led.set" in tools.automations.capabilities.items
            restarted = build_registry(settings, store=memory)
            assert (
                "led_control" in restarted
                and len([d for d in restarted.devices.store.list() if d.kind == "led_strip"]) == 1
            )
        finally:
            memory.close()

    asyncio.run(run())


def test_mute_mismatch_is_not_success(devices):
    async def run():
        result = (
            await request(
                devices, op="act", id="mac-local", action="volume", parameters={"mute": True}
            )
        )["result"]
        assert result["result_status"] == "failed"

    asyncio.run(run())


def test_observation_failure_does_not_repeat_completed_ble_write(devices, monkeypatch):
    async def run():
        import sqlite3

        r = devices
        monkeypatch.setattr(
            r.service.store,
            "observe",
            lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("private db path")),
        )
        result = (
            await request(
                r, op="act", id="led-configured", action="led", parameters={"brightness": 30}
            )
        )["result"]
        assert result["written"] and result["result_status"] == "unverified"
        assert r.factory.call_count == 1 and r.strip._load().brightness == 30
        assert r.service.store.get("led-configured").sent == {}

    asyncio.run(run())


def test_websocket_device_action_round_trip_has_no_scenario_or_llm(devices, tmp_path):
    from fastapi.testclient import TestClient

    from backend.hud.server import allowed_origins, create_app

    r = devices
    client = TestClient(create_app(r.bus, allowed_origins("127.0.0.1", 8765), tmp_path))
    with client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:8765"}) as ws:
        assert ws.receive_json()["type"] == "devices"
        assert ws.receive_json()["type"] == "transcript"
        request_id = uuid4().hex
        ws.send_json(
            {
                "type": "devices",
                "value": {
                    "op": "act",
                    "id": "led-configured",
                    "action": "led",
                    "parameters": {"brightness": 28},
                    "request_id": request_id,
                },
            }
        )
        for _ in range(20):
            event = ws.receive_json()
            if event.get("operation_id") == request_id and event.get("result"):
                assert event["result"]["result_status"] == "unverified"
                break
        else:
            pytest.fail("Device result was not returned to the HUD")
        assert r.service.store.get("led-configured").sent["brightness"] == 28
        assert r.tools.automations.store.history() == []


def test_device_service_runs_without_scenario_service_or_editor(rig):
    async def run():
        from backend.devices.service import Devices
        from backend.tools.registry import ToolRegistry

        rig.strip._save(
            LEDState(identity=rig.strip.identity, confirmed={"brightness"}, brightness=50)
        )
        memory = MemoryStore(":memory:")
        agent = LocalMacAgent()
        tool = rig.strip.tool()
        tool.agent = agent
        agent.register(tool.name, tool._handler)
        tools = ToolRegistry([tool])
        tools.mac_agent = agent
        tools.devices = Devices(tools, rig.settings, memory)
        conv = Conversation(rig.settings, NoModel(), memory, tools=tools)
        tools.devices.bind(HudBus(), conv)
        try:
            assert tools.automations is None
            await tools.devices.handle(
                {
                    "op": "act",
                    "id": "led-configured",
                    "action": "led",
                    "parameters": {"brightness": 32},
                    "request_id": uuid4().hex,
                }
            )
            assert tools.devices.store.get("led-configured").sent["brightness"] == 32
            assert (
                memory.db.execute("SELECT 1 FROM sqlite_master WHERE name='scenarios'").fetchone()
                is None
            )
        finally:
            memory.close()

    asyncio.run(run())
