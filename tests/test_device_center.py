"""Device centre contracts. All hardware, network and macOS calls are mocked."""

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from backend.automation.models import Block, Scenario
from backend.devices.adapters import WLEDAdapter, local_ip
from backend.devices.models import Device
from backend.devices.store import DeviceStore
from tests.test_devices import devices as devices
from tests.test_devices import request
from tests.test_dialogue_context import ask
from tests.test_led import rig as rig


def test_additive_migration_and_metadata(devices):
    r = devices
    original = r.service.store.get("led-configured")
    updated = r.service.store.save(
        original.model_copy(update={"aliases": ["лента"], "favorite": True})
    )
    again = DeviceStore(r.store)
    assert again.get(original.id) == updated
    assert [r[0] for r in r.store.db.execute("SELECT version FROM device_schema")] == [2]
    assert original.state == {}


@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1",
        "169.254.1.1",
        "0.0.0.0",
        "224.0.0.1",
        "8.8.8.8",
        "192.0.2.1",
        "::1",
        "http://192.168.1.1",
        "192.168.1.1:80",
        "user:password@192.168.1.1",
    ],
)
def test_only_explicit_private_network_addresses(value):
    with pytest.raises(ValueError):
        local_ip(value)


@pytest.mark.parametrize("value", ["192.168.1.50", "10.1.2.3", "172.16.0.2", "fd00::1234"])
def test_supported_local_addresses(value):
    assert local_ip(value) == value


def test_groups_have_revisions_validate_members_and_cleanup(devices):
    store = devices.service.store
    room = store.save_collection("room", {"name": "Спальня", "place_id": "dorm"})
    group = store.save_collection(
        "group",
        {
            "name": "Весь свет",
            "place_id": "dorm",
            "room": room.name,
            "device_ids": ["led-configured", "led-configured"],
        },
    )
    assert group.device_ids == ["led-configured"]
    with pytest.raises(ValueError):
        store.save_collection("group", {**group.model_dump(), "revision": 0})
    with pytest.raises(ValueError):
        store.save_collection("group", {"name": "Неизвестная", "device_ids": ["missing"]})
    device = store.get("led-configured")
    store.delete(device.id, device.revision)
    assert store.collections("group")[0].device_ids == []


def test_output_selection_requires_fresh_inventory_and_system_ack(devices):
    async def run():
        r = devices
        r.service.native.run = AsyncMock(
            return_value={"outputs": [{"uid": "builtin", "name": "Mac", "active": True}]}
        )
        denied = await request(r, op="audio_select", uid="not-present")
        assert "error" in denied
        assert [c.args[0] for c in r.service.native.run.await_args_list] == ["outputs"]
        r.service.native.run = AsyncMock(
            side_effect=[
                {"outputs": [{"uid": "builtin"}]},
                {"outputs": [{"uid": "builtin", "active": False}], "device_state_confirmed": False},
            ]
        )
        result = await request(r, op="audio_select", uid="builtin")
        assert result["result"]["result_status"] == "unverified"
        r.service.native.run = AsyncMock(
            side_effect=[
                {"outputs": [{"uid": "builtin"}]},
                {"outputs": [{"uid": "builtin", "active": True}], "device_state_confirmed": True},
            ]
        )
        result = await request(r, op="audio_select", uid="builtin")
        assert result["result"]["result_status"] == "success"

    asyncio.run(run())


def test_airplay_discovery_never_creates_devices_or_claims_streaming(devices):
    async def run():
        r = devices
        before = len(r.service.store.list())
        service = {
            "name": "Test Receiver",
            "endpoint": "Test Receiver|_airplay._tcp|local.",
            "service": "_airplay._tcp",
        }
        r.service.native.run = AsyncMock(return_value={"services": [service]})
        candidates = await r.service.discover("airplay")
        assert len(r.service.store.list()) == before
        checked = await request(r, op="check", token=candidates[0]["token"])
        assert "Bonjour" in checked["checked"]["message"]
        added = await request(
            r, op="add", token=candidates[0]["token"], name="Приёмник", place_id="general"
        )
        device = r.service.store.get(added["saved"])
        assert device.state == {}
        assert r.service.features(device)["playback"] is False
        assert r.service.features(device)["airplay_audio"] is True
        r.service.native.run = AsyncMock(
            return_value={"submitted": True, "message": "Выбери приёмник в macOS"}
        )
        result = await request(r, op="act", id=device.id, action="airplay_audio", parameters={})
        assert result["result"]["result_status"] == "unverified"
        assert r.service.store.get(device.id).state == {}

    asyncio.run(run())


def test_group_actions_report_partial_failure_without_replay(devices):
    async def run():
        r = devices
        group = r.service.store.save_collection(
            "group", {"name": "Свет и Mac", "device_ids": ["led-configured", "mac-local"]}
        )
        r.service.update_catalogue()
        event = await request(
            r, op="act_group", id=group.id, action="led", parameters={"power": False}
        )
        assert event["result"]["result_status"] == "partial_success"
        assert len(event["result"]["results"]) == 2
        assert len(r.service.store.history("led-configured")) == 1
        assert len(r.service.store.history("mac-local")) == 1
        assert r.service.store.history("mac-local")[0]["result_status"] == "failed"
        assert r.service.store.get("mac-local").state == {}

    asyncio.run(run())


def test_dynamic_scenario_actions_use_registry_and_fixed_target(devices):
    r = devices
    catalogue = r.tools.automations.capabilities
    on = catalogue.get("device.led-configured.on")
    assert on.arguments({}) == {"target": "led-configured", "action": "led", "power": True}
    assert catalogue.get("device.mac-local.volume").arguments({"level": 34}) == {
        "target": "mac-local",
        "action": "volume",
        "level": 34,
    }
    with pytest.raises(ValueError):
        on.arguments({"target": "mac-local"})

    # Engine's normal authenticated context and confirmation are covered in test_devices.
    # Validate the same typed block without executing a new physical scenario here.
    catalogue.validate(Scenario(name="Свет", actions=[Block(capability=on.id)]))


def test_voice_names_synonyms_compound_commands_and_ambiguity(devices):
    r = devices
    device = r.service.store.get("led-configured")
    r.service.store.save(
        device.model_copy(update={"name": "Свет у стола", "aliases": ["подсветку"]})
    )
    plan = r.service.voice_plan("Сделай свет у стола розовым и яркость 50 процентов")
    assert plan and not plan.get("error")
    assert all(c["target"] == device.id for c in plan["commands"])
    assert any(c.get("color") for c in plan["commands"])
    assert any(c.get("brightness") == 50 for c in plan["commands"])
    assert r.service.voice_plan("Выключи подсветку")["commands"][0]["power"] is False
    other = Device(
        name="Другой свет",
        kind="led_strip",
        protocol="ble",
        endpoint=str(uuid4()),
        aliases=["подсветку"],
    )
    r.service.store.save(other)
    assert "error" in r.service.voice_plan("Выключи подсветку")
    assert r.service.voice_plan("Включи музыку на MacBook") is None


def test_named_voice_command_bypasses_llm_and_preserves_unverified_result(devices):
    async def run():
        r = devices
        d = r.service.store.get("led-configured")
        r.service.store.save(d.model_copy(update={"aliases": ["свет у стола"]}))
        result = await ask(r.conv, "Выключи свет у стола")
        assert "подтвержд" in result.casefold() or "отправ" in result.casefold()
        assert r.service.store.get(d.id).state == {}
        assert r.service.store.history(d.id)

    asyncio.run(run())


def test_native_timeout_cancellation_terminates_pending_operation(devices):
    async def run():
        r = devices
        started = asyncio.Event()
        ended = asyncio.Event()

        async def blocked(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()

        r.service.native.run = blocked
        operation_id = uuid4().hex
        r.service.request({"op": "discover", "kind": "airplay", "request_id": operation_id})
        await asyncio.wait_for(started.wait(), 1)
        await request(r, op="cancel", operation_id=operation_id)
        await asyncio.wait_for(ended.wait(), 1)
        await r.service.close()
        assert not r.service.jobs

    asyncio.run(run())


def test_wled_verifies_readback_and_only_writes_documented_fields(monkeypatch):
    requests = []
    state = {"on": True, "bri": 128, "mainseg": 3, "seg": [{"id": 3, "col": [[255, 0, 0]]}]}

    def respond(req):
        requests.append(req)
        if req.method == "POST":
            body = json.loads(req.content)
            assert body == {
                "on": True,
                "bri": 128,
                "seg": [{"id": 3, "col": [[255, 0, 0]], "fx": 0}],
            }
            state["on"] = True
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"info": {"ver": "0.15", "leds": {}}, "state": state})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(respond), **kw)
    )
    device = Device(name="Test WLED", kind="wled", protocol="http", endpoint="192.168.1.50")
    result = asyncio.run(
        WLEDAdapter().execute(device, "led", {"power": True, "brightness": 50, "color": "#FF0000"})
    )
    assert result["device_state_confirmed"] is True
    assert [r.method for r in requests] == ["GET", "POST", "GET"]


def test_cross_location_group_does_not_touch_distant_device(devices):
    async def run():
        r = devices
        d = r.service.store.get("led-configured")
        r.service.store.save(d.model_copy(update={"place_id": "home"}))
        r.service.locations.select("manual", "dorm")
        group = r.service.store.save_collection("group", {"name": "Свет", "device_ids": [d.id]})
        r.service.update_catalogue()
        result = await request(
            r, op="act_group", id=group.id, action="led", parameters={"power": True}
        )
        assert result["result"]["result_status"] == "failed"
        r.factory.assert_not_called()

    asyncio.run(run())


def test_registry_scenario_executes_and_waits_for_led_delivery(devices):
    async def run():
        r = devices
        scenario = r.tools.automations.store.save(
            Scenario(
                name="Реестр света",
                phrases=["Ритуал проверки реестра"],
                actions=[
                    Block(capability="device.led-configured.on"),
                    Block(
                        capability="device.led-configured.brightness", parameters={"brightness": 35}
                    ),
                ],
            )
        )
        await ask(r.conv, scenario.phrases[0])
        assert r.strip._load().brightness == 35
        assert r.service.store.get("led-configured").sent["brightness"] == 35
        assert not r.service.store.get("led-configured").state
        assert len(r.service.store.history("led-configured")) == 2

    asyncio.run(run())


def test_group_request_id_prevents_replayed_physical_commands(devices):
    async def run():
        r = devices
        group = r.service.store.save_collection(
            "group", {"name": "Весь свет", "device_ids": ["led-configured"]}
        )
        r.service.update_catalogue()
        payload = {
            "op": "act_group",
            "id": group.id,
            "action": "led",
            "parameters": {"power": False},
            "request_id": uuid4().hex,
        }
        first = await request(r, **payload)
        count = r.factory.call_count
        repeated = await request(r, **payload)
        assert first["result"] == repeated["result"]
        assert r.factory.call_count == count
        assert len(r.service.store.history("led-configured")) == 1
        assert "error" in await request(r, **{**payload, "parameters": {"power": True}})

    asyncio.run(run())


def test_renaming_room_moves_assigned_devices_transactionally(devices):
    r = devices
    store = r.service.store
    room = store.save_collection("room", {"name": "Спальня", "place_id": "dorm"})
    device = store.get("led-configured")
    store.save(device.model_copy(update={"room": room.name, "place_id": room.place_id}))
    updated = store.save_collection(
        "room", {**room.model_dump(), "name": "Кабинет", "place_id": "home"}
    )
    assert store.get(device.id).room == "Кабинет"
    assert store.get(device.id).place_id == "home"
    store.save_collection("room", updated.model_dump(), delete=True)
    assert store.get(device.id).room == ""


def test_return_sound_to_mac_selects_only_unique_confirmed_builtin(devices):
    async def run():
        r = devices
        outputs = [{"uid": "builtin-test", "transport": "builtin", "active": False}]
        r.service.native.run = AsyncMock(
            side_effect=[
                {"outputs": outputs},
                {"outputs": [{**outputs[0], "active": True}], "device_state_confirmed": True},
            ]
        )
        answer = await ask(r.conv, "Верни звук на Mac")
        assert "подтвердила" in answer
        assert [c.args for c in r.service.native.run.await_args_list] == [
            ("outputs",),
            ("select", "builtin-test"),
        ]
        r.service.native.run = AsyncMock(
            return_value={"outputs": [outputs[0], {**outputs[0], "uid": "other"}]}
        )
        answer = await ask(r.conv, "Верни звук на Mac")
        assert "недоступно" in answer or "не подтверждено" in answer
        assert r.service.native.run.await_count == 1

    asyncio.run(run())


def test_wled_readback_failure_never_replays_or_reports_confirmed_state(monkeypatch):
    calls = []

    def respond(req):
        calls.append(req.method)
        if len(calls) == 1:
            return httpx.Response(
                200, json={"info": {"ver": "0.15", "leds": {}}, "state": {"on": True, "bri": 255}}
            )
        if req.method == "POST":
            return httpx.Response(200, json={"success": True})
        raise httpx.ReadTimeout("mock timeout")

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(respond), **kw)
    )
    device = Device(name="WLED", kind="wled", protocol="http", endpoint="192.168.1.50")
    result = asyncio.run(WLEDAdapter().execute(device, "led", {"power": False}))
    assert result["written"] is True
    assert result["result_status"] == "unverified"
    assert result["state"] == {}
    assert calls == ["GET", "POST", "GET"]


def test_disabled_device_controls_never_switch_audio(devices):
    async def run():
        r = devices
        r.store.set_preference("settings", "access", {"disabled_tools": ["device_command"]})
        r.service.native.run = AsyncMock()
        result = await request(r, op="audio_select", uid="test")
        assert "отключено" in result["error"]
        r.service.native.run.assert_not_awaited()

    asyncio.run(run())
