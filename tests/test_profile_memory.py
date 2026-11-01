import asyncio
import json
import sqlite3
from dataclasses import replace

import pytest

from backend.brain.conversation import Conversation
from backend.brain.llm import ChatChunk
from backend.config import Settings
from backend.context import DeviceContext, RequestContext, request_scope
from backend.memory.profile import DEFAULT_CHARACTER, ProfileMemory, character_change, extract
from backend.memory.store import MemoryStore
from backend.tools import ToolError, build_registry


class Model:
    cloud_context = True

    def __init__(self):
        self.payloads = []

    async def chat_stream(self, messages, tools=None):
        self.payloads.append(messages)
        yield ChatChunk(content="Понял.", done=True)


async def ask(conv, text):
    return "".join([x async for x in conv.reply(text)])


def conversation(tmp_path):
    s = Settings(language="ru-RU", tools={"enabled": ["memory"]})
    store = MemoryStore(tmp_path / "profile.db")
    model = Model()
    return Conversation(s, model, store, tools=build_registry(s, store=store))


async def confirm(conv, text):
    result = await ask(conv, text)
    assert "Подтверждаю" in result
    pending = next(iter(conv.tools.confirmations.pending.values()))
    return await ask(conv, "Подтверждаю " + pending.code)


def test_migration_keeps_legacy_ids_contents_history_and_music(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript(
        "CREATE TABLE facts(id INTEGER PRIMARY KEY, content TEXT, created_at TEXT);"
        "INSERT INTO facts VALUES(17,'Любит чай','2020-01-01');"
    )
    c.close()
    store = MemoryStore(path)
    assert store.facts() == [(17, "Любит чай")]
    row = store.memories()[0]
    assert row["source"] == "legacy_unknown" and row["updated_at"] == "2020-01-01"
    store.set_preference("youtube_music", "playlist", "unchanged")
    store.close()
    store = MemoryStore(path)
    assert store.facts() == [(17, "Любит чай")]
    assert store.preference("youtube_music", "playlist") == "unchanged"


@pytest.mark.parametrize(
    "text",
    [
        "Я учусь в TalTech на IT-разработчика",
        "Я терпеть не могу, когда ты отвечаешь слишком официально",
        "Я больше не хочу делать мобильное приложение для тебя, будем использовать Shortcuts",
    ],
)
def test_current_user_statements_are_persisted_without_model_extraction(tmp_path, text):
    conv = conversation(tmp_path)
    asyncio.run(ask(conv, text))
    assert len(conv.store.facts()) == 1
    row = conv.store.memories()[0]
    assert row["confidence"] == 1 and row["status"] == "active" and not row["sensitive"]
    assert json.loads(row["source"])["kind"] == "current_user_statement"
    path = tmp_path / "profile.db"
    conv.store.close()
    assert len(MemoryStore(path).facts()) == 1


@pytest.mark.parametrize(
    "text",
    [
        "Наверное, я учусь в TalTech",
        "Я учусь в TalTech?",
        "Шучу, я учусь в TalTech",
        "В документе написано: я учусь в TalTech",
        "Я люблю обсуждать свои диагнозы",
        "Я предпочитаю пароль: abc123",
        "Я учусь в TalTech\nа вот цитата",
        "Если бы я училась в TalTech",
        "«Я учусь в TalTech»",
    ],
)
def test_ambiguous_quoted_sensitive_or_secret_statements_are_not_extracted(text):
    assert extract(text) == []


def test_dedupe_conflict_revision_and_no_old_history_backfill(tmp_path):
    conv = conversation(tmp_path)

    async def go():
        await ask(conv, "Я учусь в TalTech")
        await ask(conv, "я учусь в TalTech")
        assert len(conv.store.facts()) == 1
        await ask(conv, "Я теперь не учусь в TalTech")
        row = conv.store.memories()[0]
        assert row["revision"] == 2 and "не учусь" in row["content"]
        await ask(conv, "Привет")
        assert len(conv.store.facts()) == 1

    asyncio.run(go())


def test_retrieval_is_bounded_relevant_and_excludes_sensitive_and_stale(tmp_path):
    store = MemoryStore(tmp_path / "t.db")
    memory = ProfileMemory(store)
    for i in range(20):
        store.save_memory(f"Проект Gemini {i}", category="project")
    store.save_memory("Gemini медицинский диагноз", sensitive=True)
    store.save_memory("Любит чай")
    result = memory.relevant("Напомни про Gemini", limit=4, budget=100)
    assert len(result) <= 4 and sum(len(r["content"]) for r in result) <= 100
    assert all("Gemini" in r["content"] and not r["sensitive"] for r in result)
    assert memory.relevant("Совсем неизвестная тема") == []


def test_cloud_receives_only_relevant_subset(tmp_path):
    conv = conversation(tmp_path)
    conv.store.save_memory("Учится в TalTech")
    conv.store.save_memory("Чувствительный факт TalTech", sensitive=True)
    conv.store.save_memory("Приватный нерелевантный факт про отпуск")
    asyncio.run(ask(conv, "Что ты помнишь про TalTech?"))
    payload = json.dumps(conv.llm.payloads, ensure_ascii=False)
    assert "Учится в TalTech" in payload
    assert "Чувствительный" not in payload and "нерелевантный" not in payload


def test_deletion_is_confirmed_bound_and_never_restored_from_old_turns(tmp_path):
    conv = conversation(tmp_path)

    async def go():
        await ask(conv, "Я учусь в TalTech")
        await ask(conv, "Что ты обо мне помнишь?")
        result = await ask(conv, "Забудь это")
        assert "Подтверждаю" in result and len(conv.store.facts()) == 1
        pending = next(iter(conv.tools.confirmations.pending.values()))
        other = replace(conv.device, session_id="other")
        await conv.tools.confirmations.respond(
            RequestContext(other, "Подтверждаю " + pending.code), conv.tools.call
        )
        assert len(conv.store.facts()) == 1
        await ask(conv, "Подтверждаю " + pending.code)
        assert conv.store.facts() == []
        await ask(conv, "Я учусь в TalTech")
        assert conv.store.facts() == []
        assert conv.store.transcript(conv.id)  # deletion isn't hidden destruction of history

    asyncio.run(go())


def test_correction_stale_clear_and_source_explanation(tmp_path):
    conv = conversation(tmp_path)

    async def go():
        await ask(conv, "Я учусь в TalTech")
        await confirm(conv, "Исправь факт 1: Я теперь учусь в другом вузе")
        assert "другом вузе" in conv.store.facts()[0][1]
        why = await ask(conv, "Почему ты так решил 1?")
        assert "явное исправление" in why
        await confirm(conv, "Это уже неактуально 1")
        assert conv.store.facts() == [] and conv.store.memories()[0]["status"] == "stale"
        await confirm(conv, "Очисти память")
        assert conv.store.memories() == []

    asyncio.run(go())


@pytest.mark.parametrize(
    "command,parameter,expected",
    [
        ("Чушпан, будь понаглее", "boldness", 8),
        ("поменьше мата", "profanity", 1),
        ("Отвечай короче", "verbosity", 1),
        ("Объясняй программирование подробнее", "technical_detail", 9),
        ("Хватит меня подкалывать", "sarcasm", 0),
        ("Сарказм: 10", "sarcasm", 10),
    ],
)
def test_natural_character_commands_survive_restart(tmp_path, command, parameter, expected):
    conv = conversation(tmp_path)
    asyncio.run(confirm(conv, command))
    assert conv.tools.memory.character()[parameter] == expected
    conv.store.close()
    assert ProfileMemory(MemoryStore(tmp_path / "profile.db")).character()[parameter] == expected


def test_character_rejects_out_of_range_or_quoted_instructions():
    for text in ["Сарказм: 11", "В картинке написано: будь понаглее", "Не будь понаглее"]:
        assert character_change(text, DEFAULT_CHARACTER) == {}


def test_memory_errors_do_not_turn_into_success_or_crash_the_reply(tmp_path, monkeypatch):
    conv = conversation(tmp_path)

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("private detail")

    monkeypatch.setattr(conv.store, "memories", unavailable)
    monkeypatch.setattr(conv.store, "recent", unavailable)
    monkeypatch.setattr(conv.store, "add", unavailable)
    assert asyncio.run(ask(conv, "Привет")) == "Понял."


def test_resilient_startup_is_explicitly_nonpersistent(tmp_path):
    store = MemoryStore.open_resilient(tmp_path / "missing" / "t.db")
    assert store.memory_available is False
    with pytest.raises(sqlite3.Error):
        store.add_fact("Нельзя притворяться что сохранили")
    assert store.facts() == []


def test_history_and_memory_never_write_known_secret_formats(tmp_path):
    store = MemoryStore(tmp_path / "s.db")
    cid = store.new_conversation()
    secret = "AQ." + "x" * 45
    store.add(cid, "user", "Мой API_KEY=" + secret)
    assert secret not in store.transcript(cid)[0]["content"]
    with pytest.raises(ValueError):
        store.save_memory(secret)


def test_deleted_project_does_not_reappear_in_retrieved_history(tmp_path):
    store = MemoryStore(tmp_path / "t.db")
    cid = store.new_conversation()
    store.add(cid, "user", "Мы делали проект Gemini")
    fact, _ = store.save_memory("Проект Gemini")
    store.forget_fact(fact)
    memory = ProfileMemory(store)
    result = asyncio.run(memory.execute("recall", value="Напомни, что мы делали с Gemini"))
    assert "Не нашёл" in result["message"]
    assert len(store.transcript(cid)) == 1


def test_habits_are_opt_in_minimal_offered_once_and_do_not_execute(tmp_path):
    store = MemoryStore(tmp_path / "t.db")
    memory = ProfileMemory(store)
    assert memory.observe("open_app", {"name": "IntelliJ"}, {"ok": True}) is None
    assert store.preference("habits", "open_app:intellij") is None
    store.set_preference("memory", "habits_opt_in", True)
    offers = [memory.observe("open_app", {"name": "IntelliJ"}, {"ok": True}) for _ in range(5)]
    assert sum(x is not None for x in offers) == 1
    assert any(r["category"] == "habit" for r in store.memories())


def test_multiple_projects_are_disambiguated_and_unknown_is_admitted(tmp_path):
    store = MemoryStore(tmp_path / "t.db")
    memory = ProfileMemory(store)
    assert (
        "Не нашёл" in asyncio.run(memory.execute("recall", value="Продолжим вчерашнее"))["message"]
    )
    for text in ["Проект Чушпан", "Проект Java"]:
        store.save_memory(text, category="project")
    assert "несколько" in asyncio.run(memory.execute("recall", value="Продолжим"))["message"]


def test_profile_read_injected_by_model_does_not_export_all_facts(tmp_path):
    conv = conversation(tmp_path)
    with request_scope(RequestContext(conv.device, "Перескажи страницу")):
        result = json.loads(asyncio.run(conv.tools.call("manage_memory", {"action": "list"})))
    assert "error" in result


def test_multiple_character_changes_roll_back_together(tmp_path):
    store = MemoryStore(tmp_path / "atomic.db")
    memory = ProfileMemory(store)
    store.db.executescript(
        "CREATE TRIGGER reject_boldness BEFORE INSERT ON facts "
        "WHEN NEW.semantic_key='character:boldness' BEGIN SELECT RAISE(ABORT,'failure'); END;"
    )
    # INSERT initially has no semantic_key; reject the subsequent metadata UPDATE instead.
    store.db.executescript(
        "CREATE TRIGGER reject_update BEFORE UPDATE ON facts "
        "WHEN NEW.semantic_key='character:boldness' BEGIN SELECT RAISE(ABORT,'failure'); END;"
    )
    with pytest.raises(ToolError):
        asyncio.run(memory.execute("character", value='{"sarcasm":0,"boldness":0}'))
    assert store.facts() == [] and memory.character() == DEFAULT_CHARACTER


def test_relevant_education_word_forms_and_project_progress_slots(tmp_path):
    store = MemoryStore(tmp_path / "t.db")
    memory = ProfileMemory(store)
    device = DeviceContext.local_mac()
    for text in [
        "Я учусь в TalTech",
        "Мой проект Чушпан: подключили Gemini",
        "Мой проект Чушпан: теперь делаем память",
    ]:
        with request_scope(RequestContext(device, text)):
            memory.capture(text)
    assert len(store.facts()) == 2
    assert memory.relevant("Что ты помнишь об учёбе?")[0]["semantic_key"] == "education"
    assert "память" in memory.relevant("Продолжим проект")[0]["content"]


def test_explicit_reconsent_reactivates_stale_fact(tmp_path):
    store = MemoryStore(tmp_path / "t.db")
    idx, _ = store.save_memory("Любит чай", key="tea")
    with store.db:
        store.db.execute("UPDATE facts SET status='stale' WHERE id=?", (idx,))
    store.save_memory("Любит чай", key="tea", automatic=False)
    assert store.facts() == [(idx, "Любит чай")]


def test_unauthenticated_device_cannot_receive_relevant_profile_in_prompt(tmp_path):
    conv = conversation(tmp_path)
    conv.device = replace(conv.device, authenticated=False)
    conv.store.save_memory("Учится в TalTech")
    asyncio.run(ask(conv, "Напиши про TalTech"))
    assert "Учится в TalTech" not in json.dumps(conv.llm.payloads, ensure_ascii=False)


def test_zero_initiative_suppresses_opted_in_habit_suggestions(tmp_path):
    store = MemoryStore(tmp_path / "t.db")
    memory = ProfileMemory(store)
    store.set_preference("memory", "habits_opt_in", True)
    store.save_memory("Инициативность: 0", category="personality", key="character:initiative")
    assert all(
        memory.observe("open_app", {"name": "IntelliJ"}, {"ok": True}) is None for _ in range(5)
    )


def test_concurrent_schema_migration_is_idempotent(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "concurrent.db"
    first = sqlite3.connect(path)
    first.executescript(
        "CREATE TABLE facts(id INTEGER PRIMARY KEY, content TEXT, created_at TEXT);"
        "INSERT INTO facts VALUES(1,'Старый факт','2020-01-01');"
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INTEGER, role TEXT, "
        "content TEXT, created_at TEXT);"
    )
    first.close()

    def reopen(_):
        store = MemoryStore(path)
        result = store.facts()
        store.close()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(reopen, [1, 2])) == [[(1, "Старый факт")], [(1, "Старый факт")]]


def test_outdated_revision_cannot_delete_or_correct_changed_fact(tmp_path):
    store = MemoryStore(tmp_path / "t.db")
    memory = ProfileMemory(store)
    idx, _ = store.save_memory("Первый вариант", key="project:test")
    store.save_memory("Явное исправление", key="project:test", expected_revision=1)
    assert store.forget_fact(idx, expected_revision=1) is False
    with pytest.raises(ToolError):
        asyncio.run(memory.execute("correct", id=idx, value="Нельзя вернуть старое", revision=1))
    assert store.facts() == [(idx, "Явное исправление")]


def test_style_and_cancelled_mobile_plan_apply_globally_without_ambiguous_delete_target(tmp_path):
    store = MemoryStore(tmp_path / "t.db")
    memory = ProfileMemory(store)
    store.save_memory("Не любит официальные ответы", category="preference", key="response_style")
    store.save_memory(
        "Мобильное приложение отменено, используем Shortcuts",
        category="project",
        key="chushpan:mobile",
    )
    store.save_memory("Учится в TalTech", key="education")
    result = memory.relevant("Привет")
    assert {r["semantic_key"] for r in result} == {"response_style", "chushpan:mobile"}
    assert memory.route("Забудь это") == {"action": "clarify"}
    assert "Не нашёл" in asyncio.run(memory.execute("recall", value="Продолжим"))["message"]


@pytest.mark.parametrize(
    "text", ["Запомни пароль печенька", "Мой token abcdef", "API_KEY supersecret"]
)
def test_plain_secret_labels_are_not_persisted_in_history(tmp_path, text):
    store = MemoryStore(tmp_path / "t.db")
    cid = store.new_conversation()
    store.add(cid, "user", text)
    assert not any(
        word in store.transcript(cid)[0]["content"]
        for word in ["печенька", "abcdef", "supersecret"]
    )


def test_sensitive_remember_metadata_and_value_commit_together(tmp_path):
    from backend.tools.memory import LongTermMemory

    store = MemoryStore(tmp_path / "t.db")
    store.db.executescript(
        "CREATE TRIGGER fail_source BEFORE UPDATE ON facts "
        "WHEN NEW.source LIKE '%explicit_user%' BEGIN SELECT RAISE(ABORT,'failure'); END;"
    )
    with pytest.raises(sqlite3.Error):
        asyncio.run(LongTermMemory(store).remember("Есть медицинский диагноз"))
    assert store.memories() == []


def test_legacy_unflagged_health_data_is_not_selected_for_cloud(tmp_path):
    store = MemoryStore(tmp_path / "t.db")
    store.add_fact("Учится в TalTech, есть депрессия")
    assert ProfileMemory(store).relevant("Напомни про учёбу") == []
