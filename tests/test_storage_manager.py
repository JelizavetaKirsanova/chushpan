"""Storage inventory and cleanup must never reach user data or active transactions."""

import fcntl
import hashlib
import json
import plistlib
import sqlite3
from pathlib import Path

import pytest

from backend import storage_manager as storage


def make_backup(path: Path, body: bytes = b"preferences") -> Path:
    path.mkdir(parents=True)
    data = path / "data/jarvis.db"
    data.parent.mkdir()
    with sqlite3.connect(data) as db:
        db.execute("CREATE TABLE memory (value TEXT)")
        db.execute("INSERT INTO memory VALUES (?)", (body.decode(),))
    digest = hashlib.sha256(data.read_bytes()).hexdigest()
    files = {"data/jarvis.db": digest}
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "sha256": files,
                "digest": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
            }
        )
    )
    return path


def bundle(path: Path, build: str) -> Path:
    (path / "Contents/MacOS").mkdir(parents=True)
    (path / "Contents/MacOS/Chushpan").write_bytes(b"app " + build.encode())
    (path / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "ee.chushpan.desktop",
                "CFBundleExecutable": "Chushpan",
                "CFBundleVersion": build,
            }
        )
    )
    return path


def test_verified_backup_restores_into_empty_folder_and_preserves_sqlite(tmp_path):
    backup = make_backup(tmp_path / "Backups.noindex/snapshot-1")
    target = storage.restore_backup(backup, tmp_path / "restore")
    with sqlite3.connect(target / "data/jarvis.db") as db:
        assert db.execute("SELECT value FROM memory").fetchone()[0] == "preferences"
    assert storage.verified_backup(backup)


def test_restore_never_overwrites_existing_user_data(tmp_path):
    backup = make_backup(tmp_path / "snapshot-1")
    target = tmp_path / "restore"
    target.mkdir()
    (target / "important.txt").write_text("keep")
    with pytest.raises(ValueError, match="пустая папка"):
        storage.restore_backup(backup, target)
    assert (target / "important.txt").read_text() == "keep"


def test_cleanup_preview_and_apply_preserve_user_data_and_latest_rollback(tmp_path, monkeypatch):
    home = tmp_path / "home"
    roots = storage.roots(home)
    old = roots["updates"] / "20260101T000000Z-old"
    current = roots["updates"] / "20260102T000000Z-current"
    old_previous = bundle(old / "Чушпан.app", "10")
    current_previous = bundle(current / "Чушпан.app", "11")
    installed = bundle(tmp_path / "Applications/Чушпан.app", "12")
    (old / "receipt.json").write_text(
        json.dumps({"status": "installed", "build": "11", "previous": str(old_previous)})
    )
    (current / "receipt.json").write_text(
        json.dumps({"status": "installed", "build": "12", "previous": str(current_previous)})
    )
    data = home / "Library/Application Support/Chushpan/data/jarvis.db"
    data.parent.mkdir(parents=True)
    data.write_bytes(b"must remain")
    monkeypatch.setattr(storage, "APP", installed)
    preview = storage.preview_cleanup(home)
    assert [Path(x["path"]) for x in preview["candidates"]] == [old]
    with pytest.raises(ValueError, match="Состав файлов изменился"):
        storage.apply_cleanup(home, installed, "wrong-preview")
    result = storage.apply_cleanup(home, installed, preview["token"])
    assert not old.exists()
    assert current_previous.exists()
    assert installed.exists()
    assert data.read_bytes() == b"must remain"
    assert result["removed"][0]["path"] == str(old)


def test_active_update_blocks_cleanup(tmp_path):
    home = tmp_path / "home"
    updates = storage.roots(home)["updates"]
    updates.mkdir(parents=True)
    lock = (updates / "update.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ValueError, match="выполняется обновление"):
            storage.apply_cleanup(home, tmp_path / "absent.app", "token")
    finally:
        lock.close()


def test_open_update_package_is_excluded_from_preview(tmp_path, monkeypatch):
    home = tmp_path / "home"
    state = storage.roots(home)["updates"]
    old = state / "20260101T000000Z-old"
    current = state / "20260102T000000Z-current"
    old_previous = bundle(old / "Чушпан.app", "10")
    current_previous = bundle(current / "Чушпан.app", "11")
    installed = bundle(tmp_path / "Applications/Чушпан.app", "12")
    (old / "receipt.json").write_text(
        json.dumps({"status": "installed", "previous": str(old_previous)})
    )
    (current / "receipt.json").write_text(
        json.dumps({"status": "installed", "previous": str(current_previous)})
    )
    monkeypatch.setattr(storage, "APP", installed)
    monkeypatch.setattr(storage, "open_paths", lambda: {old_previous / "Contents/Resources"})
    preview = storage.preview_cleanup(home)
    assert preview["candidates"] == []
    assert preview["protected_active_paths"] == [str(old)]


def test_active_build_lock_excludes_build_directory_from_cleanup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    build_root = storage.roots(home)["builds"]
    active = build_root / "release-active"
    active.mkdir(parents=True)
    (active / "work.bin").write_bytes(b"in use")
    lock_path = build_root.parent / "build.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(storage, "open_paths", lambda: set())
    try:
        preview = storage.preview_cleanup(home)
        assert not any(row["path"] == str(active) for row in preview["candidates"])
    finally:
        lock.close()


def test_cleanup_does_not_touch_memory_models_or_backup_copies(tmp_path, monkeypatch):
    home = tmp_path / "home"
    roots = storage.roots(home)
    roots["updates"].mkdir(parents=True)
    memory = home / "Library/Application Support/Chushpan/data/jarvis.db"
    memory.parent.mkdir(parents=True)
    memory.write_bytes(b"sqlite data")
    model = home / ".ollama/models/manifest.json"
    model.parent.mkdir(parents=True)
    model.write_text("model")
    backup = make_backup(roots["backups"] / "snapshot-1")
    monkeypatch.setattr(storage, "APP", tmp_path / "Applications/Чушпан.app")
    preview = storage.preview_cleanup(home)
    storage.apply_cleanup(home, tmp_path / "Applications/Чушпан.app", preview["token"])
    assert memory.read_bytes() == b"sqlite data"
    assert model.read_text() == "model"
    assert backup.exists()


def test_backup_cleanup_keeps_three_verified_and_every_pinned_copy(tmp_path, monkeypatch):
    home = tmp_path / "home"
    base = storage.roots(home)["backups"]
    snapshots = [make_backup(base / f"snapshot-2026110{n}T000000Z") for n in range(5)]
    monkeypatch.setattr(storage, "open_paths", lambda: set())
    storage.set_backup_pinned(str(snapshots[0]), True, home)
    preview = storage.preview_cleanup(home, tmp_path / "absent.app")
    assert [Path(x["path"]) for x in preview["backup_candidates"]] == [snapshots[1]]
    result = storage.apply_cleanup(home, tmp_path / "absent.app", preview["token"])
    assert result["removed"][0]["path"] == str(snapshots[1])
    assert snapshots[0].exists()
    assert all(snapshot.exists() for snapshot in snapshots[2:])


def test_active_backup_writer_disables_backup_cleanup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    base = storage.roots(home)["backups"]
    snapshots = [make_backup(base / f"snapshot-2026110{n}T000000Z") for n in range(4)]
    monkeypatch.setattr(storage, "open_paths", lambda: set())
    lock = (base / ".backup.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        preview = storage.preview_cleanup(home)
        assert not preview["backup_scan_available"]
        assert not preview["backup_candidates"]
        with pytest.raises(ValueError, match="выполняется резервное копирование"):
            storage.apply_cleanup(home, tmp_path / "absent.app", preview["token"])
    finally:
        lock.close()
    assert all(snapshot.exists() for snapshot in snapshots)
