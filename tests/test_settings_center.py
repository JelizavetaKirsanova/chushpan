"""Production settings/SQLite/scenario paths; no device writes or paid APIs."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from backend.automation.models import Block, Scenario
from backend.brain.conversation import Conversation
from backend.brain.llm import ChatChunk
from backend.config import Settings
from backend.context import DeviceContext, RequestContext, request_scope, voice_addressed
from backend.hud.bus import HudBus
from backend.memory.profile import ProfileMemory
from backend.memory.store import MemoryStore
from backend.personality import TRAITS, PersonalityProfile, personalities
from backend.settings_center import SettingsCenter
from backend.settings_models import Models, apply_persisted, quiet_now
from backend.tools import build_registry


class Model:
    def __init__(self):
        self.requests = []

    async def chat_stream(self, messages, tools=None):
        self.requests.append((messages, tools))
        yield ChatChunk(content="Короткий проверочный ответ.", done=True)

    async def prime(self, *args):
        pass


def fixture(path=":memory:"):
    s = Settings(language="ru-RU")
    s.llm.provider = "ollama"
    store, bus = MemoryStore(path), HudBus()
    tools = build_registry(s, store=store, local_mac=False)
    model = Model()
    conv = Conversation(s, model, store, tools=tools)
    center = SettingsCenter(s, store, conv, bus)
    return s, store, bus, model, conv, center


def event(bus):
    return next(e for e in bus.snapshot() if e["type"] == "settings_center")


def test_profiles_migrate_legacy_independently_and_survive_restart(tmp_path):
    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    store.save_memory("Сарказм: 2", category="personality", key="character:sarcasm")
    conversation = store.new_conversation()
    store.add(conversation, "user", "История должна остаться.")
    profiles = personalities(store)
    assert len(profiles.list()) == 5 and profiles.active().traits["sarcasm"] == 20
    assert profiles.note() == ""  # Merely opening settings does not change the prompt.
    copied = profiles.copy("calm", "Учёба")
    copied.traits["verbosity"] = 5
    profiles.save(copied)
    profiles.select(copied.id, locked=True)
    assert profiles.get("calm").traits["verbosity"] != 5
    store.close()
    reopened = MemoryStore(path)
    restored = personalities(reopened)
    assert restored.active().name == "Учёба" and restored.active().traits["verbosity"] == 5
    assert restored.selection()["locked"]
    assert reopened.recent(conversation, 10)[0]["content"] == "История должна остаться."
    reopened.close()


def test_presets_revision_conflicts_and_active_deletion_are_guarded():
    store = MemoryStore(":memory:")
    profiles = personalities(store)
    with pytest.raises(ValueError, match="скопировать"):
        profiles.save(profiles.get("calm"))
    with pytest.raises(ValueError, match="Предустановки"):
        profiles.delete("calm", 1)
    p = profiles.copy("calm")
    profiles.save(p)
    with pytest.raises(ValueError, match="другом окне"):
        profiles.save(p)
    profiles.select(p.id)
    with pytest.raises(ValueError, match="активный"):
        profiles.delete(p.id, profiles.get(p.id).revision)
    profiles.select("business")
    profiles.delete(p.id, profiles.get(p.id).revision)
    assert p.id not in {v.id for v in profiles.list()}
    store.close()


def test_saved_character_reaches_real_text_and_voice_prompt_once_without_history_loss():
    async def run():
        s, store, bus, model, conv, center = fixture()
        baseline = conv.system_prompt
        p = center.profiles.copy("calm", "Моя работа")
        p.instructions = "Используй редкое слово: проверка-профиля."
        p.communication.address = "вы"
        center.profiles.save(p)
        center.profiles.select(p.id)
        answer = "".join([v async for v in conv.reply("Объясни, почему небо голубое.")])
        prompt = model.requests[-1][0][0]["content"]
        assert answer and prompt != baseline
        assert prompt.count(p.instructions) == 1 and "на вы" in prompt
        assert "не меняют полномочия" in prompt.casefold()
        assert len(prompt) <= len(baseline) + 1000
        assert store.recent(conv.id, 20)[0]["content"] == "Объясни, почему небо голубое."
        await center.close()
        store.close()

    asyncio.run(run())


def test_preview_is_local_sandboxed_and_does_not_activate_or_store_turn(monkeypatch):
    async def run():
        s, store, bus, model, conv, center = fixture()
        captured = []

        class Local:
            def __init__(self, config):
                captured.append(config)

            async def chat_stream(self, messages, tools):
                assert tools == [] and len(messages) == 2
                yield ChatChunk(content="Первый фрагмент. ")
                yield ChatChunk(content="Второй фрагмент.", done=True)

            async def aclose(self):
                pass

        monkeypatch.setattr("backend.brain.llm.OllamaClient", Local)
        active, count = center.profiles.selection(), store.count(conv.id)
        await center.handle({"op": "preview", "text": "Открой Safari", "ids": ["calm", "business"]})
        assert center.profiles.selection() == active and store.count(conv.id) == count
        assert len(captured) == 1 and captured[0].provider == "ollama"
        assert len(center.last_preview) == 2 and all(
            r["first_token_s"] is not None for r in center.last_preview
        )
        assert not model.requests
        await center.close()
        store.close()

    asyncio.run(run())


def test_existing_engine_switches_one_profile_and_manual_lock_wins():
    async def run():
        s, store, bus, model, conv, center = fixture()
        automations = conv.tools.automations
        scenario = automations.store.save(
            Scenario(
                name="Учёба",
                phrases=["Пора учиться"],
                actions=[
                    Block(capability="assistant.personality", parameters={"profile_id": "business"})
                ],
            )
        )
        address = voice_addressed.set(True)
        try:
            with request_scope(RequestContext(DeviceContext.local_mac(), "Пора учиться")):
                result = await automations.engine.run(scenario, event_id="one")
            assert result["status"] == "confirmed" and center.profiles.active().id == "business"
            center.profiles.select("calm", locked=True)
            with request_scope(RequestContext(DeviceContext.local_mac(), "Пора учиться")):
                result = await automations.engine.run(scenario, event_id="two")
            assert result["status"] == "failed" and center.profiles.active().id == "calm"
            assert not model.requests
        finally:
            voice_addressed.reset(address)
            await center.close()
            store.close()

    asyncio.run(run())


def test_place_signals_are_opt_in_fresh_unambiguous_and_manual_has_priority():
    s, store, bus, model, conv, center = fixture()
    locations = conv.tools.automations.locations
    key = "a" * 64
    observed = {
        "source": "macos",
        "updated_at": datetime.now(UTC).isoformat(),
        "signals": [{"kind": "wifi", "key": key, "name": "Тестовая сеть"}],
    }
    locations.signal_observation = observed
    assert locations.current()["place_id"] is None
    dorm = locations.get("dorm")
    locations.save(dorm.model_copy(update={"signals": observed["signals"]}))
    assert locations.current()["place_id"] == "dorm" and locations.current()["city"] is None
    locations.select("manual", "home")
    assert locations.current()["place_id"] == "home"
    locations.select("auto")
    home = locations.get("home")
    locations.save(home.model_copy(update={"signals": observed["signals"]}))
    assert locations.current()["reason"] == "signal_conflict"
    locations.signal_observation = {
        **observed,
        "updated_at": (datetime.now(UTC) - timedelta(seconds=61)).isoformat(),
    }
    assert locations.current()["place_id"] is None
    store.close()


def test_policy_pending_confirmation_exclusions_and_temporary_retention():
    s, store, bus, model, conv, center = fixture()
    memory = ProfileMemory(store)
    store.set_preference(
        "settings", "memory", {"automatic": "ask", "excluded": [], "temporary_days": 3}
    )
    with request_scope(RequestContext(DeviceContext.local_mac(), "Я люблю чай")):
        memory.capture("Я люблю чай")
    assert not store.facts()
    pending = store.preference("memory", "pending")
    center.approve_memory({"id": pending[0]["id"], "approve": True})
    assert "чай" in store.facts()[0][1]
    store.set_preference(
        "settings", "memory", {"automatic": "auto", "excluded": ["preference"], "temporary_days": 3}
    )
    with request_scope(RequestContext(DeviceContext.local_mac(), "Я люблю кофе")):
        memory.capture("Я люблю кофе")
    assert not any("кофе" in content for _, content in store.facts())
    identifier, _ = store.save_memory("Временная встреча", category="episode")
    store.db.execute(
        "UPDATE facts SET updated_at=? WHERE id=?",
        ((datetime.now(UTC) - timedelta(days=4)).isoformat(), identifier),
    )
    assert not memory.relevant("Временная встреча")
    assert len(store.facts()) == 2  # Expiry excludes retrieval, does not erase history/data.
    store.close()


def test_quiet_hours_cross_midnight_follow_previous_weekday():
    s, store, bus, model, conv, center = fixture()
    store.set_preference(
        "settings",
        "quiet",
        {
            "enabled": True,
            "start": "23:00",
            "end": "07:00",
            "weekdays": [0],
            "mute": True,
            "pause_initiative": True,
        },
    )
    assert (
        quiet_now(store, s, datetime(2026, 11, 3, 1, 0, tzinfo=UTC)) is not None
    )  # Tuesday after Monday night.
    assert quiet_now(store, s, datetime(2026, 11, 3, 12, 0, tzinfo=UTC)) is None
    assert quiet_now(store, s, datetime(2026, 11, 4, 1, 0, tzinfo=UTC)) is None
    store.close()


def test_model_modes_restore_actual_parameters_without_touching_secrets():
    s, store, bus, model, conv, center = fixture()
    original = (s.llm.think, s.llm.think_for_simple, s.gemini.first_result_budget_s)
    for mode in ["fast", "quality", "balanced"]:
        store.set_preference("settings", "models", Models(mode=mode).model_dump())
        apply_persisted(s, store)
        if mode == "fast":
            assert not s.llm.think_for_simple and s.gemini.first_result_budget_s <= 5
        elif mode == "quality":
            assert s.llm.think and s.llm.think_for_simple
    assert (s.llm.think, s.llm.think_for_simple, s.gemini.first_result_budget_s) == original
    store.close()


def test_backup_preview_merge_and_invalid_import_never_delete_history():
    async def run():
        s, store, bus, model, conv, center = fixture()
        store.add(conv.id, "user", "Существующая история")
        store.save_memory("Пьёт чай")
        queue = bus.subscribe()
        await center.handle({"op": "export", "scope": "all"})
        export = next(v for v in list(queue._queue) if v["type"] == "settings_export")
        document = export["document"]
        assert "api_key" not in json.dumps(document) and "messages" not in document
        await center.handle({"op": "import_preview", "document": document})
        staged = center.pending_import
        assert staged and store.count(conv.id) == 1
        await center.handle({"op": "import_apply", "token": staged[0], "confirm": True})
        assert store.count(conv.id) == 1 and any(v[1] == "Пьёт чай" for v in store.facts())
        assert len(center.profiles.list()) == 6
        await center.handle({"op": "import_preview", "document": {"version": 9}})
        assert event(bus).get("error") and store.count(conv.id) == 1
        await center.close()
        store.close()

    asyncio.run(run())


def test_access_policy_enforced_by_registry_not_only_ui():
    async def run():
        s, store, bus, model, conv, center = fixture()
        tools = conv.tools
        store.set_preference(
            "settings", "access", {"disabled_tools": ["current_time"], "blocked_apps": []}
        )
        with request_scope(RequestContext(DeviceContext.local_mac(), "Сколько времени?")):
            result = json.loads(await tools.call("current_time", {}))
        assert "отключено" in result["error"]
        await center.close()
        store.close()

    asyncio.run(run())


@pytest.mark.parametrize("value", [-1, 101, True, 5.5])
def test_invalid_personality_values_never_save(value):
    traits = {k: 50 for k in TRAITS}
    traits["sarcasm"] = value
    with pytest.raises(ValueError):
        PersonalityProfile(name="Тест", traits=traits)


def test_legacy_character_summary_has_no_mutation_and_execution_updates_profile():
    async def run():
        s, store, bus, model, conv, center = fixture()
        memory = ProfileMemory(store)
        context = RequestContext(conv.device, "Сарказм на 2")
        args = {"action": "character", "value": json.dumps({"sarcasm": 2})}
        before = center.profiles.active().model_dump()
        with request_scope(context):
            memory.summary(args)
            assert center.profiles.active().model_dump() == before
            await memory.execute(**args, id=None)
        assert center.profiles.active().traits["sarcasm"] == 20
        await center.close()
        store.close()

    asyncio.run(run())


def test_speed_comparison_is_bounded_and_invalid_input_never_calls_model():
    async def run():
        s, store, bus, model, conv, center = fixture()
        for models in ["qwen3:8b", [], ["a", "b", "c"], [7]]:
            await center.handle({"op": "speed_test", "models": models})
            assert "две локальные" in event(bus)["error"]
        assert not model.requests and center.last_preview is None
        await center.close()
        store.close()

    asyncio.run(run())


def test_voice_studio_hot_apply_and_restart_without_playback(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from backend.audio.tts import SileroSpeaker
    from backend.settings_models import Studio

    async def run():
        path = tmp_path / "voice.sqlite"
        s, store, bus, model, conv, center = fixture(path)
        s.tts.engine = "silero"
        speaker = SileroSpeaker(s.voice_path)
        loop = SimpleNamespace(speaker=speaker, _publish_voice=Mock(), busy=False)
        center.loop = loop
        selected = Studio(
            voice="eugene",
            rate="fast",
            pitch="low",
            volume=35,
            pause_ms=200,
            wake_phrase="привет чушпан",
            text_only=True,
        )
        await center.save("studio", selected.model_dump())
        assert speaker.rate == "fast" and speaker.pitch == "low"
        assert speaker.volume == 0.35 and speaker.pause_ms == 200
        assert loop.wake_phrase == "привет чушпан" and not model.requests
        await center.close()
        store.close()
        reopened = MemoryStore(path)
        after = Settings(language="ru-RU")
        apply_persisted(after, reopened)
        assert after.wakeword.phrase == selected.wake_phrase
        assert reopened.preference("settings", "studio") == selected.model_dump()
        reopened.close()

    asyncio.run(run())


def test_microphone_test_consumes_exactly_one_utterance_without_actions():
    import time

    s, store, bus, model, conv, center = fixture()
    center.capture_target = time.monotonic() + 10
    assert center.capture("Открой Safari", True)
    assert event(bus)["captured"] == "Открой Safari"
    assert center.capture_target is None and not center.capture("Ещё", True)
    assert not model.requests and store.count(conv.id) == 0
    center.capture_target = time.monotonic() - 1
    assert center.capture("Устаревшая реплика", True)
    assert "истекло" in event(bus)["error"]
    store.close()


def test_backup_keeps_legacy_controls_and_rejects_unsupported_voice_atomically():
    from backend.audio.activity import VoiceControls
    from backend.settings_backup import Backup

    async def run():
        s, store, bus, model, conv, center = fixture()
        controls = VoiceControls(allow_barge_in=True)
        store.set_preference("voice", "controls", controls.model_dump())
        queue = bus.subscribe()
        await center.handle({"op": "export"})
        exports = []
        while not queue.empty():
            item = queue.get_nowait()
            if item["type"] == "settings_export":
                exports.append(item)
        doc = exports[-1]["document"]
        assert doc["voice_controls"] == controls.model_dump()
        assert Backup.model_validate(doc).version == 1
        doc["settings"]["studio"]["voice"] = "unsupported-voice"
        before = store.preference("settings", "studio")
        await center.handle({"op": "import_preview", "document": doc})
        await center.handle(
            {"op": "import_apply", "token": center.pending_import[0], "confirm": True}
        )
        assert "не установлен" in event(bus)["error"]
        assert store.preference("settings", "studio") == before
        bus.unsubscribe(queue)
        await center.close()
        store.close()

    asyncio.run(run())


def test_dialogue_extras_restore_without_overwriting_other_voice_controls():
    from backend.audio.activity import VoiceControls, apply_controls
    from backend.settings_models import DialogueExtras

    s, store, bus, model, conv, center = fixture()
    extras = DialogueExtras(nameless_interruptions=True, addressee_strictness="addressed")
    store.set_preference("voice", "dialogue", extras.model_dump())
    apply_persisted(s, store)
    apply_controls(s, VoiceControls(sensitivity="conservative").model_dump())
    assert s.dialogue.nameless_interruptions
    assert s.dialogue.addressee_strictness == "addressed"
    assert s.wakeword.threshold == 0.8
    store.close()


def test_profile_switch_requires_requested_name_and_cannot_change_security():
    async def run():
        s, store, bus, model, conv, center = fixture()
        context = RequestContext(DeviceContext.local_mac(), "Включи деловой режим")
        with request_scope(context):
            rejected = json.loads(
                await conv.tools.call("set_personality_profile", {"profile_id": "calm"})
            )
            assert "error" in rejected
            result = json.loads(
                await conv.tools.call("set_personality_profile", {"profile_id": "business"})
            )
        assert result["ok"] and center.profiles.selection()["id"] == "business"
        assert center.value("access")["disabled_tools"] == []
        await center.close()
        store.close()

    asyncio.run(run())


def test_bluetooth_observations_use_only_connected_devices_and_hash_identifiers():
    from backend.context_signals import bluetooth_signals

    samples = {
        "devices": [
            {"TV": {"device_connected": "attrib_Yes", "device_address": "AA-BB"}},
            {"Headphones": {"device_connected": "attrib_No", "device_address": "CC-DD"}},
        ]
    }
    observed = bluetooth_signals(samples)
    assert len(observed) == 1 and observed[0]["name"] == "TV"
    assert len(observed[0]["key"]) == 64 and "AA-BB" not in json.dumps(observed)


def test_conditional_profile_rule_waits_for_unlock_and_fires_once(monkeypatch):
    async def run():
        s, store, bus, model, conv, center = fixture()
        service = conv.tools.automations
        service.locations.select("manual", "home")
        center.save_rule({"profile_id": "business", "kind": "location", "place_id": "home"})
        rule = next(p for p in service.store.list() if p.description.startswith("Характер: "))
        spawned = []

        def spawn(coro):
            spawned.append(True)
            coro.close()

        async def one_tick(_):
            raise asyncio.CancelledError()

        monkeypatch.setattr(service, "spawn", spawn)
        monkeypatch.setattr("backend.automation.service.asyncio.sleep", one_tick)

        async def tick():
            with pytest.raises(asyncio.CancelledError):
                await service.schedule()

        center.profiles.select("mine", locked=True)
        await tick()
        assert not spawned and store.preference("automation_conditions", rule.id) is False
        center.profiles.select("mine", locked=False)
        await tick()
        assert len(spawned) == 1
        await tick()
        assert len(spawned) == 1
        service.locations.select("manual", "general")
        await tick()
        service.locations.select("manual", "home")
        await tick()
        assert len(spawned) == 2 and not model.requests
        await center.close()
        store.close()

    asyncio.run(run())


def test_profile_copy_name_validation_and_long_names_survive_reload():
    s, store, bus, model, conv, center = fixture()
    long = center.profiles.copy("calm", "я" * 80)
    copied = center.profiles.copy(long.id)
    assert len(copied.name) <= 80 and center.profiles.get(copied.id).id == copied.id
    with pytest.raises(ValueError):
        center.profiles.copy("calm", " ")
    store.close()


def test_backup_restores_builtin_selection_and_lock_but_memory_import_does_not():
    async def run():
        s, store, bus, model, conv, center = fixture()
        center.profiles.select("business", locked=True)
        queue = bus.subscribe()

        async def export(scope):
            await center.handle({"op": "export", "scope": scope})
            rows = []
            while not queue.empty():
                row = queue.get_nowait()
                if row["type"] == "settings_export":
                    rows.append(row)
            return rows[-1]["document"]

        document = await export("all")
        center.profiles.select("calm", locked=False)
        await center.handle({"op": "import_preview", "document": document})
        await center.handle(
            {"op": "import_apply", "token": center.pending_import[0], "confirm": True}
        )
        assert (
            center.profiles.selection()["id"] == "business"
            and center.profiles.selection()["locked"]
        )
        memory = await export("memory")
        center.profiles.select("calm", locked=False)
        await center.handle({"op": "import_preview", "document": memory})
        await center.handle(
            {"op": "import_apply", "token": center.pending_import[0], "confirm": True}
        )
        assert (
            center.profiles.selection()["id"] == "calm"
            and not center.profiles.selection()["locked"]
        )
        bus.unsubscribe(queue)
        await center.close()
        store.close()

    asyncio.run(run())


def test_silent_reply_streams_to_hud_without_audio_or_autoplay():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from backend.voice import reply_aloud

    async def run():
        s, store, bus, model, conv, center = fixture()
        store.set_preference("settings", "studio", {"text_only": True})
        speaker = SimpleNamespace(speak=AsyncMock(side_effect=AssertionError("audio forbidden")))
        received = []
        conv._morning_music_pending = True
        assert await reply_aloud(
            conv, speaker, "Почему небо голубое?", 20, on_silent_token=received.append
        )
        assert "".join(received) == "Короткий проверочный ответ."
        speaker.speak.assert_not_called()
        assert not conv._morning_music_pending
        await center.close()
        store.close()

    asyncio.run(run())


def test_calendar_disable_also_applies_to_existing_morning_briefing():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from backend.brain.morning import MorningRoutine

    async def run():
        s, store, bus, model, conv, center = fixture()
        store.set_preference("dashboard", "calendar_enabled", False)
        apply_persisted(s, store)
        personal = SimpleNamespace(read=AsyncMock(return_value={}))
        routine = MorningRoutine(s, None, None, None, personal=personal)
        result = "".join([piece async for piece in routine.stream(conv)])
        personal.read.assert_awaited_once_with(parts=("location",))
        assert "Календарь отключён" in result
        await center.close()
        store.close()

    asyncio.run(run())


def test_learning_habits_honors_confirmation_and_excluded_categories():
    from backend.settings_models import MemoryPolicy

    s, store, bus, model, conv, center = fixture()
    memory = ProfileMemory(store)
    store.set_preference("memory", "habits_opt_in", True)
    store.set_preference("settings", "memory", MemoryPolicy(automatic="ask").model_dump())
    for _ in range(3):
        memory.observe("open_app", {"name": "TextEdit"}, {"ok": True})
    assert not any(r["category"] == "habit" for r in store.memories())
    pending = store.preference("memory", "pending")
    assert len(pending) == 1 and pending[0]["category"] == "habit"
    store.set_preference("settings", "memory", MemoryPolicy(excluded=["habit"]).model_dump())
    for _ in range(3):
        memory.observe("open_app", {"name": "Notes"}, {"ok": True})
    assert store.preference("habits", "open_app:notes") is None
    store.close()


def test_startup_permission_check_is_read_only_and_dismissal_survives_restart():
    from unittest.mock import AsyncMock

    async def run():
        s, store, bus, model, conv, old = fixture()
        old.closed = True
        report = {
            "items": [
                {"id": "microphone", "status": "denied"},
                {"id": "location", "status": "denied"},
            ],
            "history": [],
            "scope": "test",
        }
        conv.tools.permission_center.report = AsyncMock(return_value=report)
        center = SettingsCenter(s, store, conv, bus, startup_permissions=True)
        await asyncio.gather(*center.jobs)
        conv.tools.permission_center.report.assert_awaited_once_with(
            action="check", identifier=None
        )
        assert center.permission_onboarding["visible"]
        assert center.permission_onboarding["missing"] == ["microphone"]
        await center.handle({"op": "permissions_dismiss"})
        assert not center.permission_onboarding["visible"]
        await center.handle({"op": "permissions"})
        assert not center.permission_onboarding["visible"]
        report["items"][0]["status"] = "granted"
        await center.handle({"op": "permissions"})
        assert center.permission_onboarding["missing"] == []
        assert not center.permission_onboarding["visible"]
        # Optional location refusal never blocks text or brings onboarding back.
        center2 = SettingsCenter(s, store, conv, bus, startup_permissions=True)
        await asyncio.gather(*center2.jobs)
        assert not center2.permission_onboarding["visible"]
        store.close()

    asyncio.run(run())
