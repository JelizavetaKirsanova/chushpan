"""History regressions over real routing/guards, with BLE, Mac and calendar mocked."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.addressee import SpeechSignals, decide
from backend.audio.stt import strip_wake_phrase
from backend.context import RequestContext, request_scope, voice_addressed, voice_session
from backend.dialogue import close_requested
from backend.intents import ControlSession, compile_plan
from backend.local_commands import route
from backend.mac_agent import LocalMacAgent
from backend.security import Risk
from backend.state import State
from backend.tools.calendar import calendar_tool
from backend.tools.registry import Tool, ToolRegistry, result_status
from tests.test_compound_intents import setup
from tests.test_dialogue import finish
from tests.test_dialogue_context import ask
from tests.test_led import rig as rig
from tests.test_settings_center import fixture
from tests.test_voice_loop import custom_harness
from tests.test_voice_reliability import SPEECH


@pytest.mark.parametrize(
    "text,color,brightness",
    [
        ("Сделай ленту розовой", "#FF0020", None),
        ("Включи розовый на ленте", "#FF0020", None),
        ("Сделай яркость ленты 30%", None, 30),
        ("Ленту на 100%", None, 100),
        ("Сделай ленту белой на 100%", "#FFFFFF", 100),
        ("Сделай светло-розовую ленту", "#FF1020", None),
        ("Сделай ленту белой и яркость на максимум", "#FFFFFF", 100),
        ("Убавь ленту до 20%", None, 20),
        ("Включи тёплый белый", "#FF3020", None),
    ],
)
def test_history_led_commands_use_one_device_and_preserve_original(
    rig, monkeypatch, text, color, brightness
):
    async def run():
        conv, store, screen, _, _ = setup(rig, monkeypatch)
        try:
            plan = compile_plan(text)
            assert len(plan.actions) == 1 and plan.actions[0].device == "led_strip"
            assert plan.actions[0].parameters.color == color
            assert plan.actions[0].parameters.brightness == brightness
            await ask(conv, text)
            screen.assert_not_awaited()
            rig.factory.assert_called()
            assert store.transcript(conv.id)[0]["content"] == text
            assert conv.controls.history[-1]["result_status"] == "unverified"
            assert not conv.controls.confirmed_state
        finally:
            store.close()

    asyncio.run(run())


def test_related_commands_stay_on_led_and_explicit_display_wins(rig, monkeypatch):
    async def run():
        conv, store, screen, _, _ = setup(rig, monkeypatch)
        try:
            for text in (
                "Сделай ленту розовой",
                "Теперь яркость 50%",
                "А теперь чуть темнее",
                "Сделай её поярче",
            ):
                await ask(conv, text)
                assert conv.controls.device == "led_strip"
            screen.assert_not_awaited()
            assert conv.controls.sent_state["led_strip"]["brightness"] == 50
            assert not conv.controls.confirmed_state
            assert "не подтверждено" in await ask(conv, "Верни предыдущий цвет")
            count = len(rig.clients)
            await ask(conv, "Сделай яркость экрана 30%")
            screen.assert_awaited_once_with("desktop_brightness", level=30)
            assert len(rig.clients) == count and conv.controls.device == "mac_display"
        finally:
            store.close()

    asyncio.run(run())


def test_resume_and_verified_restore_have_distinct_evidence(rig, monkeypatch):
    async def run():
        conv, store, screen, _, _ = setup(rig, monkeypatch)
        try:
            await ask(conv, "Выключи ленту")
            await ask(conv, "Включи обратно")
            assert conv.controls.sent_state["led_strip"]["power"] is True
            await ask(conv, "Сделай яркость экрана 20%")
            await ask(conv, "Сделай яркость экрана 40%")
            await ask(conv, "Верни как было")
            assert screen.await_args.kwargs == {"level": 20}
            assert conv.controls.history[-1]["result_status"] == "success"
        finally:
            store.close()

    asyncio.run(run())


def test_expired_other_session_and_new_topic_do_not_reuse_device(rig, monkeypatch):
    async def run():
        conv, store, _, _, _ = setup(rig, monkeypatch)
        try:
            await ask(conv, "Сделай ленту розовой")
            conv.controls.expires = 0
            assert "ленты или экрана" in await ask(conv, "Сделай яркость 30%")
            assert len(rig.clients) == 1
            await ask(conv, "ленты")
            other = (("other", "other"), conv.id)
            assert conv.controls.plan("Теперь яркость 40%", other).clarification
            assert not conv.controls.sent_state
            await ask(conv, "Сделай ленту розовой")
            # An unrelated local read must clear the device topic too.
            from backend.tools import Tool

            conv.tools.add(Tool("current_time", "time", AsyncMock(return_value={"time": "12:00"})))
            await ask(conv, "Сколько времени?")
            assert conv.controls.plan(
                "Теперь яркость 40%",
                (
                    (conv.device.device_id, conv.device.session_id),
                    conv.id,
                ),
            ).clarification
        finally:
            store.close()

    asyncio.run(run())


def test_default_device_and_ambiguity_policy_are_explicit():
    session = ControlSession(default_brightness_device="led_strip")
    assert session.plan("Сделай яркость 30%", "one").actions[0].device == "led_strip"
    session = ControlSession(ambiguity_rule="always_ask")
    session.device, session.binding, session.expires = "led_strip", "one", session.clock() + 30
    assert session.plan("Сделай яркость 30%", "one").clarification


def test_stt_missing_spaces_does_not_select_an_implicit_display():
    assert compile_plan("Чушпан сделай яркость30%").clarification_kind == "brightness_target"
    plan = compile_plan("Сделай яркость ленты30%")
    assert plan.actions[0].device == "led_strip"
    assert plan.actions[0].parameters.brightness == 30


def test_real_voice_turn_passes_session_authority_to_guarded_tool():
    async def run():
        s, store, _, model, conv, center = fixture()
        handler = AsyncMock(return_value={"ok": True})
        conv.tools.add(Tool("open_app", "app", handler, {"name": {"type": "string"}}))
        h = custom_harness("Открой Safari")
        h.loop.conv, h.loop.s = conv, s
        h.loop._wake_up(None)
        h.loop.last_accepted_topic = "Какие возможности у тебя есть?"
        h.loop._follow_up_ready = True
        h.loop.state.to(State.THINKING)
        try:
            h.loop.task = asyncio.create_task(
                h.loop._bounded_turn(lambda: h.loop.handle(SPEECH, 0))
            )
            h.loop.task.add_done_callback(h.loop._after_turn)
            await finish(h)
            handler.assert_awaited_once_with(name="Safari")
            assert not model.requests and voice_session.get() is None
            assert not any("явное обращение" in text for text in h.loop.speaker.said)
        finally:
            h.loop._close_session()
            await center.close()
            store.close()

    asyncio.run(run())


def test_continuous_mode_and_vad_controls_apply_without_opening_audio():
    async def run():
        from backend.audio.activity import VoiceControls, controls

        h = custom_harness("Чушпан, отключись")
        value = VoiceControls(
            mode="continuous",
            noise_suppression=False,
            vad_threshold=0.4,
            min_speech_ms=180,
            background_filter="strict",
        ).model_dump()
        h.loop.configure_voice(value)
        assert not h.loop.use_wake and h.state is State.LISTENING
        assert h.s.vad.threshold == 0.4 and h.s.vad.min_speech_ms == 180
        assert h.loop.segmenter.threshold == 0.4
        assert controls(h.s).background_filter == "strict"
        h.loop.state.to(State.THINKING)
        h.loop.task = asyncio.create_task(h.loop._bounded_turn(lambda: h.loop.handle(SPEECH, 0)))
        h.loop.task.add_done_callback(h.loop._after_turn)
        await finish(h)
        assert h.state is State.SLEEPING and h.loop.use_wake
        assert controls(h.s).mode == "name_only"

    asyncio.run(run())


@pytest.mark.parametrize("grant", ["none", "wrong", "right"])
def test_session_grant_is_bound_and_preserves_danger_confirmation(grant):
    async def run():
        s, store, _, _, conv, center = fixture()
        change, danger = AsyncMock(return_value={"ok": True}), AsyncMock(return_value={"ok": True})
        registry = ToolRegistry(
            [
                Tool("open_app", "app", change, {"name": {"type": "string"}}),
                Tool("mac_power", "power", danger, {"action": {"type": "string"}}),
            ]
        )
        context = RequestContext(conv.device, "Открой Safari")
        token = voice_addressed.set(False)
        session_token = voice_session.set(
            context.binding
            if grant == "right"
            else ("wrong", "wrong")
            if grant == "wrong"
            else None
        )
        try:
            with request_scope(context):
                result = json.loads(await registry.call("open_app", {"name": "Safari"}))
            assert change.await_count == (grant == "right")
            assert result["result_status"] == ("unverified" if grant == "right" else "failed")
            if grant == "right":
                with request_scope(replace(context, text="Выключи Mac")):
                    result = json.loads(await registry.call("mac_power", {"action": "shutdown"}))
                assert result["result_status"] == "requires_confirmation"
                danger.assert_not_awaited()
                # Session authority never lets a model invent a different intent.
                with request_scope(context):
                    assert json.loads(await registry.call("mac_power", {"action": "shutdown"}))[
                        "error"
                    ]
        finally:
            voice_session.reset(session_token)
            voice_addressed.reset(token)
            await center.close()
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "text",
    [
        "Субтитры сделал DimaTorzok",
        "И я лечу туда где ты",
        "они вчера снова приехали",
        "кхм",
        "музыка играет",
    ],
)
def test_background_after_reply_is_not_a_conversation(text):
    for active in (True, False):
        assert not decide(
            text,
            addressed=False,
            active=active,
            topic="Сделай ленту розовой",
            continuation_window=True,
            signals=SpeechSignals(True, True),
        ).accepted


@pytest.mark.parametrize("text", ["Да", "Нет", "Включай", "Отмена"])
def test_short_answers_are_admitted_only_inside_active_window(text):
    assert decide(
        text,
        addressed=False,
        active=True,
        topic="Музыка",
        continuation_window=True,
        signals=SpeechSignals(True, True),
    ).accepted
    assert not decide(
        text,
        addressed=False,
        active=False,
        topic="Музыка",
        continuation_window=True,
        signals=SpeechSignals(True, True),
    ).accepted
    assert not decide(
        text,
        addressed=False,
        active=True,
        topic="Музыка",
        continuation_window=True,
        signals=SpeechSignals(True, True, echo=True),
    ).accepted


@pytest.mark.parametrize(
    "prefix", ["Эй, Чушпан", "Хей, Чушпан", "Hey Chushpan", "Привет, Чушпан", "Чушпан"]
)
def test_supported_wake_variants(prefix):
    assert (
        strip_wake_phrase(
            prefix + ", сделай ленту розовой",
            "эй чушпан"
            if prefix.split()[0].lower().strip(",") in {"эй", "хей", "hey"}
            else "привет чушпан"
            if prefix.startswith("Привет")
            else "чушпан",
        )
        == "сделай ленту розовой"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Чушпан, отключись",
        "Чушпан, отключись, я сейчас не с тобой разговариваю",
        "Ты можешь микрофон отключить? Я уже не с тобой болтаю сейчас.",
    ],
)
def test_close_is_local_and_returns_to_wake(text):
    async def run():
        assert close_requested(text)
        h = custom_harness(text)
        h.loop._wake_up(None)
        h.loop.conv.reply = AsyncMock(side_effect=AssertionError("no LLM"))
        h.loop.state.to(State.THINKING)
        h.loop.task = asyncio.create_task(h.loop._bounded_turn(lambda: h.loop.handle(SPEECH, 0)))
        h.loop.task.add_done_callback(h.loop._after_turn)
        await finish(h)
        assert h.state is State.SLEEPING and not h.loop.session_active
        assert h.loop.speaker.said == ["Хорошо. Позови, когда понадоблюсь."]

    asyncio.run(run())


@pytest.mark.parametrize(
    "text",
    [
        "Какие у меня там на завтра планы?",
        "Покажи календарь на завтра",
        "Чушпан, блять, посмотри в календаре нахуй",
    ],
)
def test_calendar_history_phrases_are_local(text):
    assert route(text) == ("get_calendar", {"day": "tomorrow" if "завтра" in text else "today"})


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "calendar_permission",
        "timeout",
        "disabled",
        "invalid_event",
        "invalid_interval",
        "naive_date",
    ],
)
def test_calendar_uses_existing_read_without_location_or_permission_prompt(failure):
    async def run():
        s, store, _, _, conv, center = fixture()
        now = datetime.now(s.location.tz)
        tomorrow = (now + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
        events = [
            {
                "title": "Проверка",
                "start": tomorrow.isoformat(),
                "end": (tomorrow + timedelta(hours=1)).isoformat(),
            }
        ]
        if failure == "invalid_event":
            events[0].pop("start")
        if failure == "invalid_interval":
            events[0]["end"] = events[0]["start"]
        if failure == "naive_date":
            events[0]["start"] = tomorrow.replace(tzinfo=None).isoformat()
        personal = SimpleNamespace(
            read=AsyncMock(
                return_value={"calendar": {"events": events, "timezone": str(s.location.tz)}}
            )
        )
        if failure == "calendar_permission":
            personal.read.return_value = {"calendar": {"error": "calendar_permission"}}
        if failure == "timeout":
            personal.read.side_effect = TimeoutError("sensitive content must not escape")
        if failure == "disabled":
            store.set_preference("dashboard", "calendar_enabled", False)
        tool = calendar_tool(s, store, personal)
        agent = LocalMacAgent()
        agent.register(tool.name, tool._handler)
        tool.agent = agent
        conv.tools.add(tool)
        try:
            answer = await ask(conv, "Какие у меня там на завтра планы?")
            result = json.loads(
                next(
                    row["content"]
                    for row in store.recent(conv.id, 10)
                    if row.get("tool_name") == "get_calendar"
                )
            )
            assert not conv.llm.requests and "sensitive" not in answer
            if failure:
                assert result["result_status"] == "failed" and "нет планов" not in answer
            else:
                assert (
                    result["result_status"] == "success"
                    and "Завтра" in answer
                    and "Проверка" in answer
                )
            if failure != "disabled":
                personal.read.assert_awaited_once_with(
                    request_access=False, parts=("calendar",), day=tomorrow.date().isoformat()
                )
            else:
                personal.read.assert_not_awaited()
            assert (
                conv.tools.availability_for("get_calendar")
                == {
                    None: "available",
                    "calendar_permission": "requires_permission",
                    "timeout": "temporarily_unavailable",
                    "disabled": "not_configured",
                    "invalid_event": "execution_error",
                    "invalid_interval": "execution_error",
                    "naive_date": "execution_error",
                }[failure]
            )
            # The following model request sees the latest registry facts.
            await ask(conv, "Какие возможности у тебя есть?")
            prompt = conv.llm.requests[-1][0][0]["content"]
            assert "get_calendar" in prompt and "календаря пока нет" not in prompt.casefold()
        finally:
            await center.close()
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "result,expected",
    [
        ({"ok": True, "state": "playing"}, "unverified"),
        ({"ok": True, "state": "playing", "playback_confirmed": True}, "success"),
        ({"written": True}, "unverified"),
        ({"error": "BLE failed", "delivery_state": "partial"}, "partial_success"),
        ({"ok": False}, "failed"),
        ({"status": "confirmation_required"}, "requires_confirmation"),
    ],
)
def test_result_classifier_never_invents_physical_success(result, expected):
    assert (
        result_status("music_command", {"command": "Включи музыку"}, result, Risk.CHANGE)
        == expected
    )


def test_commands_settings_save_apply_and_restart():
    async def run():
        s, store, _, _, conv, center = fixture()
        try:
            await center.handle(
                {
                    "op": "save",
                    "category": "commands",
                    "value": {
                        "context_s": 25,
                        "default_brightness_device": "led_strip",
                        "ambiguity_rule": "always_ask",
                    },
                }
            )
            assert (
                conv.controls.ttl_s == 25 and conv.controls.default_brightness_device == "led_strip"
            )
            from backend.config import Settings
            from backend.settings_models import apply_persisted

            restarted = Settings(language="ru-RU")
            apply_persisted(restarted, store)
            assert restarted.tools.control_context_s == 25
            assert restarted.tools.ambiguity_rule == "always_ask"
        finally:
            await center.close()
            store.close()

    asyncio.run(run())


def test_close_clears_control_and_proposal_without_deleting_history(rig, monkeypatch):
    async def run():
        conv, store, _, _, _ = setup(rig, monkeypatch)
        try:
            await ask(conv, "Сделай ленту розовой")
            conv.add_exchange("Что можно включить?", "Хочешь включить ленту?")
            assert conv.pending_followup()
            before = store.transcript(conv.id)
            h = custom_harness("")
            h.loop.conv = conv
            h.loop._close_session()
            assert not conv.pending_followup() and not conv.controls.sent_state
            assert conv.controls.plan(
                "Теперь яркость 30%", ((conv.device.device_id, conv.device.session_id), conv.id)
            ).clarification
            assert store.transcript(conv.id) == before
        finally:
            store.close()

    asyncio.run(run())


def test_unverified_music_is_not_reported_as_completed_task():
    from backend.state import request_context as voice_request

    h = custom_harness("")
    token = voice_request.set(1)
    try:
        h.loop._observe_task(
            1,
            "music_command",
            "result",
            {"ok": True, "state": "playing", "result_status": "unverified"},
        )
        assert h.loop.task_snapshots[1].status == "unknown"
        assert "не подтверждён" in h.loop.task_snapshots[1].summary()
    finally:
        voice_request.reset(token)


@pytest.mark.parametrize(
    "code,expected",
    [
        ("permission_denied", "requires_permission"),
        ("app_not_installed", "not_configured"),
        ("service_timeout", "temporarily_unavailable"),
        ("script_error", "execution_error"),
    ],
)
def test_music_availability_uses_existing_structured_error_codes(code, expected):
    registry = ToolRegistry([Tool("music_command", "music", AsyncMock())])
    registry.observe_availability("music_command", {"code": code, "result_status": "failed"})
    assert registry.availability_for("music_command") == expected


def test_routing_benchmark_never_instantiates_models_or_audio(monkeypatch):
    from backend import bench_response

    def forbidden(*args, **kwargs):
        raise AssertionError("routing benchmark must not create audio or model clients")

    monkeypatch.setattr(bench_response, "OllamaClient", forbidden)
    monkeypatch.setattr(bench_response, "make_speaker", forbidden)
    report = asyncio.run(bench_response.benchmark_routing(1))
    assert len(report["rows"]) == 10
    assert all(row["model_calls"] == 0 for row in report["rows"])
    assert next(row for row in report["rows"] if row["case"] == "ambiguous")["tools"] == []
