"""One installed app, private build staging and recoverable updates. No audio or devices."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

APP_NAME = "Чушпан.app"
BUNDLE_ID = "ee.chushpan.desktop"
INSTALLED_APP = Path("/Applications") / APP_NAME
LSREGISTER = Path(
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)
ROOT = Path(__file__).resolve().parents[1]
BUILD_RECEIPT = ROOT / "build/macos/last-build.json"


def build_cache() -> Path:
    return Path.home() / "Library/Caches/Chushpan/Builds.noindex"


def update_state() -> Path:
    return Path.home() / "Library/Application Support/Chushpan/Updates.noindex"


def write_receipt(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as out:
        os.chmod(temporary, 0o600)
        json.dump(value, out, ensure_ascii=False, indent=2)
    temporary.replace(path)


def new_build_directory() -> Path:
    base = build_cache()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    (base / ".metadata_never_index").touch(mode=0o600, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="release-", dir=base))


def read_info(app: Path) -> dict:
    if app.is_symlink() or not app.is_dir():
        raise ValueError("Ожидается настоящий пакет приложения, не ссылка.")
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleIdentifier") != BUNDLE_ID:
        raise ValueError("Bundle ID приложения не совпадает с ee.chushpan.desktop.")
    if info.get("CFBundleExecutable") != "Chushpan":
        raise ValueError("Неожиданный исполняемый файл приложения.")
    if (
        not info.get("CFBundleShortVersionString")
        or not str(info.get("CFBundleVersion", "")).isdigit()
    ):
        raise ValueError("В пакете отсутствует версия или номер сборки.")
    return info


def verify_bundle(app: Path) -> dict:
    app = app.absolute()
    info = read_info(app)
    forbidden = {".env", "config.local.yaml", "application.json", ".git", "private", "Backups"}
    for base, directories, files in os.walk(app, followlinks=False):
        for name in directories + files:
            path = Path(base) / name
            if path.is_symlink() and not path.resolve().is_relative_to(app.resolve()):
                raise ValueError("В приложении найдена внешняя символическая ссылка.")
            public_ca = name == "cacert.pem" and "certifi" in path.parts
            if name in forbidden or (
                path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".key", ".pem"}
                and not public_ca
            ):
                raise ValueError(f"Личные данные не должны входить в приложение: {name}")
    binary = app / "Contents/MacOS/Chushpan"
    arch = subprocess.run(
        ["/usr/bin/lipo", "-archs", str(binary)], check=True, capture_output=True, text=True
    ).stdout.split()
    if "arm64" not in arch:
        raise ValueError("В приложении отсутствует архитектура arm64.")
    subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        check=True,
        capture_output=True,
    )
    return {
        "app": str(app),
        "bundle_id": BUNDLE_ID,
        "version": info["CFBundleShortVersionString"],
        "build": str(info["CFBundleVersion"]),
        "executable_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "code_identity": code_identity(app),
    }


def code_identity(app: Path) -> dict:
    result = subprocess.run(
        ["/usr/bin/codesign", "-d", "-r-", "--verbose=4", str(app)],
        check=True,
        capture_output=True,
        text=True,
    )
    value = {}
    for line in (result.stdout + getattr(result, "stderr", "")).splitlines():
        key, _, data = line.partition("=")
        if key in {"Identifier", "CDHash", "TeamIdentifier", "Signature"}:
            value[key] = data
        elif line.startswith("designated =>") or line.startswith("# designated =>"):
            value["requirement"] = line.split("=>", 1)[1].strip()
    return value


def self_test(app: Path, directory: Path) -> None:
    """Import-only check: no UI, microphone, device scan or real user data."""
    if app != INSTALLED_APP:
        raise ValueError("Проверочный запуск разрешён только из /Applications/Чушпан.app.")
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    report = directory / "imports.json"
    env = {**os.environ, "CHUSHPAN_APP_DATA": str(directory / "data")}
    for key in ("PYTHONHOME", "PYTHONPATH", "CHUSHPAN_CONTROL_TOKEN"):
        env.pop(key, None)
    subprocess.run(
        [str(app / "Contents/MacOS/Chushpan"), "--self-test", str(report)],
        check=True,
        env=env,
        capture_output=True,
        timeout=180,
    )
    result = json.loads(report.read_text())
    if not result or any(value != "ok" for value in result.values()):
        raise ValueError("Проверка импортов приложения не пройдена.")


def assert_not_running() -> None:
    from AppKit import NSWorkspace

    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() == BUNDLE_ID:
            raise ValueError("Сначала закрой Чушпан через ⌘Q; обновление не завершает его само.")


def register_installed(app: Path) -> None:
    """Only the canonical installed app and its owned helper can be registered."""
    if app != INSTALLED_APP:
        raise ValueError("В Launch Services регистрируется только /Applications/Чушпан.app.")
    subprocess.run([str(LSREGISTER), "-f", str(app)], check=True, capture_output=True)
    helper = app / "Contents/Helpers/Чушпан — утро.app"
    if helper.is_dir():
        subprocess.run([str(LSREGISTER), "-f", str(helper)], check=True, capture_output=True)


def unregister_previous(app: Path) -> None:
    if app != INSTALLED_APP:
        raise ValueError("Можно снять регистрацию только проверенной основной копии.")
    read_info(app)
    helper = app / "Contents/Helpers/Чушпан — утро.app"
    if helper.is_dir():
        subprocess.run([str(LSREGISTER), "-u", str(helper)], check=True, capture_output=True)
    subprocess.run([str(LSREGISTER), "-u", str(app)], check=True, capture_output=True)


def canonical_dock_tiles(tiles: list[dict]) -> list[dict] | None:
    """Refresh a pinned icon's bookmark after replacement; never add an unsolicited icon."""
    positions = [
        index
        for index, tile in enumerate(tiles)
        if tile.get("tile-data", {}).get("bundle-identifier") == BUNDLE_ID
    ]
    if not positions:
        return None
    first = tiles[positions[0]]
    result = [tile for index, tile in enumerate(tiles) if index not in positions]
    result.insert(
        min(positions[0], len(result)),
        {
            "GUID": first["GUID"],
            "tile-type": "file-tile",
            "tile-data": {
                "file-data": {"_CFURLString": INSTALLED_APP.as_uri() + "/", "_CFURLStringType": 15},
                "file-label": "Чушпан",
                "bundle-identifier": BUNDLE_ID,
                "file-type": 41,
            },
        },
    )
    return result


def refresh_dock() -> None:
    preferences = plistlib.loads(
        subprocess.run(
            ["/usr/bin/defaults", "export", "com.apple.dock", "-"],
            check=True,
            capture_output=True,
        ).stdout
    )
    tiles = canonical_dock_tiles(preferences.get("persistent-apps", []))
    if tiles is None:
        return
    from CoreFoundation import CFPreferencesAppSynchronize, CFPreferencesSetAppValue

    CFPreferencesSetAppValue("persistent-apps", tiles, "com.apple.dock")
    if not CFPreferencesAppSynchronize("com.apple.dock"):
        raise OSError("Не удалось обновить ссылку Dock на основное приложение.")
    subprocess.run(["/usr/bin/killall", "Dock"], capture_output=True)


def trash_build(app: Path, installed_receipt: dict) -> str:
    """Explicit opt-in cleanup of this successful build only; never backups or source."""
    app = app.absolute()
    if installed_receipt.get("status") != "installed" or installed_receipt.get("source") != str(
        app
    ):
        raise ValueError("Нет подтверждения успешной установки этой сборки.")
    if app.is_symlink() or not app.resolve().is_relative_to(build_cache().resolve()):
        raise ValueError("Очищать можно только сборку из Builds.noindex.")
    if app.name != APP_NAME or app == INSTALLED_APP:
        raise ValueError("Этот путь защищён от очистки.")
    current = verify_bundle(INSTALLED_APP)
    source = verify_bundle(app)
    if any(
        current[k] != installed_receipt[k] or source[k] != installed_receipt[k]
        for k in ("version", "build", "executable_sha256")
    ):
        raise ValueError("Сборка или установленное приложение изменились после проверки.")
    from Foundation import NSURL, NSFileManager

    ok, url, error = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(str(app)), None, None
    )
    if not ok:
        raise OSError(f"Сборка сохранена: перенос в Корзину не удался ({error}).")
    # macOS can register a trashed bundle itself. Remove only these verified origins.
    trashed = Path(url.path())
    read_info(trashed)
    for origin in (app, trashed):
        subprocess.run([str(LSREGISTER), "-u", str(origin)], capture_output=True)
    register_installed(INSTALLED_APP)
    return str(url.path())


def install(app: Path, *, cleanup: bool = False, allow_downgrade: bool = False) -> dict:
    state = update_state()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Другое обновление уже выполняется.") from exc
        return _install(app, cleanup=cleanup, allow_downgrade=allow_downgrade)


def _install(app: Path, *, cleanup: bool = False, allow_downgrade: bool = False) -> dict:
    """Verify before and after atomic replacement; preserve the old app even on success."""
    destination = INSTALLED_APP
    pending = [
        path
        for path in update_state().glob("*/receipt.json")
        if json.loads(path.read_text()).get("status") == "replacing"
    ]
    if pending:
        raise ValueError("Незавершённое обновление: scripts.install_macos --recover")
    app = app.absolute()
    if app.resolve() == destination.resolve():
        raise ValueError("Основное приложение нельзя использовать как каталог сборки.")
    if cleanup and (
        app.is_symlink()
        or app.name != APP_NAME
        or not app.resolve().is_relative_to(build_cache().resolve())
    ):
        raise ValueError("Очистка разрешена только для сборки из Builds.noindex.")
    checked = verify_bundle(app)
    if destination.exists():
        old = read_info(destination)
        if not allow_downgrade and int(checked["build"]) < int(old["CFBundleVersion"]):
            raise ValueError("Установка более старой сборки запрещена; используй явный откат.")
    assert_not_running()
    state = update_state()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    record = state / stamp
    record.mkdir(mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix=".chushpan-", suffix=".noindex", dir=destination.parent))
    staged = stage / APP_NAME
    previous = record / APP_NAME
    if record.stat().st_dev != destination.parent.stat().st_dev:
        raise ValueError("Резерв и /Applications должны находиться на одном файловом томе.")
    subprocess.run(["/usr/bin/ditto", str(app), str(staged)], check=True, capture_output=True)
    if verify_bundle(staged)["executable_sha256"] != checked["executable_sha256"]:
        raise ValueError("Копия сборки не прошла проверку.")
    assert_not_running()
    had_previous = destination.exists()
    prior_identity = verify_bundle(destination).get("code_identity", {}) if had_previous else {}
    write_receipt(
        record / "receipt.json",
        {**checked, "status": "replacing", "previous": str(previous), "source": str(app)},
    )
    published = False
    try:
        if had_previous:
            unregister_previous(destination)
            destination.rename(previous)
        staged.rename(destination)
        published = True
        installed = verify_bundle(destination)
        self_test(destination, record / "installed-check")
        register_installed(destination)
        refresh_dock()
    except BaseException:
        if published and destination.exists():
            destination.rename(stage / "failed.app")
        if previous.exists():
            previous.rename(destination)
        write_receipt(record / "receipt.json", {**checked, "status": "rolled_back"})
        if had_previous and destination.exists():
            register_installed(destination)
            refresh_dock()
        raise
    receipt = {
        **installed,
        "status": "installed",
        "source": str(app),
        "previous": str(previous) if had_previous else None,
        "installed_at": datetime.now(UTC).isoformat(),
        "cleanup_candidates": [str(app)],
        "previous_code_identity": prior_identity,
        "permissions_may_need_refresh": bool(
            prior_identity.get("requirement")
            and prior_identity.get("requirement") != installed["code_identity"].get("requirement")
        ),
    }
    write_receipt(record / "receipt.json", receipt)
    write_receipt(state / "last-install.json", receipt)
    if cleanup:
        receipt["trashed_build"] = trash_build(app, receipt)
        write_receipt(record / "receipt.json", receipt)
        write_receipt(state / "last-install.json", receipt)
    return receipt


def rollback_last() -> dict:
    """Explicit rollback still verifies/copies the backup and preserves the current app."""
    state = update_state()
    receipt = json.loads((state / "last-install.json").read_text())
    previous = Path(receipt.get("previous") or "")
    if previous.name != APP_NAME or not previous.resolve().is_relative_to(state.resolve()):
        raise ValueError("Нет проверенного резерва для отката.")
    return install(previous, allow_downgrade=True)


def recover_interrupted() -> dict:
    """Recover one interrupted transaction; retain its new app and restore verified old app."""
    state = update_state()
    with (state / "update.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pending = [
            p
            for p in state.glob("*/receipt.json")
            if json.loads(p.read_text()).get("status") == "replacing"
        ]
        if len(pending) != 1:
            raise ValueError("Нет единственной незавершённой транзакции для восстановления.")
        receipt_path = pending[0]
        previous = Path(json.loads(receipt_path.read_text())["previous"])
        if previous != receipt_path.parent / APP_NAME:
            raise ValueError("Резерв не принадлежит этой транзакции.")
        if previous.exists():
            verify_bundle(previous)
            assert_not_running()
            if INSTALLED_APP.exists():
                read_info(INSTALLED_APP)
                INSTALLED_APP.rename(receipt_path.parent / "interrupted-new.app")
            previous.rename(INSTALLED_APP)
        elif not INSTALLED_APP.exists():
            raise ValueError("Не найдено установленное приложение или резерв; файлы сохранены.")
        restored = verify_bundle(INSTALLED_APP)
        self_test(INSTALLED_APP, receipt_path.parent / ("recovery-check-" + uuid.uuid4().hex[:8]))
        register_installed(INSTALLED_APP)
        refresh_dock()
        result = {**restored, "status": "recovered"}
        write_receipt(receipt_path, result)
        return result
