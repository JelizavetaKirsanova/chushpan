"""BLE discovery, GATT checks and persisted multi-strip routing without physical hardware."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from bleak.exc import BleakBluetoothNotAvailableError, BleakBluetoothNotAvailableReason

from backend import led
from backend.devices.models import Device
from backend.devices.store import DeviceStore
from backend.led import LEDState, LEDStrip
from backend.tools.registry import ToolError
from tests.test_devices import devices as devices
from tests.test_devices import request
from tests.test_dialogue_context import ask
from tests.test_led import rig as rig


def advertisement(name, *, cached_name=None, rssi=-58, services=()):
    return (
        SimpleNamespace(name=cached_name or name, address=str(uuid4()).upper()),
        SimpleNamespace(local_name=name, rssi=rssi, service_uuids=list(services)),
    )


def test_discovery_includes_elk_families_service_candidates_and_unknowns(rig, monkeypatch):
    async def run():
        found = {
            "new": advertisement("ELK-BLEDDM 39", cached_name="Cached"),
            "old": advertisement("ELK-BLEDOB", rssi=-70),
            "future": advertisement("elk-ble-next 7", rssi=-65),
            "service": advertisement("Other controller", services=[str(rig.settings.led.service)]),
            "unknown": advertisement("Sensor", rssi=-40),
            "unnamed": advertisement(None, rssi=None),
        }
        scan = AsyncMock(return_value=found)
        monkeypatch.setattr(led, "BleakScanner", SimpleNamespace(discover=scan))
        result = await rig.strip.discover()
        assert scan.call_args.kwargs == {"timeout": 4, "return_adv": True}
        candidates = {v["name"]: v for v in result["candidates"]}
        assert len(candidates) == 6
        for name in ("ELK-BLEDDM 39", "ELK-BLEDOB", "elk-ble-next 7", "Other controller"):
            assert candidates[name]["compatibility"] == "candidate"
        assert candidates["ELK-BLEDDM 39"]["rssi"] == -58
        assert candidates["ELK-BLEDDM 39"]["connection"] == "not_checked"
        assert candidates["Sensor"]["compatibility"] == "unknown"
        assert candidates["Без имени"]["rssi"] is None
        assert result["candidates"][-1]["name"] == "Без имени"
        assert rig.strip._load().confirmed == {"color:255,0,0"}
        rig.factory.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure,match",
    [
        (
            BleakBluetoothNotAvailableError("off", BleakBluetoothNotAvailableReason.POWERED_OFF),
            "Bluetooth выключен",
        ),
        (
            BleakBluetoothNotAvailableError(
                "denied", BleakBluetoothNotAvailableReason.DENIED_BY_USER
            ),
            "Конфиденциальность",
        ),
        (TimeoutError(), "тайм-аут"),
        (RuntimeError("native failure"), "Ошибка поиска"),
    ],
)
def test_scan_error_is_actionable_and_rescan_releases_lock(rig, monkeypatch, failure, match):
    async def run():
        scan = AsyncMock(side_effect=[failure, {}])
        monkeypatch.setattr(led, "BleakScanner", SimpleNamespace(discover=scan))
        with pytest.raises(ToolError, match=match):
            await rig.strip.discover()
        assert (await rig.strip.discover())["candidates"] == []
        rig.factory.assert_not_called()

    asyncio.run(run())


def test_scan_cancellation_releases_resources_and_allows_new_scan(rig, monkeypatch):
    async def run():
        started, stopped = asyncio.Event(), asyncio.Event()

        async def scan(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        scanner = SimpleNamespace(discover=scan)
        monkeypatch.setattr(led, "BleakScanner", scanner)
        task = asyncio.create_task(rig.strip.discover())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
        scanner.discover = AsyncMock(return_value={})
        assert (await rig.strip.discover())["candidates"] == []

    asyncio.run(run())


def test_gatt_checks_write_and_notify_without_granting_command_permissions(rig):
    async def run():
        create = rig.factory.side_effect

        def client(*args, **kwargs):
            result = create(*args, **kwargs)
            notify = SimpleNamespace(
                uuid=str(rig.settings.led.notify_characteristic), properties=["notify", "read"]
            )
            service = result.services.get_service.return_value
            service.characteristics = [rig.char, notify]
            service.get_characteristic.side_effect = lambda uuid: (
                rig.char if uuid == rig.char.uuid else notify
            )
            return result

        rig.factory.side_effect = client
        rig.strip.cfg.address = uuid4()
        result = await rig.strip.inspect(listen_s=0)
        assert result["compatibility"] == "gatt_match"
        assert result["connection"] == "disconnected"
        assert "ещё не подтверждены" in result["message"]
        assert rig.strip._load().confirmed == set()
        rig.clients[0].write_gatt_char.assert_not_awaited()
        rig.clients[0].disconnect.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("missing", ["service", "write", "properties"])
def test_gatt_mismatch_is_not_reported_as_compatible_and_always_disconnects(rig, missing):
    async def run():
        create = rig.factory.side_effect

        def client(*args, **kwargs):
            result = create(*args, **kwargs)
            service = result.services.get_service.return_value
            if missing == "service":
                result.services.get_service.return_value = None
            elif missing == "write":
                service.characteristics = []
            else:
                rig.char.properties = ["read"]
            return result

        rig.factory.side_effect = client
        report = await rig.strip.inspect(listen_s=0)
        assert report["compatibility"] == "gatt_mismatch"
        assert report["control_packets_sent"] == 0
        assert "команды не отправлял" in report["message"]
        rig.clients[0].disconnect.assert_awaited_once()
        rig.clients[0].write_gatt_char.assert_not_awaited()

    asyncio.run(run())


def test_ui_scan_metadata_multiple_strips_and_restore_are_independent(devices, monkeypatch):
    async def run():
        r = devices
        new_address, unknown_address = str(uuid4()).upper(), str(uuid4())
        scan = AsyncMock(
            return_value={
                "candidates": [
                    {
                        "name": "ELK-BLEDDM 39",
                        "address": new_address,
                        "rssi": -58,
                        "compatibility": "candidate",
                        "compatibility_reason": "Проверить протокол",
                        "connection": "not_checked",
                    },
                    {
                        "name": "Sensor",
                        "address": unknown_address,
                        "rssi": -80,
                        "compatibility": "unknown",
                        "connection": "not_checked",
                    },
                ]
            }
        )
        monkeypatch.setattr(LEDStrip, "discover", scan)
        event = await request(r, op="discover", kind="led_strip")
        candidate = event["candidates"][0]
        assert candidate["rssi"] == -58 and candidate["compatibility"] == "candidate"
        assert event["candidates"][1]["compatibility"] == "unknown"
        assert "endpoint" not in candidate and "address" not in candidate
        check = await request(r, op="check", token=candidate["token"])
        assert check["checked"]["compatibility"] == "gatt_match"
        assert check["checked"]["connection"] == "disconnected"
        added = await request(
            r,
            op="add",
            token=candidate["token"],
            name="Новая лента",
            place_id="dorm",
            room="Спальня",
            auto_connect=True,
        )
        saved = DeviceStore(r.store).get(added["saved"])
        assert saved.endpoint == new_address.lower() and saved.ble_name == "ELK-BLEDDM 39"
        assert saved.auto_connect and saved.place_id == "dorm" and saved.room == "Спальня"
        new_strip = r.service.strip(saved)
        assert new_strip.cfg.name == "ELK-BLEDDM 39" and new_strip.cfg.firmware == "unknown"
        assert new_strip._load().confirmed == set()
        assert r.strip._load().confirmed == {"rgb", "on", "off", "brightness"}
        assert new_strip.path != r.strip.path
        with pytest.raises(ToolError, match="ещё не проверен"):
            await new_strip.execute(action="on")
        assert all(not c.write_gatt_char.await_count for c in r.clients)
        assert all(c.disconnect.await_count for c in r.clients)

    asyncio.run(run())


def test_concurrent_discovery_is_serialized_and_failure_in_auto_search_is_visible(
    devices, monkeypatch
):
    async def run():
        active, maximum = 0, 0

        async def scan(self, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.005)
                return {"candidates": []}
            finally:
                active -= 1

        monkeypatch.setattr(LEDStrip, "discover", scan)
        await asyncio.gather(
            devices.service.discover("led_strip"), devices.service.discover("led_strip")
        )
        assert maximum == 1
        monkeypatch.setattr(
            LEDStrip, "discover", AsyncMock(side_effect=ToolError("Bluetooth выключен"))
        )
        devices.service.native.run = AsyncMock(side_effect=ToolError("Нет доступа"))
        event = await request(devices, op="discover", kind="auto")
        assert event["candidates"] == []
        assert any("Bluetooth выключен" in e.get("message", "") for e in devices.events)

    asyncio.run(run())


def test_voice_new_strip_ambiguity_brightness_warm_white_and_home_scope(devices):
    r = devices
    r.service.locations.select("manual", "dorm")
    old = r.service.store.get("led-configured")
    r.service.store.save(old.model_copy(update={"place_id": "dorm"}))
    for text, expected in [
        ("Сделай ленту фиолетовой", {"color": "#FF00C8"}),
        ("Установи яркость 50 процентов", {"brightness": 50}),
        ("Сделай тёплый белый свет", {"color": "#FF3020"}),
    ]:
        command = r.service.voice_plan(text)["commands"][0]
        assert command == {"target": old.id, "action": "led", **expected}
    added = r.service.store.save(
        Device(
            name="Новая лента",
            kind="led_strip",
            protocol="ble",
            endpoint=str(uuid4()),
            place_id="dorm",
        )
    )
    command = r.service.voice_plan("Чушпан, включи новую ленту")["commands"][0]
    assert command == {"target": added.id, "action": "led", "power": True}
    assert "error" in r.service.voice_plan("Сделай ленту фиолетовой")
    assert "error" in r.service.voice_plan("Установи яркость 50 процентов")
    home = r.service.store.save(
        Device(
            name="Свет дома",
            kind="led_strip",
            protocol="ble",
            endpoint=str(uuid4()),
            place_id="home",
        )
    )
    result = r.service.voice_plan("Выключи все ленты дома")
    assert result["commands"] == [{"target": home.id, "action": "led", "power": False}]
    # Location is still enforced at execution, even for an explicit other-place request.
    with pytest.raises(ToolError, match="другому"):
        r.service.check_location(home)
    for text in ("Не выключи все ленты дома", "Выключи все ленты дома и в общаге"):
        assert "commands" not in (r.service.voice_plan(text) or {})


def test_saved_strips_keep_separate_evidence_and_write_targets(devices):
    async def run():
        r = devices
        other = r.service.store.save(
            Device(
                name="Вторая лента",
                kind="led_strip",
                protocol="ble",
                endpoint=str(uuid4()),
                ble_name="ELK-BLEDOB",
            )
        )
        strip = r.service.strip(other)
        strip._save(LEDState(identity=strip.identity, confirmed={"rgb"}))
        strip.cfg.settle_s = 0
        await r.strip.execute(action="color", rgb=[255, 0, 0])
        await strip.execute(action="color", rgb=[0, 0, 255])
        assert r.factory.call_args_list[-1].args[0] == other.endpoint
        assert r.factory.call_args_list[-2].args[0] == str(r.settings.led.address)
        assert r.service.store.get(other.id).sent["color"] == "#0000FF"
        assert r.service.store.get("led-configured").sent["color"] == "#FF0000"
        assert "on" not in strip._load().confirmed
        assert all(c.disconnect.await_count == 1 for c in r.clients)

    asyncio.run(run())


def test_new_brightness_voice_route_executes_guarded_registry_without_model(devices):
    async def run():
        result = await ask(devices.conv, "Чушпан, установи яркость 50 процентов")
        assert "отправ" in result.casefold()
        devices.clients[-1].write_gatt_char.assert_awaited_once()
        assert devices.service.store.get("led-configured").sent["brightness"] == 50
        assert devices.service.store.get("led-configured").state == {}

    asyncio.run(run())


def test_readonly_connection_check_disconnects_when_cancelled(rig):
    async def run():
        started = asyncio.Event()
        create = rig.factory.side_effect

        async def connect():
            started.set()
            await asyncio.Event().wait()

        def client(*args, **kwargs):
            result = create(*args, **kwargs)
            result.connect.side_effect = connect
            return result

        rig.factory.side_effect = client
        task = asyncio.create_task(rig.strip.inspect(listen_s=0))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        rig.clients[0].disconnect.assert_awaited_once()
        rig.clients[0].write_gatt_char.assert_not_awaited()
        assert not rig.strip.lock.locked()

    asyncio.run(run())


def test_readonly_connection_check_handles_native_constructor_failure(rig):
    rig.factory.side_effect = RuntimeError("native init failure")
    result = asyncio.run(rig.strip.inspect(listen_s=0))
    assert result["result_status"] == "failed"
    assert result["errors"][0]["type"] == "RuntimeError"
    assert "команды не отправлял" in result["message"]


def test_generic_colour_followup_retains_existing_authenticated_strip_selection(devices):
    async def run():
        r = devices
        old = r.service.store.get("led-configured")
        r.service.store.save(old.model_copy(update={"name": "Первая лента"}))
        r.service.store.save(
            Device(name="Вторая лента", kind="led_strip", protocol="ble", endpoint=str(uuid4()))
        )
        assert "отправ" in (await ask(r.conv, "Включи первую ленту")).casefold()
        assert "отправ" in (await ask(r.conv, "Сделай ленту фиолетовой")).casefold()
        assert r.service.store.get(old.id).sent["color"] == "#FF00C8"
        assert all(call.args[0] == old.endpoint for call in r.factory.call_args_list)

    asyncio.run(run())


def test_diagnostic_mode_does_not_classify_or_filter_names_services_or_signal(rig, monkeypatch):
    async def run():
        found = {
            "target": advertisement("ELK-BLEDDM 39", rssi=-62),
            "unknown": advertisement("Anything", rssi=-95),
        }
        scan = AsyncMock(return_value=found)
        monkeypatch.setattr(led, "BleakScanner", SimpleNamespace(discover=scan))
        result = await rig.strip.discover(scan_id="diagnostic-scan", show_all_ble=True)
        assert result["scanner_count"] == len(result["candidates"]) == 2
        assert all(c["compatibility"] == "unknown" for c in result["candidates"])
        assert all("не определялась" in c["compatibility_reason"] for c in result["candidates"])
        assert scan.call_args.kwargs == {"timeout": 4, "return_adv": True}
        rig.factory.assert_not_called()

    asyncio.run(run())


def test_discovery_counts_each_stage_and_excludes_only_invalid_macos_uuid(
    devices, monkeypatch, caplog
):
    import logging

    async def run():
        caplog.set_level(logging.INFO)
        scanner = AsyncMock(
            return_value={
                "scanner_count": 2,
                "candidates": [
                    {"name": "ELK-BLEDDM 39", "address": str(uuid4()), "rssi": -58},
                    {"name": "Bad address", "address": "not-a-macos-uuid"},
                ],
            }
        )
        monkeypatch.setattr(LEDStrip, "discover", scanner)
        event = await request(devices, op="discover", kind="led_strip", show_all_ble=True)
        report = event["discovery"]
        assert report["scanner_count"] == 2
        assert report["candidate_count"] == report["ble_candidate_count"] == 1
        assert report["excluded_count"] == 1 and report["show_all_ble"]
        assert report["started_at"] <= report["finished_at"]
        assert event["candidates"][0]["name"] == "ELK-BLEDDM 39"
        assert scanner.call_args.kwargs == {"scan_id": report["scan_id"], "show_all_ble": True}
        assert '"reason": "invalid_macos_uuid"' in caplog.text
        assert '"stage": "backend_publish"' in caplog.text
        await request(
            devices, op="scan_trace", scan_id=report["scan_id"], stage="frontend_rendered", count=1
        )
        assert '"stage": "frontend_rendered"' in caplog.text
        await request(devices, op="discover", kind="led_strip", show_all_ble=True)
        assert scanner.await_count == 2  # A second request really invokes the scanner again.
        devices.factory.assert_not_called()

    asyncio.run(run())
