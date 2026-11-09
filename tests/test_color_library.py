"""Named colours and RGB grants: SQLite and mock BLE; never physical confirmation."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.ble_profiles import DDM_PROFILE, legacy_profile
from backend.devices.colors import ColorLibrary, hex_rgb, rgb_hex
from backend.devices.models import Device
from backend.led import LEDState, LEDStrip
from backend.memory.store import MemoryStore
from backend.tools.registry import ToolError
from tests.test_devices import devices as devices
from tests.test_devices import request
from tests.test_dialogue_context import ask
from tests.test_led import rig as rig


@pytest.mark.parametrize(
    "rgb,hex_value",
    [
        ([0, 0, 0], "#000000"),
        ([255, 255, 255], "#FFFFFF"),
        ([180, 80, 255], "#B450FF"),
        ([255, 100, 50], "#FF6432"),
    ],
)
def test_rgb_hex_round_trip(rgb, hex_value):
    assert rgb_hex(rgb) == hex_value
    assert hex_rgb(hex_value.lower()) == rgb


@pytest.mark.parametrize(
    "rgb", [[], [0, 0], [0, 0, 256], [-1, 0, 0], [True, 0, 0], [1.5, 0, 0], ["1", 0, 0]]
)
def test_invalid_rgb(rgb):
    with pytest.raises(ValueError):
        rgb_hex(rgb)


@pytest.mark.parametrize("value", ["", None, "#FFF", "#GG0000", "#1234567", "123456"])
def test_invalid_hex(value):
    with pytest.raises(ValueError):
        hex_rgb(value)


def test_color_crud_timestamps_and_restart(tmp_path):
    path = tmp_path / "colors.db"
    memory = MemoryStore(path)
    colors = ColorLibrary(memory)
    first = colors.save({"name": "Ночной фиолетовый", "rgb": [180, 80, 255]})
    assert first.hex == "#B450FF" and first.created_at == first.updated_at
    edited = colors.save(
        {"name": "Лавандовый", "rgb": [255, 100, 50], "favorite": True},
        identifier=first.id,
        revision=first.revision,
    )
    assert edited.id == first.id and edited.created_at == first.created_at
    assert edited.updated_at >= first.updated_at and edited.revision == 2
    with pytest.raises(ValueError, match="другом окне"):
        colors.save({"name": "conflict", "rgb": [0, 0, 0]}, identifier=first.id, revision=1)
    with pytest.raises(ValueError, match="названием"):
        colors.save({"name": "лавандовый", "rgb": [0, 0, 0]})
    with pytest.raises(ValueError, match="Подтверди"):
        colors.delete(edited.id, edited.revision)
    memory.close()
    memory = MemoryStore(path)
    colors = ColorLibrary(memory)
    assert colors.list() == [edited]
    assert colors.named("ЛАВАНДОВЫЙ").hex == "#FF6432"
    colors.delete(edited.id, edited.revision, confirmed=True)
    assert colors.list() == []
    memory.close()


def test_library_validation_and_computed_hex(tmp_path):
    memory = MemoryStore(tmp_path / "colors.db")
    colors = ColorLibrary(memory)
    for value in (
        {"name": "", "rgb": [0, 0, 0]},
        {"name": "a", "rgb": [256, 0, 0]},
        {"name": "a", "rgb": [True, 0, 0]},
        {"name": "a", "rgb": [0, 0, 0], "hex": "#FFFFFF"},
    ):
        with pytest.raises(ValidationError):
            colors.save(value)
    memory.close()


def test_single_observed_color_grants_rgb_only_and_survives_restart(rig):
    async def run():
        rig.strip.cfg.name = "ELK-BLEDDM 39"
        await rig.strip.inspect(listen_s=0)
        result = await rig.strip.execute(action="color", rgb=[180, 80, 255], experiment=True)
        assert not rig.strip._load().confirmed
        assert result["physical_confirmed"] is False
        await rig.strip.confirm_probe(result["probe_id"], True)
        assert rig.strip._load().confirmed == {"rgb", "color:180,80,255"}
        restarted = LEDStrip(rig.settings)
        restarted.cfg = rig.strip.cfg
        assert restarted._load().confirmed == {"rgb", "color:180,80,255"}
        for rgb in ([0, 0, 0], [255, 255, 255], [12, 23, 34]):
            assert (await restarted.execute(action="color", rgb=rgb))["physical_confirmed"] is False
        for args in ({"action": "on"}, {"action": "off"}):
            assert (await restarted.execute(**args))["physical_confirmed"] is False
        with pytest.raises(ToolError, match="не проверен"):
            await restarted.execute(action="brightness", level=50)

    asyncio.run(run())


def test_previous_profile_migrates_only_recorded_human_observation(rig):
    profile = legacy_profile(rig.strip.cfg)
    state = LEDState(
        identity=rig.strip.identity,
        confirmed={"color:180,80,255"},
        driver_fingerprint=profile.previous_fingerprint,
        last_write={
            "requested": {"action": "color", "rgb": [180, 80, 255]},
            "status": "written",
            "physical_confirmed": True,
        },
    )
    rig.strip._save(state)
    assert "rgb" in rig.strip._load().confirmed
    state.last_write["physical_confirmed"] = False
    rig.strip._save(state)
    assert "rgb" not in rig.strip._load().confirmed


def test_driver_metadata_keeps_grants_but_incompatible_driver_and_gatt_revoke(rig):
    async def run():
        result = await rig.strip.execute(action="color", rgb=[1, 2, 3], experiment=True)
        await rig.strip.confirm_probe(result["probe_id"], True)
        assert not await rig.strip.select_profile(
            legacy_profile(rig.strip.cfg).model_copy(
                update={"name": "Renamed", "source": "same captured protocol"}
            )
        )
        assert "rgb" in rig.strip._load().confirmed
        rig.char.properties.append("read")
        await rig.strip.inspect(listen_s=0)
        assert not rig.strip._load().confirmed
        assert await rig.strip.select_profile(DDM_PROFILE)
        assert not rig.strip._load().confirmed
        rig.strip.cfg.address = uuid4()
        assert not rig.strip._load().confirmed

    asyncio.run(run())


def test_pending_observation_is_recoverable_after_service_restart_and_features_unlock(devices):
    async def run():
        r = devices
        strip = r.strip
        strip._save(LEDState(identity=strip.identity, confirmed=set()))
        result = (
            await request(
                r,
                op="probe_led",
                id="led-configured",
                parameters={"color": "#B450FF"},
                consent=True,
            )
        )["result"]
        assert not r.service.features(r.service.store.get("led-configured"))["color"]
        assert (
            "Подтверди"
            in r.service.features(r.service.store.get("led-configured"))["color_block_reason"]
        )
        r.service.probes.clear()  # only the persisted pending probe is authoritative
        confirmed = await request(
            r, op="confirm_probe", id="led-configured", probe_id=result["probe_id"], observed=True
        )
        assert confirmed["result"]["result_status"] == "success"
        features = r.service.features(r.service.store.get("led-configured"))
        assert features["color"] is True and features["color_block_reason"] == ""
        assert features["power_on"] and features["power_off"] and not features["brightness"]
        r.service.strips.pop("led-configured")
        assert r.service.features(r.service.store.get("led-configured"))["color"] is True
        before = len(r.clients)
        await request(
            r, op="act", id="led-configured", action="led", parameters={"color": "#FF0020"}
        )
        assert len(r.clients) == before + 1

    asyncio.run(run())


def test_color_library_and_previews_need_no_ble_and_two_devices_are_independent(devices):
    async def run():
        r = devices
        other = r.service.store.save(
            Device(
                name="Другая лента",
                kind="led_strip",
                protocol="ble",
                endpoint=str(uuid4()),
                ble_name="ELK-BLEDDM 39",
            )
        )
        saved = await request(
            r, op="color_save", value={"name": "Лавандовый", "rgb": [180, 80, 255]}
        )
        color_id = saved["result"]["saved_color_id"]
        await request(r, op="color_preview", id=other.id, color="#B450FF")
        await request(r, op="color_preview", id="led-configured", color="#FF0000")
        r.factory.assert_not_called()
        assert r.service.colors.preview(other.id) == "#B450FF"
        assert r.service.colors.preview("led-configured") == "#FF0000"
        assert r.service.snapshot()["user_colors"][0]["hex"] == "#B450FF"
        assert r.service.store.get(other.id).sent == {}
        assert r.strip._load().confirmed == {"on", "off", "rgb", "brightness"}
        assert r.service.strip(other)._load().confirmed == set()
        assert "error" in await request(r, op="color_delete", color_id=color_id, revision=1)
        await request(r, op="color_delete", color_id=color_id, revision=1, confirmed=True)
        assert r.service.colors.list() == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "command,hex_value",
    [
        ("Чушпан, сделай ленту RGB 180 80 255", "#B450FF"),
        ("Установи красный 255, зелёный 100, синий 50", "#FF6432"),
        ("Сделай ленту в общаге розовой", "#FF0020"),
    ],
)
def test_rgb_voice_uses_device_routing_and_driver(devices, command, hex_value):
    async def run():
        r = devices
        if "общаге" in command:
            device = r.service.store.get("led-configured")
            r.service.store.save(device.model_copy(update={"place_id": "dorm"}))
            await request(r, op="location", place_id="dorm")
        assert r.service.voice_plan(command), (r.service.locations.list(), command)
        answer = await ask(r.conv, command)
        assert "отправ" in answer.casefold()
        assert r.service.store.get("led-configured").sent.get("color") == hex_value, answer

    asyncio.run(run())


def test_named_color_voice_save_preview_then_apply_and_capability_guard(devices):
    async def run():
        r = devices
        await request(r, op="color_preview", id="led-configured", color="#B450FF")
        r.factory.assert_not_called()
        answer = await ask(r.conv, "Сохрани этот цвет как Лавандовый")
        assert "сохран" in answer.casefold()
        r.factory.assert_not_called()
        assert r.service.colors.named("Лавандовый").hex == "#B450FF"
        answer = await ask(r.conv, "Поставь мой цвет Лавандовый")
        assert "отправ" in answer.casefold()
        before = len(r.clients)
        r.strip._save(LEDState(identity=r.strip.identity, confirmed=set()))
        answer = await ask(r.conv, "Поставь мой цвет Лавандовый")
        assert "не проверен" in answer.casefold() or "не подтвержден" in answer.casefold()
        assert len(r.clients) == before

    asyncio.run(run())


def test_library_authentication_and_location_guards(devices):
    async def run():
        r = devices
        r.conv.device = replace(r.conv.device, authenticated=False)
        assert "error" in await request(r, op="color_save", value={"name": "x", "rgb": [1, 2, 3]})
        assert not r.service.colors.list()
        r.factory.assert_not_called()

    asyncio.run(run())


def test_pending_confirmation_rejection_never_promotes_and_cannot_cross_devices(devices):
    async def run():
        r = devices
        r.strip._save(LEDState(identity=r.strip.identity, confirmed=set()))
        other = r.service.store.save(
            Device(
                name="Вторая",
                kind="led_strip",
                protocol="ble",
                endpoint=str(uuid4()),
                ble_name="ELK-BLEDOB",
            )
        )
        result = (
            await request(
                r,
                op="probe_led",
                id="led-configured",
                parameters={"color": "#B450FF"},
                consent=True,
            )
        )["result"]
        assert "error" in await request(
            r, op="confirm_probe", id=other.id, probe_id=result["probe_id"], observed=True
        )
        assert not r.service.strip(other)._load().confirmed
        await request(
            r, op="confirm_probe", id="led-configured", probe_id=result["probe_id"], observed=False
        )
        assert not r.strip._load().confirmed
        assert "error" in await request(
            r, op="confirm_probe", id="led-configured", probe_id=result["probe_id"], observed=True
        )

    asyncio.run(run())
