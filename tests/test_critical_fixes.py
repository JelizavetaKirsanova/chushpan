"""Regression cases A–G. No real BLE, browser, microphone or external LLM."""

import asyncio
import copy
import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from bleak.exc import BleakBluetoothNotAvailableError, BleakBluetoothNotAvailableReason

from backend import led
from backend.brain.conversation import Conversation
from backend.brain.followups import action_proposal, is_short
from backend.brain.llm import ChatChunk, LLMError
from backend.brain.prompts import build_system_prompt
from backend.config import Settings
from backend.context import DeviceContext, RequestContext, request_scope, voice_addressed
from backend.latency import Profile
from backend.led import LEDStrip, parse
from backend.local_commands import route
from backend.mac_agent import LocalMacAgent
from backend.memory.store import MemoryStore
from backend.security import Risk, command_text, explicit_request
from backend.tools.clock import clock_tools
from backend.tools.registry import Tool, ToolRegistry
from backend.tools.system import system_tools
from tests.test_dialogue_context import ask, setup


class NoModel:
    cloud_context = False

    async def chat_stream(self, *args, **kwargs):
        raise AssertionError("Simple command must execute before personality or LLM")
        yield


def guarded(tool):
    agent = LocalMacAgent()
    agent.register(tool.name, tool._handler)
    tool.agent = agent
    return ToolRegistry([tool])


@pytest.mark.parametrize("accept", ["Давай, открой", "открой", "давай", "да", "сделай", "попробуй"])
def test_a_real_playlist_offer_and_compound_acceptance(accept):
    async def go():
        store = MemoryStore(":memory:")
        try:
            conv, playlist, _ = setup(store)
            conv.llm = NoModel()
            conv.add_exchange(
                "Мой плейлист не найден", "Могу просто открыть список твоих плейлистов."
            )
            proposal = conv.pending_followup()
            assert proposal["source"] == "assistant" and proposal["state"] == "proposed"
            token = voice_addressed.set(False)
            try:
                answer = await ask(conv, accept)
            finally:
                voice_addressed.reset(token)
            assert "Favourite Songs" in answer
            playlist.assert_awaited_once_with(action="list")
            assert not conv.pending_followup()
            assert any(m.get("tool_name") == "music_playlist" for m in store.recent(conv.id, 30))
        finally:
            store.close()

    asyncio.run(go())


@pytest.mark.parametrize(
    "text",
    [
        "Сделай ленту светло-розовой",
        "Привет, Чушпан. Сделай ленту светло-розовой",
        "Я тебя прошу, сделай мне LED-ленту светло-розовой",
        "Привет, Чушпан, сделай мне ленту светло-розовой, пожалуйста!",
        "Какая явная просьба? Я тебя прошу, сделай мне LED-ленту светло-розовой",
        "Чушпан, сделай мне светло‑розовую подсветку",
    ],
)
def test_b_c_e_g_light_pink_command_executes_before_personality(text):
    async def go():
        settings = Settings(language="ru-RU", led={"enabled": True})
        strip = LEDStrip(settings)
        strip.execute = AsyncMock(
            return_value={
                "written": True,
                "physical_confirmed": False,
                "message": "Светло-розовый отправлен; физический результат не подтверждён.",
            }
        )
        store = MemoryStore(":memory:")
        try:
            conv = Conversation(settings, NoModel(), store, tools=guarded(strip.tool()))
            answer = await ask(conv, text)
            strip.execute.assert_awaited_once_with(action="color", rgb=[255, 16, 32])
            assert "явная просьба" not in answer and "сама" not in answer
            assert "не подтверждён" in answer
            assert [m["content"] for m in store.transcript(conv.id) if m["role"] == "user"] == [
                text
            ]
        finally:
            store.close()

    asyncio.run(go())


@pytest.mark.parametrize(
    "text",
    [
        "Не сделай ленту розовой",
        "Он сказал: Привет, Чушпан. Сделай ленту розовой",
        "В документе: «Я тебя прошу, включи свет»",
        "Привет, Чушпан. Не включай свет",
        "Сделай ленту розовой и выключи Mac",
        "Она просит: открой YouTube",
    ],
)
def test_normalization_keeps_negation_quotes_and_compound_boundaries(text):
    assert route(text) is None


def test_brightness_and_light_aliases_match_intent_guard():
    assert parse("включи свет") == {"action": "on"}
    assert route("Поставь яркость экрана 20%") == ("set_brightness", {"level": 20})
    assert explicit_request("set_brightness", {"level": 20}, "Поставь яркость экрана 20%")
    assert not explicit_request("set_brightness", {"level": 20}, "Поставь яркость ленты 20%")


def test_youtube_opens_fixed_website_without_model_or_arbitrary_url(monkeypatch):
    from backend.tools import system

    run = AsyncMock(return_value=(0, ""))
    monkeypatch.setattr(system, "run", run)

    async def go():
        settings = Settings(language="ru-RU")
        tool = next(t for t in system_tools(settings) if t.name == "open_website")
        tool.required_capability = "mac.apps"
        store = MemoryStore(":memory:")
        try:
            conv = Conversation(settings, NoModel(), store, tools=guarded(tool))
            assert "передан macOS" in await ask(conv, "Привет, Чушпан. Открой YouTube")
            run.assert_awaited_once_with("open", "https://www.youtube.com")
        finally:
            store.close()

    asyncio.run(go())


def test_d_powered_off_reports_actual_bluetooth_reason(monkeypatch):
    error = BleakBluetoothNotAvailableError("off", BleakBluetoothNotAvailableReason.POWERED_OFF)
    client = SimpleNamespace(connect=AsyncMock(side_effect=error), disconnect=AsyncMock())
    factory = Mock(return_value=client)
    monkeypatch.setattr(led, "BleakClient", factory)

    async def go():
        settings = Settings(language="ru-RU", led={"enabled": True})
        strip = LEDStrip(settings)
        store = MemoryStore(":memory:")
        try:
            conv = Conversation(settings, NoModel(), store, tools=guarded(strip.tool()))
            answer = await ask(conv, "Чушпан, сделай ленту красной")
            assert "Bluetooth выключен" in answer
            assert "доступ запрещён" not in answer and "готово" not in answer.casefold()
            assert factory.call_count == 1
        finally:
            store.close()

    asyncio.run(go())


def test_f_shared_trusted_session_resumes_proposal_after_restart(tmp_path):
    path = tmp_path / "history.db"
    mac = DeviceContext.local_mac("trusted-shared-session")
    store = MemoryStore(path)
    conv, _, _ = setup(store, mac)
    conv.add_exchange("Плейлисты?", "Могу просто открыть список твоих плейлистов.")
    identity = conv.id
    store.close()

    async def go():
        store = MemoryStore(path)
        try:
            phone = replace(mac, device_id="phone", device_type="iphone")
            conv, playlist, _ = setup(store, phone)
            assert conv.id == identity
            assert "Favourite Songs" in await ask(conv, "Давай, открой")
            playlist.assert_awaited_once()
            rows = store.db.execute("SELECT DISTINCT device_type FROM messages").fetchall()
            assert {r[0] for r in rows} == {"mac", "iphone"}
            assert (
                store.db.execute("SELECT COUNT(DISTINCT request_id) FROM messages").fetchone()[0]
                == 2
            )
        finally:
            store.close()

    asyncio.run(go())


def test_proposal_survives_minutes_command_and_error_without_duplicate_stt(monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])

    async def go():
        store = MemoryStore(":memory:")
        try:
            conv, playlist, _ = setup(store)
            conv.tools.add(clock_tools(conv.s)[0])
            conv.add_exchange("Плейлист?", "Могу просто открыть список твоих плейлистов.")
            original = conv.pending_followup()["id"]
            now[0] += 180
            await ask(conv, "Сколько времени?")

            async def failed(*args, **kwargs):
                raise LLMError("test outage")
                yield

            conv.llm.chat_stream = failed
            with pytest.raises(LLMError):
                await ask(conv, "А этот плейлист точно доступен?")
            assert conv.pending_followup()["id"] == original
            assert "Favourite Songs" in await ask(conv, "Давай, открой")
            assert (
                sum(
                    m["content"] == "А этот плейлист точно доступен?"
                    for m in store.transcript(conv.id)
                )
                == 1
            )
            playlist.assert_awaited_once()
        finally:
            store.close()

    asyncio.run(go())


def test_bound_safe_led_offer_requires_acceptance_and_preserves_capabilities():
    async def go():
        settings = Settings(language="ru-RU", led={"enabled": True})
        strip = LEDStrip(settings)
        strip.execute = AsyncMock(
            return_value={"message": "Команда записана; состояние не подтверждено."}
        )
        store = MemoryStore(":memory:")
        try:
            conv = Conversation(settings, NoModel(), store, tools=guarded(strip.tool()))
            conv.add_exchange("Какой цвет?", "Могу сделать ленту светло-розовой.")
            strip.execute.assert_not_called()
            token = voice_addressed.set(False)
            try:
                assert "Команда записана" in await ask(conv, "давай сделай")
            finally:
                voice_addressed.reset(token)
            strip.execute.assert_awaited_once_with(action="color", rgb=[255, 16, 32])
            conv.add_exchange("Ещё?", "Могу сделать ленту светло-розовой.")
            conv.device = replace(conv.device, capabilities=frozenset())
            assert "Нет разрешённого" in await ask(conv, "давай")
            assert strip.execute.await_count == 1
            conv.add_exchange("Ещё?", "Могу сделать ленту светло-розовой.")
            assert "отменил" in await ask(conv, "не надо")
            assert not conv.pending_followup()
        finally:
            store.close()

    asyncio.run(go())


def test_proposals_cannot_grant_destructive_or_automatic_actions():
    secret = "sk-" + "x" * 24
    proposal = action_proposal(
        f"{secret}. Могу просто открыть список твоих плейлистов.", {"music_playlist"}
    )
    assert secret not in proposal["offer"]
    assert action_proposal("Могу усыпить Mac.", {"mac_power"}) is None
    assert action_proposal("Могу удалить все файлы.", {"delete_files"}) is None
    assert (
        action_proposal(
            "Могу открыть YouTube или сделать ленту розовой.", {"open_website", "led_control"}
        )
        is None
    )
    store = MemoryStore(":memory:")
    try:
        conv, _, _ = setup(store)
        conv.add_assistant_note("Могу просто открыть список твоих плейлистов.")
        assert not conv.pending_followup()
    finally:
        store.close()


def test_local_model_keeps_history_after_tool_and_does_not_echo_current_user():
    text = "Если бы это была поездка, я учиться еду вообще-то"

    class Model:
        cloud_context = False

        def __init__(self):
            self.requests = []

        async def chat_stream(self, messages, tools=None):
            self.requests.append(copy.deepcopy(messages))
            if len(self.requests) == 1:
                yield ChatChunk(content=text + ". Завтра дождь.", done=True)
            elif len(self.requests) == 2:
                yield ChatChunk(
                    tool_calls=[{"function": {"name": "read_test", "arguments": {}}}], done=True
                )
            else:
                yield ChatChunk(content="Понял, ты едешь учиться. Что будешь изучать?", done=True)

    async def go():
        store = MemoryStore(":memory:")
        try:
            model = Model()
            reg = ToolRegistry(
                [
                    Tool(
                        "read_test",
                        "read",
                        AsyncMock(return_value={"context": "study"}),
                        risk=Risk.READ,
                    )
                ]
            )
            conv = Conversation(Settings(language="ru-RU"), model, store, tools=reg)
            conv.add_exchange("Я еду на учёбу", "Помню, речь об учёбе.")
            answer = await ask(conv, text)
            assert not answer.startswith(text) and "дождь" not in answer
            assert "Что будешь изучать" in answer
            assert all(
                any(m["content"] == "Помню, речь об учёбе." for m in request)
                for request in model.requests
            )
            assert sum(m["content"] == text for m in store.transcript(conv.id)) == 1
            assert any(m.get("tool_name") == "read_test" for m in store.recent(conv.id, 20))
        finally:
            store.close()

    asyncio.run(go())


def test_history_turn_is_atomic_on_sqlite_failure():
    store = MemoryStore(":memory:")
    try:
        conv = Conversation(Settings(), NoModel(), store)
        store.db.execute(
            "CREATE TRIGGER reject_tool BEFORE INSERT ON messages WHEN NEW.role='tool' "
            "BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
        trace = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "read_test", "arguments": {}}}],
            },
            {"role": "tool", "tool_name": "read_test", "content": '{"ok": true}'},
        ]
        with pytest.raises(sqlite3.Error):
            conv._persist_available("Вопрос", trace, "Ответ")
        assert store.count(conv.id) == 0
    finally:
        store.close()


def test_metrics_use_first_token_and_sum_total_without_fabricated_values():
    profile = Profile()
    assert all(value is None for value in profile.summary().values())
    profile.add("model_first_chunk", 0.1)
    profile.add("model_first_chunk", 0.3)
    profile.add("model_total", 0.2)
    profile.add("model_total", 0.4)
    summary = profile.summary()
    assert summary["llm_first_token_s"] == 0.1
    assert summary["llm_total_s"] == pytest.approx(0.6)
    assert summary["tts_first_audio_s"] is None


def test_uuid_discovery_is_bounded_async_and_never_rebinds_or_writes(monkeypatch):
    settings = Settings(led={"enabled": True})
    strip = LEDStrip(settings)
    matches = {
        "one": (
            SimpleNamespace(name="ELK-BLEDOB", address="new-uuid"),
            SimpleNamespace(local_name="ELK-BLEDOB"),
        ),
        "two": (
            SimpleNamespace(name="other", address="other-uuid"),
            SimpleNamespace(local_name="other"),
        ),
    }

    async def discover(**kwargs):
        assert kwargs["timeout"] == 4
        await asyncio.sleep(0.01)
        return matches

    monkeypatch.setattr(led, "BleakScanner", SimpleNamespace(discover=discover))
    client = Mock(side_effect=AssertionError("Discovery cannot send commands"))
    monkeypatch.setattr(led, "BleakClient", client)

    async def go():
        ticks = []

        async def heartbeat():
            await asyncio.sleep(0)
            ticks.append(True)

        result, _ = await asyncio.gather(strip.discover(), heartbeat())
        assert ticks
        assert [(c["name"], c["address"], c["compatibility"]) for c in result["candidates"]] == [
            ("ELK-BLEDOB", "new-uuid", "candidate"),
            ("other", "other-uuid", "unknown"),
        ]
        assert str(strip.cfg.address) == "3a4f5301-2291-2e56-628d-83567e951876"
        assert not strip.path.exists()

    asyncio.run(go())


def test_functionality_and_truth_remain_above_character():
    prompt = build_system_prompt(Settings(language="ru-RU"))
    assert "Сарказм не заменяет" in prompt
    assert "реальную причину" in prompt and "Не повторяй текст STT" in prompt
    assert is_short("Чушпан, давай, открой")
    assert command_text("Привет, Чушпан. Открой YouTube") == "открой youtube"


def test_benchmark_reuses_client_and_closes_it_once(monkeypatch, capsys):
    from backend import latency_check

    class Model:
        last_provider = "test"
        last_model = "test-model"

        def __init__(self):
            self.calls = 0
            self.aclose = AsyncMock()

        async def chat_stream(self, *args, **kwargs):
            self.calls += 1
            yield ChatChunk(content="Тестовый ответ.", done=True)

    model = Model()
    factory = Mock(return_value=model)
    monkeypatch.setattr(latency_check, "create_llm", factory)
    assert asyncio.run(latency_check.run(Settings(language="ru-RU"), live=True, runs=3)) == 0
    assert model.calls == 3
    factory.assert_called_once()
    model.aclose.assert_awaited_once()
    output = capsys.readouterr().out
    assert '"llm_total_s": null' not in output


def test_voice_greeting_and_led_command_is_one_addressed_request():
    import numpy as np

    from tests.test_voice_loop import FRAME, FakeConversation, FakeSpeaker, Harness

    async def go():
        h = Harness(text="Привет, Чушпан. Сделай ленту светло-розовой", phrase="эй чушпан")
        h.s.language, h.s.assistant_name = "ru-RU", "Чушпан"
        conv = FakeConversation("Команда отправлена.")
        conv.s.language = "ru-RU"
        requests = []

        async def reply(text):
            requests.append(text)
            yield conv.text

        conv.reply = reply
        h.loop.conv, h.loop.speaker = conv, FakeSpeaker()
        await h.loop._check_wake_phrase(np.tile(FRAME, 20), time.perf_counter())
        assert h.loop.task is not None
        await h.loop.task
        assert requests == ["Сделай ленту светло-розовой"]
        assert h.stt.calls == 1
        stats = [e for e in h.loop.bus.snapshot() if e["type"] == "stats"][-1]
        assert stats["total_response_s"] is not None
        assert stats["llm_total_s"] is None and not stats["model_used"]

    asyncio.run(go())


def test_full_tool_registry_keeps_local_history_instead_of_negative_budget():
    from backend.brain.llm import OllamaClient
    from backend.config import ROOT_DIR
    from backend.tools import build_registry

    settings = Settings(language="ru-RU", persona={"file": ROOT_DIR / "personality.md"})
    store = MemoryStore(":memory:")
    try:
        registry = build_registry(settings, store=store)
        conv = Conversation(settings, NoModel(), store, tools=registry)
        history = []
        for _ in range(10):
            history.extend(
                [
                    {"role": "user", "content": "Я еду учиться программированию."},
                    {"role": "assistant", "content": "Помню, это учёба, а не отпуск."},
                ]
            )
        client = OllamaClient(settings.llm, client=SimpleNamespace())
        payload = client._payload(conv._messages("Зачем я еду?", history), True, registry.schemas())
        assert len(registry) >= 35
        assert all(message in payload["messages"] for message in history)
        assert payload["options"]["num_ctx"] == settings.llm.num_ctx
    finally:
        store.close()


def test_context_fit_never_drops_current_tool_round_or_modifies_sqlite_copy():
    from backend.brain.followups import fit_local_context

    history = [{"role": "system", "content": "Системные правила."}]
    for i in range(30):
        history.extend(
            [
                {"role": "user", "content": ("Старый вопрос " + str(i)) * 50},
                {"role": "assistant", "content": "Старый ответ " * 50},
            ]
        )
    current_turn = [
        {"role": "user", "content": "Посмотри список плейлистов"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "music_playlist", "arguments": {"action": "list"}}}
            ],
        },
        {
            "role": "tool",
            "tool_name": "music_playlist",
            "content": '{"playlists":[{"title":"Verified"}]}',
        },
    ]
    original = copy.deepcopy(history + current_turn)
    fitted = fit_local_context(original, [], 4096)
    assert fitted[0] == history[0] and fitted[-3:] == current_turn
    assert len(fitted) < len(original) and original == history + current_turn


def test_greeting_does_not_bypass_destructive_confirmation():
    handler = AsyncMock()
    tool = Tool(
        "mac_power",
        "test",
        handler,
        {"action": {"type": "string", "enum": ["shutdown"]}},
        ["action"],
    )
    registry = ToolRegistry([tool])

    async def go():
        with request_scope(
            RequestContext(DeviceContext.local_mac(), "Привет, Чушпан. Выключи Mac")
        ):
            result = await registry.call("mac_power", {"action": "shutdown"})
            assert '"status": "confirmation_required"' in result
            handler.assert_not_awaited()

    asyncio.run(go())


def test_fallback_can_warm_local_cache_after_cloud_failure():
    from backend.brain.providers import FallbackClient

    async def go():
        local = SimpleNamespace(prime=AsyncMock(), warmup=AsyncMock())
        client = FallbackClient(SimpleNamespace(available=True), local)
        await client.prime("prompt")
        local.prime.assert_not_awaited()
        client.last_provider = "ollama"
        assert client.needs_local_warmup
        await client.prime("prompt", [], [])
        await client.warmup()
        local.prime.assert_awaited_once_with("prompt", [], [])
        local.warmup.assert_awaited_once()
        client.last_provider = "gemini"
        assert not client.needs_local_warmup

    asyncio.run(go())


def test_new_request_cancels_background_prime_instead_of_waiting_for_it():
    async def go():
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        class Model(NoModel):
            cloud_context = True
            needs_local_warmup = True

            async def prime(self, *args):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        settings = Settings(language="ru-RU")
        store = MemoryStore(":memory:")
        try:
            conv = Conversation(
                settings,
                Model(),
                store,
                tools=ToolRegistry(clock_tools(settings)),
                warm_after_turn=True,
            )
            conv.schedule_warm()
            await entered.wait()
            answer = await asyncio.wait_for(ask(conv, "Сколько времени?"), 1)
            assert "Сейчас" in answer and cancelled.is_set()
        finally:
            store.close()

    asyncio.run(go())
