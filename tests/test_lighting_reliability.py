"""Regression coverage for old observations and cancellation before transport writes."""

import asyncio
from unittest.mock import AsyncMock

from backend.led import LEDState
from backend.tools.registry import ToolError
from tests.test_devices import devices as devices
from tests.test_led import rig as rig


def test_pre_fingerprint_observation_unlocks_presets_but_seed_does_not(rig):
    rig.strip._save(
        LEDState(
            identity=rig.strip.identity,
            confirmed={"color:12,34,56"},
            last_write={
                "status": "written",
                "requested": {"action": "color", "rgb": [12, 34, 56]},
                "physical_confirmed": True,
            },
        )
    )
    assert "rgb" in rig.strip._load().confirmed
    rig.strip._save(LEDState(identity=rig.strip.identity, confirmed={"color:255,0,0"}))
    assert "rgb" not in rig.strip._load().confirmed


def test_latest_queued_rgb_wins_before_ble_write(rig):
    async def run():
        state = rig.strip._load()
        state.confirmed.add("rgb")
        rig.strip._save(state)
        rig.strip._write = AsyncMock()
        await rig.strip.lock.acquire()
        one = asyncio.create_task(rig.strip.execute(action="color", rgb=[1, 2, 3]))
        two = asyncio.create_task(rig.strip.execute(action="color", rgb=[4, 5, 6]))
        await asyncio.sleep(0)
        rig.strip.lock.release()
        results = await asyncio.gather(one, two, return_exceptions=True)
        assert isinstance(results[0], ToolError)
        assert results[1]["written"] is True
        assert rig.strip._write.await_count == 1
        assert rig.strip._write.await_args.args[-1]["rgb"] == [4, 5, 6]

    asyncio.run(run())


def test_non_lighting_temporal_request_is_left_to_existing_router(devices):
    assert devices.service.voice_plan("Напомни через 45 минут достать пирог") is None


def test_both_strips_and_plural_color_use_existing_device_route(devices):
    from uuid import uuid4

    from backend.devices.models import Device

    devices.service.store.save(
        Device(name="Вторая лента", kind="led_strip", protocol="ble", endpoint=str(uuid4()))
    )
    plan = devices.service.voice_plan("Сделай обе ленты розовыми")
    assert len(plan["commands"]) == 2
    assert all(command["color"] == "#FF0020" for command in plan["commands"])


def test_pause_distant_timer_does_not_need_hardware(devices):
    from tests.test_lighting import make_job

    job = make_job(devices)
    device = devices.service.store.get("led-configured")
    device.place_id = "dorm"
    devices.service.store.save(device)
    job.enabled = False
    devices.service.lighting.save("job", job.model_dump())
    devices.factory.assert_not_called()


def test_parallel_scenario_on_two_leds_uses_real_guarded_transport(devices):
    from uuid import uuid4

    from backend.context import RequestContext, request_scope, voice_addressed
    from backend.devices.models import Device

    async def run():
        service = devices.service
        second = service.store.save(
            Device(
                name="Вторая лента",
                ble_name="ELK-BLEDOB",
                kind="led_strip",
                protocol="ble",
                endpoint=str(uuid4()),
            )
        )
        strip = service.strip(second)
        strip._save(LEDState(identity=strip.identity, confirmed={"rgb", "brightness"}))
        service.update_catalogue()
        service.lighting.templates(["led-configured", second.id])
        scenario = next(
            s for s in devices.tools.automations.store.list() if s.name == "Просмотр фильма"
        )
        token = voice_addressed.set(True)
        try:
            with request_scope(RequestContext(devices.conv.device, "Запусти сценарий освещения")):
                result = await devices.tools.automations.engine.run(scenario)
            assert result["status"] == "sent", "\n".join(
                str(s) for s in result["steps"] if s.get("status") == "failed"
            )
        finally:
            voice_addressed.reset(token)

    asyncio.run(run())
