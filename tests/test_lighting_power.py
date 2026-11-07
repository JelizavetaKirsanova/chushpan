"""Normal HUD requests reach the model-specific BLE writer without a power reading."""

import asyncio
from dataclasses import replace

import pytest
from bleak.exc import BleakError

from backend.led import LEDState
from tests.test_devices import devices as devices
from tests.test_devices import request
from tests.test_led import rig as rig


@pytest.mark.parametrize(
    ("model", "on", "color", "brightness"),
    [
        ("ELK-BLEDOB", "7e0404010001ff00ef", "7e07050300ff8010ef", "7e040124ffffff00ef"),
        ("ELK-BLEDDM 39", "7e0404f00001ff00ef", "7e00050300ff8000ef", "7e040124ff00ff00ef"),
    ],
)
def test_power_reconnect_and_preset_use_selected_driver(devices, model, on, color, brightness):
    async def run():
        d = devices
        d.strip.cfg = d.strip.cfg.model_copy(update={"name": model})
        driver = d.strip.driver()
        d.strip._save(
            LEDState(
                identity=d.strip.identity,
                driver_fingerprint=driver.fingerprint,
                confirmed={"rgb", "brightness"},
            )
        )
        identifier = "led-configured"
        action = dict(op="lighting", operation="apply", targets=[identifier])
        denied = await request(d, **action, action={"power": True})
        assert "Переподключить" in denied["error"]
        d.factory.assert_not_called()
        await request(d, op="inspect", id=identifier)
        d.clients[0].write_gatt_char.assert_not_awaited()
        item = next(i for i in d.service.snapshot()["items"] if i["id"] == identifier)
        assert item["features"]["power_on"] and item["features"]["power_off"]
        assert item["control_reason"] == "" and "power" not in item["state"]
        result = await request(d, **action, action={"power": True})
        assert result["result"]["result_status"] == "unverified"
        assert d.service.store.get(identifier).sent["power"] is True
        assert "power" not in d.service.store.get(identifier).state
        assert any(i["busy"] for event in d.events for i in event["items"] if i["id"] == identifier)
        preset = d.service.lighting.save(
            "preset", {"name": "Тестовая палитра", "action": {"color": "#00FF80", "brightness": 36}}
        )
        await request(d, **action, action={"kind": "preset", "preset_id": preset.id})
        await request(d, **action, action={"power": False})
        packets = [
            call.args[1].hex()
            for client in d.clients
            for call in client.write_gatt_char.await_args_list
        ]
        # The existing combined colour/brightness preset turns the strip on first.
        assert packets == [on, on, color, brightness, "7e0404000000ff00ef"]
        assert "power" not in d.service.store.get(identifier).state

    asyncio.run(run())


def test_failed_reconnect_reason_and_agent_permission(devices):
    async def run():
        create = devices.factory.side_effect

        def offline(*args, **kwargs):
            client = create(*args, **kwargs)
            client.connect.side_effect = BleakError("private connection detail")
            return client

        devices.factory.side_effect = offline
        await request(devices, op="inspect", id="led-configured")
        item = next(i for i in devices.service.snapshot()["items"] if i["id"] == "led-configured")
        assert item["connection"] == "error"
        assert "Не удалось подключиться" in item["connection_error"]
        assert "private" not in item["connection_error"]
        devices.clients[0].write_gatt_char.assert_not_awaited()
        devices.conv.device = replace(devices.conv.device, capabilities=frozenset())
        event = await request(
            devices,
            op="lighting",
            operation="apply",
            targets=["led-configured"],
            action={"power": True},
        )
        assert "Mac Agent" in event["error"]
        assert devices.factory.call_count == 1

    asyncio.run(run())
