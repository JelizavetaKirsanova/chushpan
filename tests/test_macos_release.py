"""Real temporary bundles/renames; native tools are mocked, never user installations."""

import fcntl
import json
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import macos_release as release


def bundle(path, build="19", payload=b"new", bundle_id=release.BUNDLE_ID):
    (path / "Contents/MacOS").mkdir(parents=True)
    (path / "Contents/MacOS/Chushpan").write_bytes(payload)
    (path / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": bundle_id,
                "CFBundleExecutable": "Chushpan",
                "CFBundleShortVersionString": "0.3.6",
                "CFBundleVersion": build,
            }
        )
    )
    return path


@pytest.fixture
def installation(tmp_path, monkeypatch):
    from backend import storage_manager

    monkeypatch.setattr(storage_manager, "open_paths", lambda: set())
    applications = tmp_path / "Applications"
    applications.mkdir()
    main = bundle(applications / release.APP_NAME, "18", b"working-old")
    source = bundle(tmp_path / "Builds.noindex/release/dist" / release.APP_NAME)
    state = tmp_path / "Updates.noindex"
    monkeypatch.setattr(release, "INSTALLED_APP", main)
    monkeypatch.setattr(release, "update_state", lambda: state)
    monkeypatch.setattr(release, "build_cache", lambda: tmp_path / "Builds.noindex")
    monkeypatch.setattr(release, "assert_not_running", lambda: None)
    monkeypatch.setattr(release, "refresh_dock", lambda: None)
    checks = []
    registrations = []
    removed = []
    copies = []

    def run(command, **kwargs):
        if command[0] == "/usr/bin/ditto":
            shutil.copytree(command[1], command[2], symlinks=True)
            copies.append(Path(command[2]))
        elif command[0] == str(release.LSREGISTER):
            (registrations if command[1] == "-f" else removed).append(Path(command[2]))
        elif command[0] not in ["/usr/bin/lipo", "/usr/bin/codesign"]:
            raise AssertionError(command)
        return SimpleNamespace(stdout="arm64\n", returncode=0)

    def self_test(app, directory):
        checks.append(app)
        directory.mkdir(parents=True)

    monkeypatch.setattr(release.subprocess, "run", run)
    monkeypatch.setattr(release, "self_test", self_test)
    return SimpleNamespace(
        main=main,
        source=source,
        state=state,
        checks=checks,
        registrations=registrations,
        removed=removed,
        copies=copies,
    )


def test_update_preserves_previous_and_registers_only_canonical_path(installation):
    i = installation
    result = release.install(i.source)
    assert (i.main / "Contents/MacOS/Chushpan").read_bytes() == b"new"
    assert (Path(result["previous"]) / "Contents/MacOS/Chushpan").read_bytes() == b"working-old"
    assert i.source.exists()  # cleanup is opt-in
    assert i.registrations == i.removed == [i.main]
    assert i.checks == [i.main]  # executing a test bundle can implicitly register it with macOS
    assert any(part.endswith(".noindex") for part in i.copies[0].parts)
    assert json.loads((i.state / "last-install.json").read_text())["status"] == "installed"
    assert len(list(i.main.parent.glob("*.app"))) == 1


def test_successful_update_prunes_older_verified_rollback_packages(installation):
    i = installation
    older = i.state / "20260101T000000Z-old"
    previous = bundle(older / release.APP_NAME, "17", b"older")
    release.write_receipt(
        older / "receipt.json",
        {"status": "installed", "build": "18", "previous": str(previous)},
    )
    result = release.install(i.source)
    assert not older.exists()
    assert Path(result["previous"]).exists()


@pytest.mark.parametrize("failure", ["installed-check", "registration"])
def test_failed_update_restores_old_app_and_preserves_failed_new_bundle(
    installation, monkeypatch, failure
):
    i = installation
    if failure == "installed-check":

        def check(app, directory):
            if app == i.main:
                raise RuntimeError("new build cannot start")
            directory.mkdir(parents=True)

        monkeypatch.setattr(release, "self_test", check)
    else:
        original = release.register_installed
        calls = []

        def register(app):
            calls.append(app)
            if len(calls) == 1:
                raise RuntimeError("registration failed")
            original(app)

        monkeypatch.setattr(release, "register_installed", register)
    with pytest.raises(RuntimeError):
        release.install(i.source)
    assert (i.main / "Contents/MacOS/Chushpan").read_bytes() == b"working-old"
    failed = list(i.main.parent.glob(".chushpan-*.noindex/failed.app"))
    assert len(failed) == 1 and (failed[0] / "Contents/MacOS/Chushpan").read_bytes() == b"new"
    assert i.source.exists()
    assert not (i.state / "last-install.json").exists()


def test_preflight_failure_keeps_main_registered_and_untouched(installation, monkeypatch):
    i = installation
    original = release.verify_bundle

    def verify(app):
        if app != i.source:
            raise RuntimeError("bad staging signature")
        return original(app)

    monkeypatch.setattr(release, "verify_bundle", verify)
    with pytest.raises(RuntimeError):
        release.install(i.source)
    assert (i.main / "Contents/MacOS/Chushpan").read_bytes() == b"working-old"
    assert i.registrations == i.removed == []


def test_downgrade_and_running_app_are_not_replaced(installation, monkeypatch):
    i = installation
    info = plistlib.loads((i.source / "Contents/Info.plist").read_bytes())
    info["CFBundleVersion"] = "17"
    (i.source / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    with pytest.raises(ValueError, match="старой"):
        release.install(i.source)
    info["CFBundleVersion"] = "19"
    (i.source / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    monkeypatch.setattr(
        release, "assert_not_running", lambda: (_ for _ in ()).throw(ValueError("running"))
    )
    with pytest.raises(ValueError, match="running"):
        release.install(i.source)
    assert (i.main / "Contents/MacOS/Chushpan").read_bytes() == b"working-old"


@pytest.mark.parametrize("name", [".env", "data/jarvis.db", "private/pairing.json"])
def test_private_files_reject_the_bundle_before_native_tools(tmp_path, name):
    app = bundle(tmp_path / release.APP_NAME)
    path = app / "Contents/Resources" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("private test data")
    with pytest.raises(ValueError, match="Личные данные"):
        release.verify_bundle(app)


def test_foreign_bundle_id_and_external_symlink_are_rejected(tmp_path):
    app = bundle(tmp_path / "foreign.app", bundle_id="unrelated.app")
    with pytest.raises(ValueError, match="Bundle ID"):
        release.verify_bundle(app)
    app = bundle(tmp_path / release.APP_NAME)
    (app / "Contents/external").symlink_to(tmp_path / "user-data")
    with pytest.raises(ValueError, match="внешняя"):
        release.verify_bundle(app)


def test_development_bundle_cannot_be_registered(installation):
    with pytest.raises(ValueError, match="только"):
        release.register_installed(installation.source)


def test_cleanup_requires_successful_receipt_and_protects_installed_app(installation):
    i = installation
    with pytest.raises(ValueError, match="подтверждения"):
        release.trash_build(i.source, {"status": "rolled_back", "source": str(i.source)})
    with pytest.raises(ValueError, match="Builds.noindex"):
        release.trash_build(i.main, {"status": "installed", "source": str(i.main)})


def test_signature_failure_does_not_replace_main(installation, monkeypatch):
    i = installation

    def fail(command, **kwargs):
        if command[0] == "/usr/bin/codesign":
            raise subprocess.CalledProcessError(1, command)
        return SimpleNamespace(stdout="arm64")

    monkeypatch.setattr(release.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        release.install(i.source)
    assert (i.main / "Contents/MacOS/Chushpan").read_bytes() == b"working-old"


def test_explicit_rollback_preserves_both_versions(installation):
    i = installation
    first = release.install(i.source)
    restored = release.rollback_last()
    assert restored["build"] == "18"
    assert (i.main / "Contents/MacOS/Chushpan").read_bytes() == b"working-old"
    assert (Path(restored["previous"]) / "Contents/MacOS/Chushpan").read_bytes() == b"new"
    assert Path(first["previous"]).exists()


def test_failed_first_install_does_not_leave_bad_canonical_app(installation, monkeypatch):
    i = installation
    shutil.rmtree(i.main)

    def check(app, directory):
        if app == i.main:
            raise RuntimeError("bad first start")
        directory.mkdir(parents=True)

    monkeypatch.setattr(release, "self_test", check)
    with pytest.raises(RuntimeError):
        release.install(i.source)
    assert not i.main.exists()
    assert len(list(i.main.parent.glob(".chushpan-*.noindex/failed.app"))) == 1


def test_parallel_update_is_rejected_before_replacement(installation):
    i = installation
    i.state.mkdir()
    with (i.state / "update.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="Другое"):
            release.install(i.source)
    assert (i.main / "Contents/MacOS/Chushpan").read_bytes() == b"working-old"


def test_interrupted_update_blocks_new_install_and_recovers_old_bundle(installation):
    i = installation
    record = i.state / "interrupted"
    record.mkdir(parents=True)
    previous = record / release.APP_NAME
    i.main.rename(previous)
    shutil.copytree(i.source, i.main)
    release.write_receipt(
        record / "receipt.json", {"status": "replacing", "previous": str(previous)}
    )
    with pytest.raises(ValueError, match="Незавершённое"):
        release.install(i.source)
    result = release.recover_interrupted()
    assert result["status"] == "recovered"
    assert (i.main / "Contents/MacOS/Chushpan").read_bytes() == b"working-old"
    assert (record / "interrupted-new.app/Contents/MacOS/Chushpan").read_bytes() == b"new"


def test_dock_refresh_keeps_other_icons_and_replaces_stale_bookmark(installation):
    other = {"GUID": 1, "tile-data": {"bundle-identifier": "other.app", "book": b"keep"}}
    stale = {"GUID": 2, "tile-data": {"bundle-identifier": release.BUNDLE_ID, "book": b"old"}}
    tiles = release.canonical_dock_tiles([other, stale, {**stale, "GUID": 3}])
    assert len(tiles) == 2 and tiles[0] == other
    assert tiles[1]["GUID"] == 2 and "book" not in tiles[1]["tile-data"]
    assert tiles[1]["tile-data"]["file-data"]["_CFURLString"] == installation.main.as_uri() + "/"
    assert release.canonical_dock_tiles([other]) is None


def test_import_check_never_executes_a_temporary_bundle(tmp_path):
    with pytest.raises(ValueError, match="только"):
        release.self_test(tmp_path / release.APP_NAME, tmp_path / "test-data")
    assert not (tmp_path / "test-data").exists()


def test_explicit_cleanup_trashes_only_successful_build_and_removes_its_ls_records(
    installation, monkeypatch, tmp_path
):
    i = installation
    receipt = release.install(i.source)
    target = tmp_path / "Trash" / release.APP_NAME
    target.parent.mkdir()

    class Manager:
        def trashItemAtURL_resultingItemURL_error_(self, source, *args):
            Path(source).rename(target)
            return True, SimpleNamespace(path=lambda: str(target)), None

    monkeypatch.setitem(
        sys.modules,
        "Foundation",
        SimpleNamespace(
            NSURL=SimpleNamespace(fileURLWithPath_=lambda value: value),
            NSFileManager=SimpleNamespace(defaultManager=Manager),
        ),
    )
    assert release.trash_build(i.source, receipt) == str(target)
    assert target.exists() and not i.source.exists() and Path(receipt["previous"]).exists()
    assert i.removed[-2:] == [i.source, target]
    assert i.registrations[-1] == i.main
