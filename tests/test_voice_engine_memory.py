"""Durable episodic memory with provenance, privacy and no transcript backfill."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from backend.context import DeviceContext, RequestContext, request_scope
from backend.memory.profile import ProfileMemory, extract
from backend.memory.store import MemoryStore

EVENT = "Сегодня я завершила проект Чушпан"


def capture(store, text=EVENT):
    with request_scope(RequestContext(DeviceContext.local_mac(), text)):
        ProfileMemory(store).capture(text)


def test_episode_survives_restart_with_expiry_source_and_self_report_confidence(tmp_path):
    path = tmp_path / "memory.sqlite"
    store = MemoryStore(path)
    store.set_preference("settings", "memory", {"temporary_days": 3})
    capture(store)
    capture(store)  # repeated statement is one fact
    store.close()
    store = MemoryStore(path)
    try:
        (row,) = store.memories()
        assert row["category"] == "episode" and row["confidence"] == 0.9
        assert json.loads(row["source"])["kind"] == "current_user_statement"
        assert datetime.fromisoformat(row["expires_at"]) > datetime.now(UTC)
        assert ProfileMemory(store).relevant("завершила проект")
        store.db.execute(
            "UPDATE facts SET expires_at=?",
            ((datetime.now(UTC) - timedelta(days=1)).isoformat(),),
        )
        assert not ProfileMemory(store).relevant("завершила проект")
        assert len(store.memories()) == 1  # expiry never deletes data
    finally:
        store.close()


def test_deleted_episode_does_not_reappear_from_repeated_extraction(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    try:
        capture(store)
        (identifier,) = [r["id"] for r in store.memories()]
        store.forget_fact(identifier)
        capture(store)
        assert store.facts() == []
        assert ProfileMemory(store).relevant("проект Чушпан") == []
    finally:
        store.close()


@pytest.mark.parametrize(
    "text",
    [
        "Наверное, сегодня я завершила проект Чушпан",
        "Сегодня я завершила проект Чушпан?",
        "Он сказал: сегодня я завершила проект Чушпан",
        "Сегодня я завершила проект с паролем example",
        "Сегодня я посетила медицинский центр",
        "Сегодня я не завершила проект Чушпан",
    ],
)
def test_uncertain_sensitive_secret_or_quoted_events_are_not_facts(text):
    assert extract(text) == []


@pytest.mark.parametrize("policy", [{"automatic": "off"}, {"excluded": ["episode"]}])
def test_privacy_policy_prevents_automatic_episode_storage(policy):
    store = MemoryStore(":memory:")
    try:
        store.set_preference("settings", "memory", policy)
        capture(store)
        assert store.memories() == []
    finally:
        store.close()


def test_ask_policy_proposes_episode_and_weak_facts_do_not_enter_context():
    store = MemoryStore(":memory:")
    try:
        store.set_preference("settings", "memory", {"automatic": "ask"})
        capture(store)
        assert not store.memories()
        assert store.preference("memory", "pending")[0]["category"] == "episode"
        store.save_memory("Проект Чушпан наблюдался косвенно", confidence=0.3)
        assert not ProfileMemory(store).relevant("Проект Чушпан")
    finally:
        store.close()
