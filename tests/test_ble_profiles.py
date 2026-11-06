"""Mock BLE only: these tests provide no evidence about the physical strip."""

import asyncio
import json
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from bleak.exc import BleakError
from pydantic import ValidationError

from backend.ble_profiles import DDM_PROFILE, BLEProfile, candidate_profile, legacy_profile
from backend.led import LEDState, LEDStrip, packet
from backend.tools.registry import ToolError
from tests.test_devices import devices as devices
from tests.test_devices import request
from tests.test_dialogue_context import ask
from tests.test_led import rig as rig


@pytest.mark.parametrize(
    "args,expected",
    [
        ({"action": "on"}, "7e0404f00001ff00ef"),
        ({"action": "off"}, "7e0404000000ff00ef"),
        ({"action": "color", "rgb": [12, 34, 56]}, "7e0005030c223800ef"),
        ({"action": "brightness", "level": 0}, "7e040100ff00ff00ef"),
        ({"action": "brightness", "level": 100}, "7e040164ff00ff00ef"),
    ],
)
def test_documented_ddm_candidate_is_separate_from_legacy(args, expected, rig):
    assert DDM_PROFILE.serialize(**args).hex() == expected
    assert legacy_profile(rig.strip.cfg).serialize(**args) == packet(**args)
    if args["action"] != "off":
        assert DDM_PROFILE.serialize(**args) != packet(**args)


def test_model_names_never_grant_compatibility(rig):
    rig.strip.cfg.name = "ELK-BLEDDM 39"
    assert candidate_profile(rig.strip.cfg).id == "elk-bleddm-candidate"
    for name in ("ELK-BLE", "ELK-BLEDOM", "Sensor", "unknown", "ELK-BLEDDM39"):
        rig.strip.cfg.name = name
        with pytest.raises(ToolError, match="Протокол LED неизвестен"):
            asyncio.run(rig.strip.execute(action="on", experiment=True))
    rig.factory.assert_not_called()


def test_ddm_needs_gatt_and_human_observation_and_restarts_without_replay(rig):
    rig.strip.cfg.name = "ELK-BLEDDM 39"

    async def run():
        with pytest.raises(ToolError, match="диагностику"):
            await rig.strip.execute(action="on", experiment=True)
        rig.factory.assert_not_called()
        report = await rig.strip.inspect(listen_s=0)
        assert report["compatibility"] == "gatt_match"
        rig.clients[-1].write_gatt_char.assert_not_awaited()
        with pytest.raises(ToolError, match="не проверен"):
            await rig.strip.execute(action="on")
        result = await rig.strip.execute(action="on", experiment=True)
        assert result["physical_confirmed"] is False
        assert "on" not in rig.strip._load().confirmed
        await rig.strip.confirm_probe(result["probe_id"], True)
        restarted = LEDStrip(rig.settings)
        restarted.cfg = rig.strip.cfg
        assert "on" in restarted._load().confirmed
        assert rig.factory.call_count == 2  # Loading state never opens a connection.
        result = await restarted.execute(action="on")
        assert result["physical_confirmed"] is False
        assert rig.clients[-1].write_gatt_char.await_args.args[1] == DDM_PROFILE.serialize("on")

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode,props,response",
    [
        ("response", ["write"], True),
        ("without-response", ["write-without-response"], False),
        ("auto", ["write"], True),
        ("auto", ["write", "write-without-response"], False),
    ],
)
def test_imported_gatt_and_write_modes(rig, mode, props, response):
    service_uuid, char_uuid = str(uuid4()), str(uuid4())
    profile = BLEProfile(
        id="verified-example",
        name="Local confirmed capture",
        source="Mock capture",
        service=service_uuid,
        write_characteristic=char_uuid,
        write_mode=mode,
        commands={"on": [170, 1]},
    )
    create = rig.factory.side_effect

    def client(*args, **kwargs):
        result = create(*args, **kwargs)
        service = result.services.get_service.return_value
        service.uuid = service_uuid
        rig.char.uuid, rig.char.properties = char_uuid, props
        return result

    rig.factory.side_effect = client

    async def run():
        await rig.strip.select_profile(profile)
        assert not rig.strip._load().confirmed
        report = await rig.strip.inspect(listen_s=0)
        assert report["write_characteristic"] == char_uuid
        result = await rig.strip.execute(action="on", experiment=True)
        assert rig.clients[-1].write_gatt_char.await_args.kwargs == {"response": response}
        assert rig.clients[-1].write_gatt_char.await_args.args[1] == b"\xaa\x01"
        await rig.strip.confirm_probe(result["probe_id"], True)
        assert LEDStrip(rig.settings)._load().confirmed == {"on"}

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation",
    [
        {"commands": {"on": [True]}},
        {"commands": {"on": [256]}},
        {"commands": {"on": ["r"]}},
        {"commands": {"color": ["r", "g"]}},
        {"commands": {"brightness": ["eval(1)"]}},
        {"commands": {"on": [0] * 65}},
        {"commands": {"erase": [1]}},
        {"write_mode": "guess"},
        {"service": "fff0"},
        {"startup_commands": [1]},
    ],
)
def test_profile_import_rejects_unsafe_or_unsupported_definitions(mutation):
    with pytest.raises(ValidationError):
        BLEProfile.model_validate({**DDM_PROFILE.model_dump(), **mutation})


def test_all_gatt_services_read_errors_notifications_and_export_privacy(rig, caplog):
    create = rig.factory.side_effect
    extra = SimpleNamespace(uuid=str(uuid4()), properties=["read", "write", "notify"])
    extra_service = SimpleNamespace(uuid=str(uuid4()), characteristics=[extra])
    private_value = b"private serial and authorization"

    def client(*args, **kwargs):
        result = create(*args, **kwargs)
        service = result.services.get_service.return_value
        result.services.__iter__.side_effect = lambda: iter([service, extra_service])
        result.read_gatt_char.side_effect = BleakError("private name " + str(rig.strip.cfg.address))

        async def notify(char, callback):
            callback(char, bytearray(private_value))

        result.start_notify = AsyncMock(side_effect=notify)
        return result

    rig.factory.side_effect = client
    report = asyncio.run(rig.strip.inspect(listen_s=0.001))
    assert len(report["services"]) == 2
    assert report["services"][1]["characteristics"][0]["read"] == {"error": "BleakError"}
    assert report["services"][1]["characteristics"][0]["write_modes"] == ["response"]
    assert report["notifications"][0]["byte_count"] == len(private_value)
    path = rig.strip.path.parent / "diagnostics" / (report["report_id"] + ".json")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    serialized = path.read_text() + json.dumps(report) + caplog.text
    for private in (
        str(rig.strip.cfg.address),
        rig.strip.cfg.name,
        private_value.decode(),
        "private name",
    ):
        assert private not in serialized
    assert {e["stage"] for e in report["stages"]} >= {
        "connect_started",
        "connected",
        "read",
        "notification",
        "notify_stopped",
        "disconnect",
    }
    rig.clients[0].write_gatt_char.assert_not_awaited()
    rig.clients[0].stop_notify.assert_awaited_once_with(extra)


def test_gatt_drift_revokes_grants_without_writing_or_replay(rig):
    async def run():
        await rig.strip.execute(action="color", rgb=[255, 0, 0])
        rig.char.properties.append("read")
        with pytest.raises(ToolError, match="GATT изменился"):
            await rig.strip.execute(action="color", rgb=[255, 0, 0])
        assert not rig.strip._load().confirmed
        assert rig.strip._load().last_diagnostic is None
        rig.clients[-1].write_gatt_char.assert_not_awaited()
        assert sum(c.write_gatt_char.await_count for c in rig.clients) == 1

    asyncio.run(run())


def test_ambiguous_characteristics_and_wrong_write_mode_never_write(rig):
    async def run():
        create = rig.factory.side_effect

        def client(*args, **kwargs):
            result = create(*args, **kwargs)
            result.services.get_service.return_value.characteristics = [rig.char, rig.char]
            return result

        rig.factory.side_effect = client
        with pytest.raises(ToolError, match="неоднозначен"):
            await rig.strip.execute(action="color", rgb=[255, 0, 0])
        rig.clients[-1].write_gatt_char.assert_not_awaited()
        rig.factory.side_effect = create
        result = create()
        result.services.get_service.return_value.characteristics = [rig.char]
        rig.char.properties = ["write"]
        with pytest.raises(ToolError, match="режим записи"):
            await rig.strip.execute(action="color", rgb=[255, 0, 0])
        assert all(not c.write_gatt_char.await_count for c in rig.clients)

    asyncio.run(run())


def test_profile_switch_and_old_ddm_evidence_do_not_authorize_new_bytes(rig):
    async def run():
        await rig.strip.select_profile(DDM_PROFILE)
        assert not rig.strip._load().confirmed
        assert rig.strip._load().brightness is None
        rig.strip.cfg.name = "ELK-BLEDDM 39"
        # Simulate evidence created before separate DDM serializers existed.
        rig.strip._save(
            LEDState(identity=rig.strip.identity, confirmed={"on", "rgb"}, brightness=50)
        )
        state = rig.strip._load()
        assert not state.confirmed and state.brightness is None
        with pytest.raises(ToolError, match="не проверен"):
            await rig.strip.execute(action="on")
        rig.factory.assert_not_called()

    asyncio.run(run())


def test_ddm_write_timeout_does_not_retry_or_confirm(rig):
    rig.strip.cfg.name = "ELK-BLEDDM 39"
    create = rig.factory.side_effect

    def client(*args, **kwargs):
        result = create(*args, **kwargs)
        result.write_gatt_char.side_effect = TimeoutError("private device")
        return result

    rig.factory.side_effect = client

    async def run():
        await rig.strip.inspect(listen_s=0)
        with pytest.raises(ToolError, match="автоповтора"):
            await rig.strip.execute(action="on", experiment=True)
        assert rig.factory.call_count == 2
        assert not rig.strip._load().confirmed
        assert rig.strip._load().pending_probe is None
        assert rig.strip._load().last_write["status"] == "unknown"

    asyncio.run(run())


def test_ddm_and_old_strip_voice_names_have_separate_bytes_targets_and_state(devices):
    from backend.devices.models import Device

    r = devices
    other = r.service.store.save(
        Device(
            name="Новая лента",
            aliases=["свет у окна"],
            kind="led_strip",
            protocol="ble",
            endpoint=str(uuid4()),
            ble_name="ELK-BLEDDM 39",
        )
    )
    strip = r.service.strip(other)
    r.service.update_catalogue()
    strip.cfg.settle_s = 0

    async def run():
        await strip.inspect(listen_s=0)
        for rgb in ([255, 0, 0], [0, 255, 0], [0, 0, 255]):
            result = await strip.execute(action="color", rgb=rgb, experiment=True)
            await strip.confirm_probe(result["probe_id"], True)
        before = r.strip._load().model_dump()
        reply = await ask(r.conv, "Сделай свет у окна синей")
        assert "не подтвержден" in reply.casefold().replace("ё", "е")
        assert r.clients[-1].write_gatt_char.await_args.args[1] == DDM_PROFILE.serialize(
            "color", rgb=[0, 0, 255]
        )
        assert r.factory.call_args.args[0] == other.endpoint
        assert r.strip._load().model_dump() == before
        assert r.service.store.get(other.id).sent["color"] == "#0000FF"
        assert "error" in r.service.voice_plan("Выключи ленту")
        rig_name = "led-configured"
        assert r.service.store.get(rig_name).sent == {}

    asyncio.run(run())


def test_ui_import_and_diagnostics_remain_local_explicit_and_noncontrolling(devices):
    async def run():
        r = devices
        assert "error" in await request(
            r, op="ble_profile", id="led-configured", driver="elk-bleddm-candidate"
        )
        r.factory.assert_not_called()
        await request(
            r, op="ble_profile", id="led-configured", driver="elk-bleddm-candidate", consent=True
        )
        report = (await request(r, op="diagnose_ble", id="led-configured"))["result"]
        assert report["driver_id"] == "elk-bleddm-candidate"
        assert report["control_packets_sent"] == 0
        assert not r.strip._load().confirmed
        r.clients[-1].write_gatt_char.assert_not_awaited()
        result = (
            await request(
                r, op="probe_led", id="led-configured", parameters={"power": True}, consent=True
            )
        )["result"]
        assert result["device_id"] == "led-configured"
        assert result["physical_confirmed"] is False
        assert not r.strip._load().confirmed

    asyncio.run(run())


def test_cli_failed_diagnostic_is_nonzero_and_profile_errors_hide_values(rig, monkeypatch, capsys):
    import argparse

    from backend import led_cli

    monkeypatch.setattr(led_cli, "selected_strip", lambda *args: rig.strip)
    monkeypatch.setattr(rig.strip, "inspect", AsyncMock(return_value={"result_status": "failed"}))
    assert asyncio.run(led_cli.run(rig.settings, argparse.Namespace(led_action="inspect"))) == 1
    path = rig.settings.data_path / "profile.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"private-extra-field": "must never be printed"}))
    assert (
        asyncio.run(
            led_cli.run(rig.settings, argparse.Namespace(led_action="profile", profile=str(path)))
        )
        == 1
    )
    output = capsys.readouterr()
    assert "must never be printed" not in output.out + output.err
    rig.factory.assert_not_called()
