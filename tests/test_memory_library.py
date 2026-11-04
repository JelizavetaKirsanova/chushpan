import asyncio
import json

import pytest

from backend.config import Settings
from backend.context import DeviceContext, RequestContext, request_scope
from backend.hud.bus import HudBus
from backend.memory.library import MemoryLibrary, search
from backend.memory.profile import ProfileMemory
from backend.memory.store import MemoryStore


@pytest.fixture
def library(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    service = MemoryLibrary(store, Settings(), HudBus())
    yield service
    store.close()


def run(service, **value):
    return asyncio.run(service.handle(value))


def test_projects_persist_dedupe_independently_and_never_leak_into_global_context(library):
    run(library, op="project_add", name="Учёба", description="Алгоритмы")
    project = library.snapshot()["projects"][0]["id"]
    for scope in (None, project, project):
        run(library, op="add", content="Люблю программирование на Python", project=scope)
    assert library.snapshot()["total"] == 2
    assert len(search(library.store, "Python", project)) == 1
    assert len(search(library.store, "Python", context=True)) == 1
    bound = DeviceContext.local_mac("project-session")
    cid = library.store.session_conversation(bound.session_id)
    run(library, op="project_bind", project=project, conversation=cid)
    with request_scope(RequestContext(bound, "Python")):
        selected = ProfileMemory(library.store).relevant("Python")
    assert len(selected) == 1 and selected[0]["project_id"] == project
    assert library.snapshot()["used_today"] == 1


def test_fts_updates_and_delete_purge_index_sources_access_and_context(library):
    run(library, op="add", content="Люблю тёплый свет", category="preference")
    item = library.snapshot()["items"][0]
    assert search(library.store, "тёпл")
    run(
        library,
        op="update",
        id=item["id"],
        revision=item["revision"],
        content="Люблю прохладный свет",
        category="preference",
    )
    assert search(library.store, "тёпл") == []
    assert search(library.store, "прохлад")
    with pytest.raises(ValueError, match="изменилась"):
        run(library, op="delete", id=item["id"], revision=item["revision"], confirmed=True)
    item = library.snapshot()["items"][0]
    run(library, op="delete", id=item["id"], revision=item["revision"], confirmed=True)
    assert search(library.store, "прохлад") == []
    assert library.store.db.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0] == 0
    assert library.store.preference("memory", "context_floor") == 0


def test_sensitive_expiry_and_import_are_fail_closed_and_atomic(library):
    with pytest.raises(ValueError, match="согласие"):
        run(library, op="add", content="Мой диагноз: пример")
    run(library, op="add", content="Мой диагноз: пример", consent_sensitive=True)
    assert ProfileMemory(library.store).relevant("диагноз") == []
    run(library, op="add", content="Временная заметка", expires_at="2000-01-01T00:00:00+00:00")
    assert search(library.store, "Временная") == []
    before = library.store.memories()
    invalid = {
        "schema": 1,
        "items": [{"content": "Новая запись"}, {"content": "Мой диагноз: другой"}],
    }
    with pytest.raises(ValueError, match="Чувствительные"):
        run(library, op="import", content=json.dumps(invalid))
    assert library.store.memories() == before


def test_import_export_preserves_project_isolation(library, tmp_path):
    run(library, op="project_add", name="Изолированный")
    project = library.snapshot()["projects"][0]["id"]
    run(library, op="add", content="План изучения графов", project=project)
    exported = run(library, op="export")["download"]["content"]
    target = MemoryStore(tmp_path / "import.sqlite")
    try:
        other = MemoryLibrary(target, Settings(), HudBus())
        run(other, op="import", content=exported)
        assert len(target.memories()) == 1
        assert target.facts() == []
        assert search(target, "граф", context=True) == []
    finally:
        target.close()


def test_automatic_conflict_is_proposed_and_explicit_correction_remains_compatible(library):
    memory = ProfileMemory(library.store)
    device = DeviceContext.local_mac("test")
    for content in ("Я учусь в TalTech", "Я учусь в другом университете"):
        with request_scope(RequestContext(device, content)):
            memory.capture(content)
    assert len(library.store.facts()) == 1
    assert "taltech" in library.store.facts()[0][1]
    assert len(library.store.preference("memory", "pending")) == 1


def test_reopen_legacy_and_fts_remain_consistent(library):
    run(library, op="add", content="Проект сохранён")
    path = library.store.db.execute("PRAGMA database_list").fetchone()[2]
    other = MemoryStore(path)
    try:
        assert search(other, "сохран")
        assert other.memories()[0]["source"] == "desktop_library"
    finally:
        other.close()
