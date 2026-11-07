"""Private SQLite, guarded tools and mock BLE; no physical hardware evidence."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from backend.context import RequestContext, request_scope, voice_addressed
from backend.devices.lighting import Lighting, now
from backend.devices.lighting_models import LightAction, LightJob, Rule
from backend.devices.lighting_schedule import next_occurrence, solar
from backend.tools.registry import ToolError
from tests.test_devices import devices as devices
from tests.test_devices import request
from tests.test_led import rig as rig


def make_job(r, **updates):
    value = {
        "name": "Сон",
        "targets": ["led-configured"],
        "action": {"power": False},
        "next_at": (now() + timedelta(minutes=45)).isoformat(),
    }
    value.update(updates)
    return r.service.lighting.save("job", value)


def test_presets_crud_revision_restart_and_no_ble(devices):
    lighting = devices.service.lighting
    item = lighting.save(
        "preset", {"name": "Мой розовый", "action": {"color": "#ff0099", "brightness": 12}}
    )
    assert item.action.color == "#FF0099"
    item = lighting.save("preset", {**item.model_dump(), "name": "Вечер"})
    with pytest.raises(ValueError, match="другом окне"):
        lighting.save("preset", {**item.model_dump(), "revision": 1})
    assert Lighting(devices.service).list("preset")[0].name == "Вечер"
    lighting.delete("preset", item.id, item.revision)
    assert not lighting.list("preset")
    devices.factory.assert_not_called()


def test_import_is_atomic_and_export_has_no_devices(devices):
    async def run():
        lighting = devices.service.lighting
        palette = {
            "version": 1,
            "colors": [{"name": "Тёплый", "rgb": [255, 120, 40]}],
            "presets": [{"name": "Кино", "action": {"color": "#112233"}}],
        }
        await request(devices, op="lighting", operation="import", palette=palette)
        assert len(lighting.list("preset")) == 1
        duplicate = {**palette, "colors": [{"name": "Новый", "rgb": [0, 0, 0]}]}
        result = await request(devices, op="lighting", operation="import", palette=duplicate)
        assert result.get("error")
        assert [c.name for c in devices.service.colors.list()] == ["Тёплый"]
        result = await request(devices, op="lighting", operation="export")
        exported = json.dumps(result["result"]["palette"])
        assert "endpoint" not in exported and "targets" not in exported
        devices.factory.assert_not_called()

    asyncio.run(run())


def test_manual_apply_and_duplicate_request_write_once(devices):
    async def run():
        payload = {
            "op": "lighting",
            "operation": "apply",
            "targets": ["led-configured"],
            "action": {"color": "#123456"},
            "request_id": "one-request-123",
        }
        result = await request(devices, **payload)
        assert result["result"]["result_status"] == "unverified"
        assert result["result"]["written"]
        await request(devices, **payload)
        assert devices.factory.call_count == 1
        device = devices.service.store.get("led-configured")
        assert device.sent["color"] == "#123456" and "color" not in device.state

    asyncio.run(run())


def test_timer_is_durable_claimed_and_never_replayed(devices):
    async def run():
        lighting = devices.service.lighting
        job = make_job(devices)
        lighting = Lighting(devices.service)
        lighting.apply = AsyncMock(return_value={"result_status": "unverified", "message": "sent"})
        due = datetime.fromisoformat(job.next_at)
        await lighting.tick(due)
        await lighting.tick(due + timedelta(seconds=20))
        lighting.apply.assert_awaited_once()
        assert not lighting.get("job", job.id).enabled
        assert lighting.snapshot()["journal"][0]["status"] == "unverified"

    asyncio.run(run())


def test_sleep_miss_recurring_anchor_and_conditions(devices):
    async def run():
        lighting = devices.service.lighting
        job = make_job(devices, rule={"kind": "interval", "interval_s": 3600}, catch_up_s=30)
        lighting.apply = AsyncMock(return_value={"result_status": "success", "message": "ok"})
        due = datetime.fromisoformat(job.next_at)
        await lighting.tick(due + timedelta(hours=5, minutes=10))
        lighting.apply.assert_not_awaited()
        saved = lighting.get("job", job.id)
        assert saved.next_at == (due + timedelta(hours=6)).isoformat()
        saved.conditions = {"учёба": True}
        lighting.put("job", saved)
        await lighting.tick(datetime.fromisoformat(saved.next_at))
        lighting.apply.assert_not_awaited()
        assert lighting.get("job", job.id).status == "skipped"

    asyncio.run(run())


def test_crash_does_not_replay_ambiguous_write(devices):
    job = make_job(devices)
    job.status = "running"
    devices.service.lighting.put("job", job)
    restarted = Lighting(devices.service)
    assert restarted.get("job", job.id).status == "interrupted"
    assert not restarted.get("job", job.id).enabled
    devices.factory.assert_not_called()


def test_conflict_validation_and_runtime_conflict(devices):
    async def run():
        lighting = devices.service.lighting
        first = make_job(devices)
        with pytest.raises(ValueError, match="Конфликт"):
            make_job(devices, next_at=first.next_at)
        second = make_job(
            devices,
            next_at=(datetime.fromisoformat(first.next_at) + timedelta(seconds=3)).isoformat(),
        )
        lighting.apply = AsyncMock(return_value={"result_status": "success", "message": "ok"})
        await lighting.tick(datetime.fromisoformat(second.next_at))
        assert lighting.apply.await_count == 1
        assert {j.status for j in lighting.list("job")} == {"success", "conflict"}

    asyncio.run(run())


def test_fade_progress_cancel_and_restart(devices):
    async def run():
        d = devices.service.store.get("led-configured")
        d.sent = {"color": "#FF0000", "brightness": 80}
        devices.service.store.observe(d)
        lighting = devices.service.lighting
        job = make_job(devices, action={"kind": "fade", "brightness": 0, "duration_s": 1200})
        lighting.apply = AsyncMock(return_value={"result_status": "unverified", "message": "sent"})
        start = datetime.fromisoformat(job.next_at)
        await lighting.tick(start)
        job = lighting.get("job", job.id)
        assert job.status == "active"
        restarted = Lighting(devices.service)
        frames, finished = restarted.frame(job, start + timedelta(seconds=600))
        assert frames[0][1].brightness == 40 and not finished
        await restarted.stop(job.targets)
        assert not restarted.get("job", job.id).enabled

    asyncio.run(run())


@pytest.mark.parametrize(
    "kind", ["cycle", "pulse", "breathing", "rainbow", "twinkle", "transition"]
)
def test_software_frames_are_bounded(devices, kind):
    job = LightJob(
        name="effect",
        targets=["led-configured"],
        next_at=now().isoformat(),
        action=LightAction(kind="effect", effect=kind, colors=["#000000", "#FFFFFF"]),
        progress={
            "started": now().isoformat(),
            "initial": {"led-configured": {"color": "#FF0000", "brightness": 75}},
        },
    )
    for second in range(0, 80, 2):
        frames, _ = devices.service.lighting.frame(job, now() + timedelta(seconds=second))
        assert frames[0][1].kind == "set"
        level = frames[0][1].brightness
        assert level is None or 0 <= level <= 100


def test_schedule_weekdays_dates_dst_and_solar():
    friday = datetime(2026, 11, 6, 22, 0, tzinfo=UTC)
    rule = Rule(kind="weekly", at="23:30", weekdays=[0, 1, 2, 3, 4])
    assert next_occurrence(rule, friday) == datetime(2026, 11, 9, 21, 30, tzinfo=UTC)
    rule = Rule(kind="date", date="2026-12-01", at="12:00")
    assert next_occurrence(rule, friday) == datetime(2026, 12, 1, 10, 0, tzinfo=UTC)
    # Tallinn jumps 03:00 -> 04:00 on 29 March. Missing wall time skips that day.
    rule = Rule(kind="weekly", at="03:30")
    assert next_occurrence(rule, datetime(2026, 3, 28, 23, tzinfo=UTC)).day == 30
    rise = solar(friday.date(), 59.4, 24.7, False)
    setting = solar(friday.date(), 59.4, 24.7, True)
    assert 5 <= rise.hour <= 7 and 13 <= setting.hour <= 15
    assert solar(datetime(2026, 6, 21).date(), 89, 0, False) is None
    with pytest.raises(ValidationError):
        Rule(kind="sunrise")


@pytest.mark.parametrize(
    "text,action",
    [
        ("Выключи ленту через 45 минут", "light_timer"),
        ("Сделай плавное затухание на 20 минут", "light_fade"),
        ("Включи ELK-BLEDOB через 2 часа", "light_timer"),
    ],
)
def test_voice_temporal_grammar(devices, text, action):
    device = devices.service.store.get("led-configured")
    device.ble_name = "ELK-BLEDOB"
    devices.service.store.save(device)
    plan = devices.service.voice_plan(text)
    assert plan["commands"][0]["action"] == action


def test_weekday_voice_and_fail_closed(devices):
    plan = devices.service.voice_plan("Каждый будний день в 23:30 выключай освещение")
    assert plan["lighting_schedule"]["rule"]["weekdays"] == [0, 1, 2, 3, 4]
    for text in [
        "Выключи ленту через 45 минут или включи ее",
        "Не выключи ленту через 45 минут",
        "Выключи ленту через 45 минут и открой Safari",
    ]:
        assert devices.service.voice_plan(text).get("error")


def test_unavailable_capabilities_never_write_or_create_task(devices):
    devices.strip._save(devices.strip._load().model_copy(update={"confirmed": {"rgb"}}))
    with pytest.raises(ToolError, match="не проверен"):
        make_job(devices)
    assert not devices.service.lighting.list("job")
    devices.factory.assert_not_called()


def test_scenario_capabilities_and_templates(devices):
    async def run():
        d = devices
        result = await request(d, op="lighting", operation="templates", targets=["led-configured"])
        assert result["result"]["result_status"] == "success"
        created = [s for s in d.tools.automations.store.list() if s.name == "Просмотр фильма"]
        assert len(created) == 1 and created[0].actions[0].kind == "parallel"
        cap = d.tools.automations.capabilities.get("device.led-configured.light_timer")
        token = voice_addressed.set(True)
        try:
            with request_scope(RequestContext(d.conv.device, "Управляй выбранным устройством")):
                result = json.loads(
                    await d.tools.call(cap.tool, cap.arguments({"delay_s": 60, "power": False}))
                )
            assert result.get("scheduled") is True, result
            assert d.tools.automations.capabilities.verify(cap.tool, {}, result) == "confirmed"
        finally:
            voice_addressed.reset(token)
        d.factory.assert_not_called()

    asyncio.run(run())
