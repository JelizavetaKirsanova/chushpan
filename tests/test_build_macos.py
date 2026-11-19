"""Build cleanup only removes intermediates owned by the current build."""

import os

import pytest

from scripts import build_macos, macos_release


def test_build_cleanup_preserves_older_release_packages(tmp_path, monkeypatch):
    from backend import storage_manager

    builds = tmp_path / "Builds.noindex"
    current = builds / "release-current"
    older = builds / "release-older"
    (current / "native").mkdir(parents=True)
    (current / "pyinstaller").mkdir()
    previous_app = older / "dist" / "Чушпан.app"
    previous_app.mkdir(parents=True)
    (older / "build-complete.json").write_text("{}")
    checkout = tmp_path / "checkout"
    swift_cache = checkout / "build/macos/swift-cache"
    swift_cache.mkdir(parents=True)

    monkeypatch.setattr(build_macos, "ROOT", checkout)
    monkeypatch.setattr(storage_manager, "open_paths", lambda: set())

    build_macos.clean_build_temporaries(current)

    assert previous_app.is_dir()
    assert (older / "build-complete.json").is_file()
    assert not (current / "native").exists()
    assert not (current / "pyinstaller").exists()
    assert not swift_cache.exists()


def test_build_keeps_successful_output_until_install(tmp_path, monkeypatch):
    base = tmp_path / "Caches/Chushpan/Builds.noindex"
    monkeypatch.setattr(macos_release, "build_cache", lambda: base)
    monkeypatch.setattr(build_macos, "new_build_directory", lambda: base / "release-success")

    def successful_build():
        build_macos._ACTIVE_BUILD_ROOT = base / "release-success"
        build_macos._ACTIVE_BUILD_ROOT.mkdir(parents=True)
        return build_macos._ACTIVE_BUILD_ROOT / "dist/Чушпан.app"

    monkeypatch.setattr(build_macos, "_build", successful_build)
    monkeypatch.setattr(build_macos, "update_state", lambda: tmp_path / "updates")
    result = build_macos.build()
    assert result == base / "release-success/dist/Чушпан.app"
    assert (base / "release-success").is_dir()


@pytest.mark.parametrize("error", [RuntimeError("compile failed"), KeyboardInterrupt()])
def test_failed_or_interrupted_build_removes_its_staging_directory(tmp_path, monkeypatch, error):
    base = tmp_path / "Caches/Chushpan/Builds.noindex"
    monkeypatch.setattr(macos_release, "build_cache", lambda: base)
    root = base / "release-aborted"

    def failed_build():
        build_macos._ACTIVE_BUILD_ROOT = root
        root.mkdir(parents=True)
        (root / "large-temporary.bin").write_bytes(b"temporary")
        raise error

    monkeypatch.setattr(build_macos, "_build", failed_build)
    monkeypatch.setattr(build_macos, "update_state", lambda: tmp_path / "updates")
    monkeypatch.setattr(build_macos, "ROOT", tmp_path)
    with pytest.raises(type(error)):
        build_macos.build()
    assert not root.exists()


def test_startup_removes_abandoned_builds_but_defers_active_or_uninspectable(tmp_path, monkeypatch):
    from backend import storage_manager

    base = storage_manager.roots(tmp_path)["builds"]
    old = base / "release-old"
    active = base / "release-active"
    old.mkdir(parents=True)
    os.utime(old, (0, 0))
    active.mkdir()
    recent = base / "release-just-built"
    recent.mkdir()
    monkeypatch.setattr(storage_manager, "open_paths", lambda: {active / "native/tool"})
    result = storage_manager.cleanup_stale_builds(tmp_path)
    assert not old.exists()
    assert active.exists()
    assert recent.exists()
    assert result["removed"] == [str(old)]
    monkeypatch.setattr(storage_manager, "open_paths", lambda: None)
    active_new = base / "release-uninspectable"
    active_new.mkdir()
    deferred = storage_manager.cleanup_stale_builds(tmp_path)
    assert active_new.exists()
    assert deferred["deferred"] == ["process scan unavailable"]
