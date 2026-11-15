"""Build cleanup only removes intermediates owned by the current build."""

from scripts import build_macos


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
