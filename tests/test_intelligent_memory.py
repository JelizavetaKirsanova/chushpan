"""Behavior tests for local memory; all user/device/model data here is synthetic."""

import asyncio
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.brain.conversation import Conversation
from backend.brain.llm import ChatChunk
from backend.config import Settings
from backend.context import DeviceContext, RequestContext, request_scope
from backend.hud.bus import HudBus
from backend.memory import backup
from backend.memory.intelligence import (
    action_context,
    action_state,
    delete_conversation,
    disable_conversation,
    evidence,
    history,
    record_action,
    summary,
)
from backend.memory.library import MemoryLibrary, search
from backend.memory.profile import ProfileMemory, extract
from backend.memory.store import MemoryStore, _now
from backend.settings_center import SettingsCenter
from backend.tools import build_registry


@pytest.fixture
def store(tmp_path):
    result = MemoryStore(tmp_path / "memory.sqlite")
    yield result
    result.close()


@pytest.fixture
def library(store, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    return MemoryLibrary(store, Settings(), HudBus())


def run(service, **value):
    return asyncio.run(service.handle(value))


def user(store, text, session="test"):
    context = RequestContext(DeviceContext.local_mac(session), text)
    cid = store.session_conversation(session)
    store.add(cid, "user", text, source=context)
    mid = store.db.execute("SELECT MAX(id) FROM messages").fetchone()[0]
    return context, cid, mid


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("Меня зовут Лена", "profile"),
        ("Не называй меня крошкой", "correction"),
        ("Мой коллега Иван работает над Орионом", "people"),
        ("Я изучаю алгоритмы", "study"),
        ("Я работаю над Орионом", "work"),
        ("Мой компьютер MacBook поддерживает BLE", "device"),
        ("Мой телефон iPhone", "device"),
        ("Мой сценарий Орион включает тёплый свет", "home"),
        ("Я не люблю аниме", "entertainment"),
        ("Я обычно ложусь в полночь", "habit"),
        ("Мы решили изучать графы", "conversation"),
        ("Этот способ не работает", "correction"),
        ("Теперь лента работает", "correction"),
        ("Мой план изучить Python", "goal"),
        ("Завтра экзамен", "temporary"),
    ],
)
def test_categories_extract_only_user_statements(text, category):
    assert extract(text)[0][0] == category


@pytest.mark.parametrize(
    "text",
    [
        "Возможно, я не люблю аниме",
        "Я люблю аниме?",
        "«Я не люблю аниме»",
        "Мне кажется, завтра экзамен",
        "Мой компьютер имеет пароль: example-secret",
        "Моя девушка Мария",
        "Мой диагноз астма",
        "Я живу в Таллине на улице Примерной 5",
        "Я предпочитаю лечение инсулином",
        "Мой доход 3000 евро",
        "Мой компьютер находится по координатам 59.123456, 24.654321",
    ],
)
def test_uncertainty_sensitive_and_secrets_never_become_facts(text):
    assert extract(text) == []


def test_capture_is_local_linked_and_expires_tomorrow(store):
    memory = ProfileMemory(store)
    context, cid, mid = user(store, "Завтра экзамен")
    with request_scope(context):
        memory.capture(context.text, mid)
    fact = store.memories()[0]
    assert fact["category"] == "temporary" and fact["expires_at"]
    assert datetime.fromisoformat(fact["expires_at"]) > datetime.now(UTC)
    assert store.db.execute("SELECT message_id FROM memory_evidence").fetchone()[0] == mid
    assert json.loads(fact["source"])["request_id"] == context.request_id
    assert len(memory.relevant("экзамен")) == 1
    # No network client or background cloud generation is involved.
    assert store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_conflicting_preferences_require_review_then_explicit_change_retains_versions(store):
    memory = ProfileMemory(store)
    for text in ("Я не люблю аниме", "Я люблю аниме"):
        context, _, mid = user(store, text)
        with request_scope(context):
            memory.capture(text, mid)
    assert len(store.memories()) == 1
    assert "не люблю" in store.memories()[0]["content"]
    assert len(store.preference("memory", "pending")) == 1
    context, _, mid = user(store, "Я теперь люблю аниме")
    with request_scope(context):
        memory.capture(context.text, mid)
    assert store.memories()[0]["revision"] == 2
    assert "теперь люблю" in store.memories()[0]["content"]
    assert "не люблю" in store.db.execute("SELECT content FROM memory_versions").fetchone()[0]


def test_deleted_fact_removes_provenance_versions_entities_and_fts_and_stays_forgotten(store):
    memory = ProfileMemory(store)
    context, cid, mid = user(store, "Я учусь в TalTech")
    with request_scope(context):
        memory.capture(context.text, mid)
    fact = store.memories()[0]
    store.save_memory("Я теперь учусь в TalTech на IT", key="education")
    assert store.db.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0] == 1
    assert history(store, "TalTech")
    store.forget_fact(fact["id"])
    for table in ("memory_evidence", "memory_versions", "memory_entity_links", "memory_entities"):
        assert store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert history(store, "TalTech") == []
    assert (
        store.db.execute(
            "SELECT rowid FROM history_fts WHERE history_fts MATCH 'taltech'"
        ).fetchall()
        == []
    )
    assert search(store, "TalTech") == []
    assert store.transcript(cid)  # user may independently delete original history
    with request_scope(context):
        memory.capture(context.text, mid)
    assert store.memories() == []
    store.set_preference("settings", "memory", {"automatic": "ask"})
    with request_scope(context):
        memory.capture(context.text)
    assert not store.preference("memory", "pending")


def test_restart_does_not_rebuild_blocked_history_index(store):
    context, cid, mid = user(store, "Синтетический Орион")
    idx, _ = store.save_memory("Синтетический Орион")
    evidence(store, idx, mid)
    store.forget_fact(idx)
    path = store.db.execute("PRAGMA database_list").fetchone()[2]
    reopened = MemoryStore(path)
    try:
        assert history(reopened, "Орион") == []
        assert (
            reopened.db.execute(
                "SELECT rowid FROM history_fts WHERE history_fts MATCH 'орион'"
            ).fetchall()
            == []
        )
        assert reopened.db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        reopened.close()


def test_private_conversation_removes_automatic_learning_and_stops_new_history(store):
    context, cid, mid = user(store, "Я учусь в TalTech")
    memory = ProfileMemory(store)
    with request_scope(context):
        memory.capture(context.text, mid)
    disable_conversation(store, cid)
    store.add(cid, "user", "Я не люблю аниме", source=context)
    with request_scope(context):
        memory.capture("Я не люблю аниме")
        record_action(store, "led_control", {}, {"sent": True})
    assert store.memories() == []
    assert store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM memory_actions").fetchone()[0] == 0
    assert history(store, "TalTech") == []


@pytest.mark.parametrize("also_facts", [False, True])
def test_transcript_deletion_can_keep_or_delete_facts_and_cascades(store, also_facts):
    _, cid, mid = user(store, "Мой проект Орион")
    idx, _ = store.save_memory("Мой проект Орион")
    evidence(store, idx, mid)
    delete_conversation(store, cid, facts=also_facts)
    assert bool(store.memories()) is not also_facts
    for table in ("messages", "memory_evidence", "dialogue_sessions"):
        assert store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert (
        store.db.execute("SELECT rowid FROM history_fts WHERE history_fts MATCH 'орион'").fetchall()
        == []
    )
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_fts_russian_inflections_yo_and_bounded_typos(store):
    idx, _ = store.save_memory("Я занимаюсь алгоритмами и люблю тёплый свет")
    for query in ("алгоритмы", "алгоритмов", "теплый", "тёплый", "алгортмы"):
        assert [r["id"] for r in search(store, query)] == [idx], query
        assert [r["id"] for r in ProfileMemory(store).relevant(query)] == [idx], query
    assert ProfileMemory(store).relevant("космический вакуум") == []


def test_categories_sort_and_paging_apply_before_limit(library):
    for i in range(105):
        library.store.save_memory(f"Факт проекта {i}", category="project")
    library.store.save_memory("Алгоритмы", category="study")
    assert len(run(library, op="list", category="study")["data"]["items"]) == 1
    page1 = run(library, op="list", sort="oldest")["data"]["items"]
    page2 = run(library, op="list", sort="oldest", page=1)["data"]["items"]
    assert len(page1) == 100 and len(page2) == 6
    assert not {r["id"] for r in page1} & {r["id"] for r in page2}


def test_sensitive_requires_separate_consent_and_context_opt_in(library):
    with pytest.raises(ValueError, match="согласие"):
        run(library, op="add", content="Мой диагноз астма")
    run(library, op="add", content="Мой диагноз астма", consent_sensitive=True)
    memory = ProfileMemory(library.store)
    assert memory.relevant("астма") == []
    run(library, op="policy", value={"sensitive_context": True})
    assert len(memory.relevant("астма")) == 1
    with pytest.raises(ValueError):
        library.store.save_memory("Мой диагноз астма", automatic=True)


def test_preview_import_is_read_only_atomic_and_requires_current_token(library):
    raw = json.dumps({"schema": 1, "items": [{"content": "Проект Орион", "category": "work"}]})
    preview = run(library, op="import_preview", content=raw)["data"]["import_preview"]
    assert preview["count"] == 1 and library.store.memories() == []
    assert search(library.store, "Орион") == []
    with pytest.raises(ValueError, match="устарел"):
        run(library, op="import_apply", token="wrong", confirmed=True)
    run(library, op="import_apply", token=preview["token"], confirmed=True)
    assert len(library.store.memories()) == 1
    assert library.store.memories()[0]["confirmed"] == 0
    assert ProfileMemory(library.store).relevant("Орион") == []
    with pytest.raises(ValueError, match="устарел"):
        run(library, op="import_apply", token=preview["token"], confirmed=True)


def test_invalid_preview_rolls_back_all_rows_and_can_retry(library):
    raw = json.dumps(
        {"schema": 1, "items": [{"content": "Проект Орион"}, {"content": "Люблю пароль: fake"}]}
    )
    with pytest.raises(ValueError, match="Секреты"):
        run(library, op="import_preview", content=raw)
    assert library.store.memories() == []
    assert not library.store.db.in_transaction
    assert search(library.store, "Орион") == []
    assert library.pending_import is None


def test_sensitive_import_preview_does_not_grant_import_consent(library):
    raw = json.dumps({"schema": 1, "items": [{"content": "Мой диагноз астма", "sensitive": True}]})
    preview = run(library, op="import_preview", content=raw)["data"]["import_preview"]
    assert preview["sensitive"] == 1 and library.store.memories() == []
    with pytest.raises(ValueError, match="отдельное согласие"):
        run(library, op="import_apply", token=preview["token"], confirmed=True)
    run(library, op="import_apply", token=preview["token"], confirmed=True, consent_sensitive=True)
    assert library.store.memories()[0]["sensitive"] == 1


def test_relations_entities_and_exports_roundtrip_scoped(library, tmp_path):
    for content in ("Проект Орион", "Решение Орион"):
        run(library, op="add", content=content, entities=["Орион"])
    first, second = library.store.memories()
    run(library, op="link", id=first["id"], related_id=second["id"], kind="solves")
    entity = library.snapshot()["entities"][0]
    assert len(search(library.store, "", entity=entity["id"])) == 2
    raw = run(library, op="export")["download"]["content"]
    target = MemoryStore(tmp_path / "import.sqlite")
    try:
        other = MemoryLibrary(target, Settings(), HudBus())
        preview = run(other, op="import_preview", content=raw)["data"]["import_preview"]
        run(other, op="import_apply", token=preview["token"], confirmed=True)
        assert len(other.snapshot()["relations"]) == 1
        assert len(other.snapshot()["entities"]) >= 1
    finally:
        target.close()


def test_encrypted_backup_has_private_permissions_and_never_persists_password(library):
    run(library, op="add", content="Люблю программировать")
    phrase = "test-only-long-passphrase"
    result = run(library, op="backup", password=phrase)
    name = result["data"]["backups"][0]["name"]
    path = library.backup_folder() / name
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert "программировать".encode() not in path.read_bytes()
    assert phrase.encode() not in path.read_bytes()
    with pytest.raises(ValueError, match="парольную"):
        backup.read(path.parent, name, "wrong-long-password")
    doc = backup.read(path.parent, name, phrase)
    assert doc["items"][0]["content"] == "Люблю программировать"
    with pytest.raises(ValueError):
        backup.read(path.parent, "../outside.enc", phrase)
    assert not library.store.db.execute(
        "SELECT 1 FROM preferences WHERE value LIKE ?", ("%" + phrase + "%",)
    ).fetchone()
    run(library, op="backup_delete", name=name, confirmed=True)
    assert not path.exists()


@pytest.mark.parametrize(
    ("result", "state"),
    [
        ({"ok": True}, "unverified"),
        ({"sent": True}, "sent"),
        ({"ok": True, "delivery_state": "sent_unconfirmed"}, "sent"),
        ({"acknowledged": True}, "acknowledged"),
        ({"physical_confirmed": True}, "confirmed"),
        ({"verified": True}, "confirmed"),
        ({"error": "failed", "physical_confirmed": True}, "error"),
    ],
)
def test_action_verification_is_not_inferred_from_ok(result, state):
    assert action_state(result) == state


def test_action_results_are_scoped_retrievable_and_off_means_no_learning(store):
    context, cid, _ = user(store, "Сделай свет фиолетовым")
    with request_scope(context):
        record_action(store, "led_control", {"color": "purple"}, {"sent": True})
        record_action(store, "led_control", {}, {"error": "Нет ответа"})
    rows = store.db.execute("SELECT * FROM memory_actions").fetchall()
    assert [r["state"] for r in rows] == ["sent", "error"]
    assert "не подтверждён" in action_context(store, "свет")[0]
    store.set_preference("settings", "memory", {"automatic": "off"})
    with request_scope(context):
        record_action(store, "led_control", {}, {"verified": True})
    assert store.db.execute("SELECT COUNT(*) FROM memory_actions").fetchone()[0] == 2
    for i in range(15):
        store.add(cid, "user", f"Обсуждали алгоритмы {i}", source=context)
    summary(store, cid)
    assert store.db.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0] == 0


def test_summary_is_literal_bounded_and_excludes_sensitive_messages(store):
    for i in range(15):
        _, cid, _ = user(store, f"Обсуждали алгоритмы {i}")
    store.add(cid, "user", "Мой диагноз астма")
    store.add(cid, "assistant", "У пользователя пять собак")
    summary(store, cid)
    row = store.db.execute("SELECT * FROM conversation_summaries").fetchone()
    assert len(row["content"]) <= 1400
    assert "алгоритмы" in row["content"]
    assert "астма" not in row["content"] and "собак" not in row["content"]


class Model:
    cloud_context = True

    def __init__(self):
        self.payloads = []

    async def chat_stream(self, messages, tools=None):
        self.payloads.append(messages)
        yield ChatChunk(content="Понял.", done=True)


def test_response_precedes_extraction_and_model_guess_never_becomes_fact(store):
    settings = Settings(language="ru-RU", tools={"enabled": ["memory"]})
    model = Model()
    conv = Conversation(settings, model, store, tools=build_registry(settings, store=store))

    async def go():
        stream = conv.reply("Я не люблю аниме")
        assert await anext(stream)
        assert store.memories() == []
        async for _ in stream:
            pass
        assert len(store.memories()) == 1
        await conv.tools.memory.after_response(
            conv.id, RequestContext(conv.device, "Модель придумала факт")
        )
        assert len(store.memories()) == 1

    asyncio.run(go())
    assert len(model.payloads) == 1


def test_private_voice_command_is_immediate_without_confirmation(store):
    settings = Settings(language="ru-RU", tools={"enabled": ["memory"]})
    conv = Conversation(settings, Model(), store, tools=build_registry(settings, store=store))

    async def go():
        answer = "".join([piece async for piece in conv.reply("Не запоминай этот разговор")])
        assert "Не буду сохранять" in answer
        assert not conv.tools.confirmations.pending
        assert store.count(conv.id) == 0
        async for _ in conv.reply("Я не люблю аниме"):
            pass
        assert store.count(conv.id) == 0 and store.memories() == []

    asyncio.run(go())


def test_explicit_voice_commands_and_source_explanation(store):
    settings = Settings(language="ru-RU", tools={"enabled": ["memory"]})
    conv = Conversation(settings, Model(), store, tools=build_registry(settings, store=store))

    async def ask(text):
        return "".join([x async for x in conv.reply(text)])

    async def go():
        prompt = await ask("Чушпан, запомни, что я не люблю аниме")
        assert "Подтверждаю" in prompt
        code = next(iter(conv.tools.confirmations.pending.values())).code
        answer = await ask("Подтверждаю " + code)
        assert "Запомнил" in answer and len(store.memories()) == 1
        await ask("Что ты обо мне помнишь?")
        explanation = await ask("Откуда ты это знаешь?")
        assert "Источник" in explanation and "уверенность" in explanation
        assert conv.tools.memory.route("Исправь это в своей памяти") == {"action": "clarify"}
        assert conv.tools.memory.route("Помнишь, мы обсуждали алгоритмы?")["action"] == "recall"
        assert conv.tools.memory.route("Что мы решили вчера?")["action"] == "recall"

    asyncio.run(go())


def test_search_and_context_remain_bounded_on_large_offline_history(store):
    cid = store.new_conversation()
    with store.memory_transaction():
        store.db.executemany(
            "INSERT INTO facts(content,created_at,updated_at) VALUES(?,?,?)",
            [(f"Проект Орион {i}", _now(), _now()) for i in range(3000)],
        )
        store.db.executemany(
            "INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)",
            [(cid, "user", f"Алгоритмы {i}", _now()) for i in range(10000)],
        )
    start = time.perf_counter()
    selected = ProfileMemory(store).relevant("Орион", limit=4, budget=150)
    assert time.perf_counter() - start < 1
    assert len(selected) <= 4 and sum(len(r["content"]) for r in selected) <= 150
    assert store.db.execute("SELECT COUNT(*) FROM memory_metrics").fetchone()[0] < 10
