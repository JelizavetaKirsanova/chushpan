import json
import sqlite3
import sys
from pathlib import Path

import pytest
from chushpan_core.memory import MemoryConflict, MemoryStore


def test_memory_crud_search_private_mode_history_and_forget(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    created = store.create("Chushpan runs on Huawei", category="project", now=10)
    identity = created["entry"]["id"]

    duplicate = store.create("  chushpan   runs ON huawei ", category="project", now=11)
    assert duplicate["created"] is False
    assert duplicate["entry"]["id"] == identity
    assert store.create("private note", private_mode=True) == {
        "stored": False,
        "private_mode": True,
    }
    assert store.search("huawei chushpan")[0]["id"] == identity

    updated = store.update(identity, "Core API is isolated", revision=1, now=12)
    assert updated["revision"] == 2
    assert [row["action"] for row in store.history(identity)] == ["create", "update"]
    with pytest.raises(MemoryConflict, match="stale"):
        store.update(identity, "stale update", revision=1)

    assert store.forget(identity)
    assert store.history(identity) is None
    assert store.search("huawei") == []
    store.close()


def test_memory_backup_restore_is_validated_and_never_overwrites(tmp_path):
    source = tmp_path / "memory.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    restored = tmp_path / "restored.sqlite3"
    store = MemoryStore(source)
    store.create("Keep this test fact", category="note")

    store.backup_to(backup)
    assert MemoryStore.verify_backup(backup) == {"valid": True, "schema_version": 2}
    with pytest.raises(FileExistsError):
        store.backup_to(backup)
    copy = MemoryStore.restore_to_empty(backup, restored)
    assert copy.search("test fact")[0]["content"] == "Keep this test fact"
    copy.close()

    with pytest.raises(FileExistsError):
        MemoryStore.restore_to_empty(backup, restored)

    corrupt = tmp_path / "not-a-core-database.sqlite3"
    connection = sqlite3.connect(corrupt)
    connection.execute("CREATE TABLE other_data(value TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="validation"):
        MemoryStore.verify_backup(corrupt)
    store.close()


def test_explicit_memory_import_is_atomic_idempotent_and_reversible(tmp_path):
    from uuid import uuid4

    store = MemoryStore(tmp_path / "memory.sqlite3")
    migration_id = str(uuid4())
    entries = [
        {"content": "Keep my choices", "category": "preference", "source": "explicit_user"},
        {"content": "Keep my choices", "category": "preference", "source": "explicit_user"},
        {"content": "Chushpan Core server", "category": "project", "source": "ui_confirmed"},
    ]
    result = store.import_batch(migration_id, entries, now=20)
    assert len(result["created"]) == 2
    assert result["duplicates"] == 1
    assert store.count() == 2
    assert store.import_batch(migration_id, entries, now=21)["already_imported"] is True
    assert store.count() == 2

    changed_id = result["created"][0]["id"]
    store.update(changed_id, "Keep my updated choices", revision=1, now=22)
    rolled_back = store.rollback_import(migration_id, now=23)
    assert rolled_back["deleted"] == 1
    assert rolled_back["preserved_modified"] == 1
    assert store.count() == 1
    assert store.get(changed_id)["content"] == "Keep my updated choices"
    assert store.rollback_import(migration_id)["already_rolled_back"] is True
    store.close()


def test_admin_backup_commands_stay_inside_programdata(tmp_path, monkeypatch, capsys):
    from chushpan_core import admin

    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["chushpan-core-admin", "backup-memory"])
    assert admin.main() == 0
    backup = json.loads(capsys.readouterr().out)["backup"]
    assert (tmp_path / "Chushpan" / "CoreServer" / "backups") in Path(backup).parents

    monkeypatch.setattr(
        sys,
        "argv",
        ["chushpan-core-admin", "verify-memory-backup", "--name", Path(backup).name],
    )
    assert admin.main() == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
