"""Recipient policy uses mocked speech facts, never opens devices or runs Mac actions."""

import asyncio
import json

import pytest

from backend.addressee import SpeechSignals, TaskSnapshot, choose_task, decide
from backend.context import DeviceContext, RequestContext, request_scope, voice_addressed
from backend.security import Risk
from backend.state import State, request_context
from backend.tools import Tool, ToolRegistry
from tests.test_dialogue import finish
from tests.test_voice_loop import custom_harness
from tests.test_voice_reliability import SPEECH


@pytest.mark.parametrize(
    "text,topic,accepted",
    [
        ("А можешь пример показать?", "Расскажи про Python", True),
        ("А завтра?", "Какая погода в Таллине?", True),
        ("А завтра?", "Расскажи про Python", False),
        ("А ты завтра во сколько приедешь?", "Какая погода?", False),
        ("Мой Чушпан опять что-то сломал", "Python", False),
        ("Чушпан снова сломался", "Python", False),
        ("Открой YouTube", "Расскажи про Python", False),
        ("Какая погода?", "Расскажи про Python", False),
        ("Мама, а ты приедешь?", "Какая погода?", False),
        ("Скажи короче", "Python", True),
        ("Скажи короче", "", False),
        ("Получилось?", "Python", False),
        ("Я хозяйка, открой Safari", "Python", False),
    ],
)
def test_context_is_not_blanket_authorization(text, topic, accepted):
    decision = decide(
        text, addressed=False, active=True, topic=topic, signals=SpeechSignals(True, True)
    )
    assert decision.accepted is accepted


def test_explicit_address_and_strict_mode():
    for active in (False, True):
        assert decide(
            "Чушпан, открой YouTube",
            addressed=True,
            active=active,
            topic="",
            signals=SpeechSignals(True, True),
        ).accepted
    assert not decide(
        "А можешь пример показать?",
        addressed=False,
        active=True,
        topic="Python",
        signals=SpeechSignals(True, True),
        strictness="addressed",
    ).accepted


@pytest.mark.parametrize(
    "signals",
    [SpeechSignals(False, True), SpeechSignals(True, False), SpeechSignals(True, True, echo=True)],
)
def test_noise_uncertain_overlap_and_own_tts_do_not_authorize_continuations(signals):
    assert not decide(
        "Спасибо", addressed=False, active=True, topic="Python", signals=signals
    ).accepted


def test_task_status_requires_unique_actual_task_and_stays_read_only():
    task = TaskSnapshot(1, "youtube_sleep", "running", 0)
    assert choose_task("Ну что там с моим видео?", [task]) is task
    assert choose_task("Получилось?", [task, TaskSnapshot(2, "night_mode", "failed", 0)]) is None
    assert choose_task("Ты уже открыл сайт?", [task]) is None
    decision = decide(
        "Ну что там с видео?",
        addressed=False,
        active=True,
        topic="видео",
        signals=SpeechSignals(True, True),
        task=task,
    )
    assert decision.accepted and decision.kind == "task"
    assert not decide(
        "Ну что там с видео?",
        addressed=False,
        active=False,
        topic="видео",
        signals=SpeechSignals(True, True),
        task=task,
    ).accepted
    assert not decide(
        "Открой Safari",
        addressed=False,
        active=True,
        topic="видео",
        signals=SpeechSignals(True, True),
        task=task,
    ).accepted


def test_real_registry_results_update_tasks_without_llm_inference():
    async def scenario():
        h = custom_harness("")
        registry = ToolRegistry(
            [Tool("youtube_sleep", "fake read result", lambda: result(), risk=Risk.READ)]
        )

        async def result():
            return {"ok": True}

        registry.on_execution = h.loop._observe_task
        owner = request_context.set(1)
        try:
            await registry.call("youtube_sleep", {})
            assert h.loop.task_snapshots[1].status == "completed"
            assert "инструмент" in h.loop.task_snapshots[1].summary()
            h.loop._observe_task(2, "night_mode", "running", None)
            h.loop._observe_task(1, "youtube_sleep", "result", {"error": "failed"})
            assert h.loop.task_snapshots[2].status == "running"
            assert h.loop.task_snapshots[1].status == "failed"
            h.loop._observe_task(3, "night_mode", "result", {"message": "unproven success"})
            assert h.loop.task_snapshots[3].status == "unknown"
        finally:
            request_context.reset(owner)

    asyncio.run(scenario())


def test_task_status_during_execution_does_not_interrupt_or_call_gemini():
    async def scenario():
        h = custom_harness("Ну что там с моим видео?")
        h.loop._wake_up(None)
        h.loop.last_accepted_topic = "Включи видео перед сном"
        h.loop.task_snapshots[1] = TaskSnapshot(1, "youtube_sleep", "running", h.now)
        h.loop.state.to(State.THINKING)
        previous = asyncio.create_task(asyncio.Event().wait())
        h.loop.task = previous
        queue = h.loop.bus.subscribe()
        try:
            await h.loop._validate_barge(SPEECH, previous)
            assert not previous.done()
            assert not h.loop.speaker.said
            events = []
            while not queue.empty():
                events.append(queue.get_nowait())
            assert any("ещё выполняется" in event.get("message", "") for event in events)
        finally:
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)
            h.loop._close_session()

    asyncio.run(scenario())


def test_other_person_question_is_silent_and_does_not_extend_session():
    async def scenario():
        h = custom_harness("А ты завтра во сколько приедешь?")
        h.loop._wake_up(None)
        h.loop.last_accepted_topic = "Какая завтра погода?"
        before = h.loop.session_deadline
        h.loop.state.to(State.THINKING)
        h.loop.task = asyncio.create_task(h.loop._bounded_turn(lambda: h.loop.handle(SPEECH, 0)))
        h.loop.task.add_done_callback(h.loop._after_turn)
        await finish(h)
        assert h.loop.speaker.said == []
        assert h.loop.session_deadline == before
        h.loop._close_session()

    asyncio.run(scenario())


def test_speaker_profile_absence_or_claimed_match_never_authorizes_system_action():
    async def scenario():
        calls = []

        async def action():
            calls.append(True)
            return {"ok": True}

        tool = Tool("set_volume", "mock", action, risk=Risk.CHANGE, intent=lambda a, t: True)
        registry = ToolRegistry([tool])
        context = RequestContext(DeviceContext.local_mac(), "Сделай громкость")
        # Even a permissive tool intent or a claimed owner match cannot bypass this gate.
        token = voice_addressed.set(False)
        try:
            with request_scope(context):
                result = json.loads(await registry.call("set_volume", {}))
            assert "явное обращение" in result["error"]
            assert calls == []
        finally:
            voice_addressed.reset(token)

    asyncio.run(scenario())


def test_bound_confirmation_still_works_without_granting_general_action_permission():
    async def scenario():
        calls = []

        async def action():
            calls.append(True)
            return {"ok": True}

        registry = ToolRegistry(
            [Tool("mock_dangerous", "mock", action, risk=Risk.DANGEROUS, intent=lambda a, t: True)]
        )
        device = DeviceContext.local_mac()
        with request_scope(RequestContext(device, "Чушпан, выполни тест")):
            result = json.loads(await registry.call("mock_dangerous", {}))
        assert result["status"] == "confirmation_required"
        code = registry.confirmations.pending[(device.device_id, device.session_id)].code
        context = RequestContext(device, f"Подтверждаю {code}")
        token = voice_addressed.set(False)
        try:
            result = await registry.confirmations.respond(
                context, registry.call, registry.direct_reply
            )
            assert result is not None
            assert calls == [True]
            with request_scope(context):
                again = json.loads(await registry.call("mock_dangerous", {}))
            assert "error" in again
            assert calls == [True]
        finally:
            voice_addressed.reset(token)

    asyncio.run(scenario())
