"""Actual DDM serializer and mocked BLE; a separate legacy strip must never receive RGB."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from backend.ble_profiles import DDM_PROFILE
from backend.context import RequestContext, device_selection, request_scope
from backend.devices.models import Device
from backend.tools.registry import ToolError
from tests.test_devices import devices as devices
from tests.test_devices import request
from tests.test_dialogue_context import ask
from tests.test_led import rig as rig


def ddm(r):
    device = r.service.store.save(
        Device(
            name="Тестовая DDM",
            kind="led_strip",
            protocol="ble",
            endpoint="22222222-2222-4222-8222-222222222222",
            ble_name="ELK-BLEDDM 39",
            place_id="dorm",
        )
    )
    r.service.locations.select("manual", "dorm")
    return device


@pytest.mark.parametrize(
    "route", ["preset", "rgb-editor", "saved-color", "voice-rgb", "voice-named", "legacy-voice"]
)
def test_every_color_route_matches_the_successful_ddm_probe(devices, monkeypatch, route):
    async def run():
        r = devices
        device = ddm(r)
        strip = r.service.strip(device)
        assert strip.driver().id == DDM_PROFILE.id
        assert not r.service.features(device)["color"]
        await strip.inspect(listen_s=0, read_values=False)
        common = AsyncMock(wraps=r.service.apply_color)
        monkeypatch.setattr(r.service, "apply_color", common)
        tested = await request(
            r, op="probe_led", id=device.id, parameters={"color": "#FF0020"}, consent=True
        )
        assert "error" not in tested, tested
        reference = r.clients[-1].write_gatt_char.call_args
        assert reference.args[1] == bytes.fromhex("7e 00 05 03 ff 00 20 00 ef")
        assert not r.service.features(device)["color"]  # write ACK grants nothing
        await request(
            r,
            op="confirm_probe",
            id=device.id,
            probe_id=tested["result"]["probe_id"],
            observed=True,
        )
        r.service.strips.pop(device.id)
        restarted = r.service.strip(device)
        assert restarted.driver().id == DDM_PROFILE.id
        assert r.service.features(device)["color"]
        assert not r.service.features(device)["brightness"]
        assert r.service.features(device)["power_on"]  # Validated GATT permits ordinary ON.
        assert "on" not in restarted._load().confirmed
        assert "power" not in r.service.store.get(device.id).state
        common.reset_mock()
        before = len(r.clients)
        if route in {"preset", "rgb-editor", "saved-color"}:
            if route == "saved-color":
                saved = r.service.colors.save({"name": "Розовый DDM", "rgb": [255, 0, 32]})
                color = saved.hex
            else:
                color = "#FF0020"
            result = await request(
                r, op="act", id=device.id, action="led", parameters={"color": color}
            )
            assert result["result"]["result_status"] == "unverified", result
        elif route == "voice-rgb":
            assert "отправ" in (await ask(r.conv, "Сделай ленту в общаге RGB 255 0 32")).casefold()
        elif route == "voice-named":
            r.service.colors.save({"name": "Розовый DDM", "rgb": [255, 0, 32]})
            await request(r, op="color_preview", id=device.id, color="#FF0020")
            assert "отправ" in (await ask(r.conv, "Поставь мой цвет Розовый DDM")).casefold()
        else:
            token = device_selection.set(device.id)
            try:
                with request_scope(RequestContext(r.conv.device, "Сделай ленту розовой")):
                    result = json.loads(
                        await r.tools.call("led_control", {"command": "Сделай ленту розовой"})
                    )
                assert result["written"], result
            finally:
                device_selection.reset(token)
        assert common.await_count == 1
        assert common.call_args.args[0].id == device.id
        assert common.call_args.args[1] == "#FF0020"
        assert not common.call_args.kwargs.get("experiment", False)
        assert len(r.clients) == before + 1  # one command, no legacy/power/brightness preamble
        sent = r.clients[-1].write_gatt_char.call_args
        assert sent.args[1] == reference.args[1]
        assert sent.args[0].uuid == reference.args[0].uuid
        assert sent.kwargs == reference.kwargs == {"response": False}
        assert r.factory.call_args.args[0] == device.endpoint
        assert not r.service.store.get("led-configured").sent

    asyncio.run(run())


def test_pending_ddm_test_blocks_normal_rgb_until_explicit_confirmation(devices):
    async def run():
        r = devices
        device = ddm(r)
        strip = r.service.strip(device)
        await strip.inspect(listen_s=0, read_values=False)
        await request(
            r, op="probe_led", id=device.id, parameters={"color": "#00FF00"}, consent=True
        )
        before = len(r.clients)
        result = await request(
            r, op="act", id=device.id, action="led", parameters={"color": "#FF0020"}
        )
        assert "Подтверди" in result["result"]["error"]
        assert len(r.clients) == before
        assert strip._load().confirmed == set()
        assert r.service.features(r.service.store.get("led-configured"))["color"]

    asyncio.run(run())


def test_035_pending_probe_can_be_confirmed_after_restart_without_repeating_ble(devices):
    async def run():
        r = devices
        device = ddm(r)
        strip = r.service.strip(device)
        await strip.inspect(listen_s=0, read_values=False)
        result = await strip.execute(action="color", rgb=[0, 255, 0], experiment=True)
        state = strip._load()
        state.pending_probe.pop("driver_fingerprint")
        state.pending_probe.pop("gatt_fingerprint")
        strip._save(state)
        r.service.strips.pop(device.id)
        before = len(r.clients)
        confirmed = await request(
            r, op="confirm_probe", id=device.id, probe_id=result["probe_id"], observed=True
        )
        assert confirmed["result"]["result_status"] == "success"
        assert r.service.features(device)["color"]
        assert len(r.clients) == before

    asyncio.run(run())


@pytest.mark.parametrize("change", ["driver_fingerprint", "gatt_fingerprint", "observation_type"])
def test_confirmation_rejects_changed_probe_evidence(devices, change):
    async def run():
        r = devices
        device = ddm(r)
        strip = r.service.strip(device)
        await strip.inspect(listen_s=0, read_values=False)
        result = await strip.execute(action="color", rgb=[0, 255, 0], experiment=True)
        state = strip._load()
        if change != "observation_type":
            state.pending_probe[change] = "different"
            strip._save(state)
        with pytest.raises(ToolError, match="устарела"):
            await strip.confirm_probe(
                result["probe_id"], "yes" if change == "observation_type" else True
            )
        assert not strip._load().confirmed

    asyncio.run(run())
