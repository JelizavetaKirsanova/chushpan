"""Real planner, guards and receipts; only tool transports are simulated."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.config import Settings
from backend.context import DeviceContext, RequestContext, request_scope
from backend.intents import (
    Brightness,
    ControlPlan,
    ControlSession,
    DisplayAction,
    VolumeAction,
    VolumeParameters,
    compile_plan,
)
from backend.security import Risk
from backend.tools.registry import Tool, ToolRegistry, result_status

PARALLEL = "Одновременно сделай ленту розовой и яркость экрана 30%"
SEQUENCE = "Сделай ленту розовой и яркость экрана 30%"


def harness(led, display):
    registry = ToolRegistry(
        [
            Tool(
                "led_control",
                "fake LED",
                led,
                {"command": {"type": "string"}},
                ["command"],
                risk=Risk.CHANGE,
            ),
            Tool(
                "set_brightness",
                "fake display",
                display,
                {"level": {"type": "integer"}},
                ["level"],
                risk=Risk.CHANGE,
            ),
        ]
    )
    return SimpleNamespace(id=1, s=Settings(), tools=registry, on_tool=None)


def test_parallel_mode_is_explicit_and_rejects_conflicting_resources_before_execution():
    assert compile_plan(SEQUENCE).mode == "sequence"
    assert compile_plan(PARALLEL).mode == "parallel"
    plan = compile_plan("Одновременно яркость экрана 30% и громкость Mac 20%")
    assert plan.clarification and not plan.actions
    with pytest.raises(ValidationError):
        ControlPlan(
            mode="parallel",
            actions=[
                DisplayAction(parameters=Brightness(brightness=30)),
                VolumeAction(parameters=VolumeParameters(level=20)),
            ],
        )


@pytest.mark.parametrize(
    "text",
    [
        "Одновременно сделай ленту розовой и выключи компьютер",
        "Одновременно сделай ленту розовой и затем яркость экрана 30%",
        "Сделай ленту розовой, затем сотри все файлы",
        "Если темно, затем включи ленту",
    ],
)
def test_incomplete_unsafe_or_mixed_order_never_produces_partial_plan(text):
    plan = compile_plan(text)
    assert plan is None or not plan.actions


def test_known_explicit_sequence_retains_repeated_target_and_order():
    plan = compile_plan("Сделай яркость экрана 20%, затем сделай яркость экрана 30%")
    assert plan.mode == "sequence" and not plan.clarification
    assert [a.parameters.brightness for a in plan.actions] == [20, 30]


def test_parallel_operations_overlap_and_receipts_stay_in_user_order():
    async def run():
        arrived = set()
        both = asyncio.Event()

        async def arrive(name):
            arrived.add(name)
            if len(arrived) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)

        async def led(**args):
            await arrive("led")
            return {"written": True, "physical_confirmed": False}

        async def display(level):
            await arrive("display")
            return {"level": level}

        conv, controls = harness(led, display), ControlSession()
        c = RequestContext(DeviceContext.local_mac(), PARALLEL)
        with request_scope(c):
            receipt = await controls.execute(controls.plan(c.text, (c.binding, 1)), conv)
        assert [s["tool"] for s in receipt["steps"]] == ["led_control", "set_brightness"]
        assert [s["status"] for s in receipt["steps"]] == ["sent", "confirmed"]
        assert receipt["result_status"] == "unverified"

    asyncio.run(run())


def test_parallel_failure_preserves_successful_sibling_and_does_not_retry():
    async def run():
        calls = []

        async def led(**args):
            calls.append("led")
            raise RuntimeError("simulated offline device")

        async def display(level):
            calls.append("display")
            return {"level": level}

        conv, controls = harness(led, display), ControlSession()
        c = RequestContext(DeviceContext.local_mac(), PARALLEL)
        with request_scope(c):
            plan = controls.plan(c.text, (c.binding, 1))
            receipt = await controls.execute(plan, conv)
            replay = await controls.execute(plan, conv)
        assert calls == ["led", "display"] and replay["replayed"]
        assert receipt["status"] == "partial"
        assert [s["status"] for s in receipt["steps"]] == ["failed", "confirmed"]

    asyncio.run(run())


def test_cancel_waits_for_parallel_branches_and_retains_completed_results():
    async def run():
        running, cancelled = asyncio.Event(), asyncio.Event()

        async def led(**args):
            running.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def display(level):
            return {"level": level}

        conv, controls = harness(led, display), ControlSession()
        c = RequestContext(DeviceContext.local_mac(), PARALLEL)
        with request_scope(c):
            plan = controls.plan(c.text, (c.binding, 1))
            task = asyncio.create_task(controls.execute(plan, conv))
            await running.wait()
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            replay = await controls.execute(plan, conv)
        assert cancelled.is_set() and replay["replayed"]
        assert replay["status"] == "unverified"
        assert replay["steps"][1]["status"] == "confirmed"
        assert not any(lock.locked() for lock in conv.tools.device_locks.values())

    asyncio.run(run())


def test_generic_confirmation_flag_cannot_override_mismatched_requested_state():
    assert (
        result_status("set_brightness", {"level": 30}, {"level": 80, "physical_confirmed": True})
        == "failed"
    )
    assert (
        result_status(
            "set_volume",
            {"level": 30},
            {"ok": True, "volume_percent": 80, "device_state_confirmed": True},
        )
        == "failed"
    )
