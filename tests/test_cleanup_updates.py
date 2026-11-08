"""Update cleanup is isolated, preview-bound and safe around the app worker."""

import hashlib
import plistlib
from pathlib import Path

import pytest

from scripts import cleanup_updates as cleaner
from scripts import macos_release as release


def _bundle(path: Path, build: str, payload: bytes) -> Path:
    executable = path / "Contents/MacOS/Chushpan"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(payload)
    (path / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": release.BUNDLE_ID,
                "CFBundleExecutable": "Chushpan",
                "CFBundleShortVersionString": "0.3.17",
                "CFBundleVersion": build,
            }
        )
    )
    return path


@pytest.fixture
def update_tree(tmp_path, monkeypatch):
    home = tmp_path / "home"
    support = home / "Library/Application Support/Chushpan"
    state = support / "Updates.noindex"
    state.mkdir(parents=True)
    app = _bundle(tmp_path / "Applications/Чушпан.app", "42", b"installed-42")
    latest = state / "20261108T195749Z-latest"
    rollback = _bundle(latest / "Чушпан.app", "41", b"rollback-41")
    release.write_receipt(
        latest / "receipt.json",
        {"status": "installed", "build": "42", "previous": str(rollback)},
    )
    old = state / "20261107T154325Z-old"
    old_bundle = _bundle(old / "Чушпан.app", "40", b"old-40")
    release.write_receipt(
        old / "receipt.json",
        {"status": "installed", "build": "41", "previous": str(old_bundle)},
    )

    # User data, backups, settings, models and sources must remain outside cleanup.
    protected = {
        support / "data/memory.sqlite": b"sqlite-user-data",
        support / "config.local.yaml": b"user-settings",
        support / "private/scenarios.json": b"user-scenarios",
        support / "models/ollama/model.gguf": b"ollama-model",
        support / "Backups.noindex/snapshot-user/manifest.json": b"backup-manifest",
        tmp_path / "repo/backend/voice.py": b"source",
    }
    for path, payload in protected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    from backend import storage_manager

    monkeypatch.setattr(storage_manager, "open_paths", lambda: set())

    def verify(path):
        info = release.read_info(path)
        executable = (path / "Contents/MacOS/Chushpan").read_bytes()
        return {
            "app": str(path),
            "bundle_id": release.BUNDLE_ID,
            "build": info["CFBundleVersion"],
            "executable_sha256": hashlib.sha256(executable).hexdigest(),
        }

    monkeypatch.setattr(cleaner, "verify_bundle", verify)
    monkeypatch.setattr(cleaner, "_process_table", lambda: [])
    return {
        "home": home,
        "support": support,
        "state": state,
        "app": app,
        "rollback": rollback,
        "old": old,
        "old_bundle": old_bundle,
        "protected": protected,
    }


def test_preview_bound_cleanup_keeps_installed_rollback_and_user_data(update_tree, monkeypatch):
    tree = update_tree
    closed = []
    opened = []
    monkeypatch.setattr(cleaner, "open_paths", lambda: set())
    preview = cleaner.preview_updates(home=tree["home"], app=tree["app"])
    assert preview["count"] == 1
    result = cleaner.cleanup_updates(
        preview["token"],
        home=tree["home"],
        app=tree["app"],
        shutdown=lambda app: closed.append(app),
        relaunch=lambda app: opened.append(app),
    )
    assert result["removed"] == [tree["old"].name]
    assert result["installed_build"] == "42"
    assert result["rollback_build"] == "41"
    assert result["app_launch_verified"] is True
    assert closed == opened == [tree["app"]]
    assert not tree["old"].exists()
    assert tree["app"].exists() and tree["rollback"].exists()
    assert all(path.read_bytes() == payload for path, payload in tree["protected"].items())


def test_cleanup_stops_when_candidate_has_open_files(update_tree, monkeypatch):
    tree = update_tree
    called = []
    preview = cleaner.preview_updates(home=tree["home"], app=tree["app"])
    monkeypatch.setattr(
        cleaner,
        "open_paths",
        lambda: {tree["old_bundle"] / "Contents/Info.plist"},
    )
    with pytest.raises(RuntimeError, match="открыт"):
        cleaner.cleanup_updates(
            preview["token"],
            home=tree["home"],
            app=tree["app"],
            shutdown=lambda app: None,
            relaunch=lambda app: None,
        )
    assert tree["old"].exists()
    assert called == []


def test_cleanup_rejects_changed_preview_before_closing_app(update_tree):
    tree = update_tree
    called = []
    with pytest.raises(RuntimeError, match="после предпросмотра"):
        cleaner.cleanup_updates(
            "wrong-token",
            home=tree["home"],
            app=tree["app"],
            shutdown=lambda app: called.append(app),
            relaunch=lambda app: None,
        )
    assert called == []
    assert tree["old"].exists()


def test_cleanup_rejects_busy_update_lock(update_tree):
    tree = update_tree
    preview = cleaner.preview_updates(home=tree["home"], app=tree["app"])
    lock_path = tree["state"] / "update.lock"
    with lock_path.open("a") as handle:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="уже выполняется"):
            cleaner.cleanup_updates(preview["token"], home=tree["home"], app=tree["app"])
    assert tree["old"].exists()


def test_cleanup_stops_for_active_updater_process(update_tree, monkeypatch):
    tree = update_tree
    preview = cleaner.preview_updates(home=tree["home"], app=tree["app"])
    monkeypatch.setattr(
        cleaner,
        "_process_table",
        lambda: [(99999, 1, "python scripts/update_macos.py")],
    )
    monkeypatch.setattr(cleaner, "open_paths", lambda: set())
    with pytest.raises(RuntimeError, match="сборка или установка"):
        cleaner.cleanup_updates(
            preview["token"],
            home=tree["home"],
            app=tree["app"],
            shutdown=lambda app: None,
            relaunch=lambda app: None,
        )
    assert tree["old"].exists()


def test_shutdown_signals_only_chushpan_owned_worker(tmp_path, monkeypatch):
    app_path = tmp_path / "Applications/Чушпан.app"
    binary = str(app_path / "Contents/MacOS/Chushpan")

    class RunningApp:
        def processIdentifier(self):
            return 10

        def terminate(self):
            return True

    app = RunningApp()
    monkeypatch.setattr(cleaner, "_running_application", lambda path: [app])
    unrelated = (90, 1, "/Applications/Ollama.app/Contents/MacOS/Ollama")
    main = (10, 1, f"{binary} --no-auto-listen")
    worker = (11, 10, f"{binary} --desktop-worker --parent 10")
    tables = iter(
        [[main, worker, unrelated], [worker, unrelated], [worker, unrelated], [unrelated]]
    )
    monkeypatch.setattr(cleaner, "_process_table", lambda: next(tables))
    signals = []
    monkeypatch.setattr(cleaner.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(cleaner.time, "sleep", lambda seconds: None)

    cleaner.stop_chushpan(app_path)

    assert signals == [(11, cleaner.signal.SIGTERM)]
