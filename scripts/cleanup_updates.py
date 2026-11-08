"""Safely prune old Chushpan update transactions while the app is stopped.

Preview first. Applying requires the preview token and will only remove verified,
successful update transactions that retain neither the installed app nor rollback.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.storage_manager import _updates_plan, open_paths  # noqa: E402
from scripts.macos_release import (  # noqa: E402
    BUNDLE_ID,
    INSTALLED_APP,
    verify_bundle,
)


def _token(rows: list[dict]) -> str:
    value = sorted((str(Path(row["path"]).resolve()), row["size"]) for row in rows)
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def _process_table() -> list[tuple[int, int, str]]:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,args="],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Не удалось проверить список процессов.")
    rows = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    return rows


def _running_application(app_path: Path):
    try:
        from AppKit import NSWorkspace
    except ImportError as exc:  # pragma: no cover - macOS runtime only
        raise RuntimeError("Невозможно проверить Чушпан через Launch Services.") from exc

    matches = []
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() != BUNDLE_ID:
            continue
        bundle_url = app.bundleURL()
        if not bundle_url or Path(str(bundle_url.path())).resolve() != app_path.resolve():
            continue
        matches.append(app)
    return matches


def stop_chushpan(app_path: Path, *, timeout: float = 20.0) -> None:
    """Ask only the canonical app to quit; stop only its exact orphan worker."""
    applications = _running_application(app_path)
    for app in applications:
        if not app.terminate():
            raise RuntimeError("Чушпан не принял штатное завершение; очистка остановлена.")

    binary = str(app_path / "Contents/MacOS/Chushpan")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _process_table()
        mains = [row for row in rows if binary in row[2] and "--desktop-worker" not in row[2]]
        if not mains:
            break
        time.sleep(0.25)
    else:
        raise RuntimeError("Чушпан не завершился штатно; очистка остановлена.")

    # The app normally reaps its worker in quit(). If a worker outlives the app,
    # signal only that app-owned worker, never a matching name from another app.
    rows = _process_table()
    workers = [row for row in rows if binary in row[2] and "--desktop-worker" in row[2]]
    for pid, _, _ in workers:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    worker_pids = {row[0] for row in workers}
    deadline = time.monotonic() + 8.0
    while worker_pids and time.monotonic() < deadline:
        alive = {row[0] for row in _process_table() if row[0] in worker_pids}
        if not alive:
            break
        time.sleep(0.25)
    else:
        if worker_pids:
            raise RuntimeError("Worker Чушпана не завершился штатно; очистка остановлена.")


def _assert_no_transaction_processes() -> None:
    own = {os.getpid(), os.getppid()}
    needles = (
        "scripts/build_macos.py",
        "scripts/update_macos.py",
        "scripts/install_macos.py",
    )
    active = [
        (pid, command)
        for pid, _, command in _process_table()
        if pid not in own and any(needle in command for needle in needles)
    ]
    if active:
        raise RuntimeError("Обнаружена активная сборка или установка; очистка остановлена.")


def _assert_app_stopped(app: Path) -> None:
    binary = str(app / "Contents/MacOS/Chushpan")
    active = [row for row in _process_table() if binary in row[2]]
    if active:
        raise RuntimeError("Процесс Чушпана ещё работает; очистка остановлена.")


def relaunch_chushpan(app: Path, *, timeout: float = 25.0) -> None:
    """Open the installed app with auto-listen disabled and verify Launch Services."""
    subprocess.run(
        ["/usr/bin/open", "-na", str(app), "--args", "--no-auto-listen"],
        check=True,
        capture_output=True,
        timeout=10,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(
            Path(str(item.bundleURL().path())).resolve() == app.resolve()
            for item in _running_application(app)
        ):
            return
        time.sleep(0.25)
    raise RuntimeError("Установленный Чушпан не появился в Launch Services после запуска.")


def _plan(state: Path, app: Path) -> list[dict]:
    if state.is_symlink():
        raise RuntimeError("Updates.noindex является ссылкой; очистка остановлена.")
    rows = _updates_plan(state, app)
    root = state.resolve()
    for row in rows:
        path = Path(row["path"])
        if path.is_symlink() or path.resolve().parent != root:
            raise RuntimeError("Найден неожиданный путь кандидата; очистка остановлена.")
    return rows


def preview_updates(*, home: Path | None = None, app: Path | None = None) -> dict:
    home = home or Path.home()
    app = app or INSTALLED_APP
    state = home / "Library/Application Support/Chushpan/Updates.noindex"
    rows = _plan(state, app)
    return {
        "candidates": rows,
        "count": len(rows),
        "logical_bytes": sum(row["size"] for row in rows),
        "token": _token(rows),
    }


def cleanup_updates(
    expected_token: str,
    *,
    home: Path | None = None,
    app: Path | None = None,
    shutdown=stop_chushpan,
    relaunch=relaunch_chushpan,
) -> dict:
    home = home or Path.home()
    app = app or INSTALLED_APP
    state = home / "Library/Application Support/Chushpan/Updates.noindex"
    if state.is_symlink() or not state.is_dir():
        raise RuntimeError("Каталог обновлений отсутствует или является ссылкой.")

    # Lock first, then close the app. This excludes concurrent updater activity.
    with (state / "update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Обновление уже выполняется; очистка остановлена.") from exc

        installed_before = verify_bundle(app)
        rows = _plan(state, app)
        if not expected_token or _token(rows) != expected_token:
            raise RuntimeError("Список обновился после предпросмотра; очистка остановлена.")

        rollback_path = None
        receipts = []
        for receipt in state.glob("*/receipt.json"):
            try:
                value = json.loads(receipt.read_text())
            except (OSError, ValueError):
                continue
            if value.get("status") == "installed":
                receipts.append((receipt, value))
        latest = max(receipts, key=lambda item: item[0].parent.name, default=None)
        if latest:
            rollback_path = Path(latest[1].get("previous") or "").resolve()
        if rollback_path is None or not rollback_path.is_dir():
            raise RuntimeError("Рабочая копия для отката не найдена; очистка остановлена.")
        rollback_before = verify_bundle(rollback_path)
        if rollback_before["bundle_id"] != BUNDLE_ID:
            raise RuntimeError("Копия для отката не прошла проверку.")
        if any(Path(row["path"]).resolve() in {app.resolve(), rollback_path} for row in rows):
            raise RuntimeError("Текущая или откатная версия попала в кандидаты.")

        shutdown(app)
        try:
            result = _finish_cleanup(
                expected_token,
                state=state,
                app=app,
                home=home,
                rows=rows,
                rollback_path=rollback_path,
                installed_before=installed_before,
                rollback_before=rollback_before,
            )
        except Exception as exc:
            try:
                relaunch(app)
            except Exception as launch_exc:
                exc.add_note(f"Также не удалось перезапустить Чушпан: {launch_exc}")
            raise
        launch_error = None
        try:
            relaunch(app)
        except Exception as exc:  # deletion is done; preserve a useful outcome report
            launch_error = f"{type(exc).__name__}: {exc}"
        result["app_launch_verified"] = launch_error is None
        result["app_launch_error"] = launch_error
        return result


def _finish_cleanup(
    expected_token: str,
    *,
    state: Path,
    app: Path,
    home: Path,
    rows: list[dict],
    rollback_path: Path,
    installed_before: dict,
    rollback_before: dict,
) -> dict:
    rows = _plan(state, app)
    if _token(rows) != expected_token:
        raise RuntimeError("После закрытия приложения список изменился; очистка остановлена.")
    _assert_no_transaction_processes()
    _assert_app_stopped(app)
    opened = open_paths()
    if opened is None:
        raise RuntimeError("Проверка открытых файлов не сработала; очистка остановлена.")
    candidate_roots = [Path(row["path"]).resolve() for row in rows]
    open_candidate = any(
        path == root or path.is_relative_to(root) for path in opened for root in candidate_roots
    )
    if open_candidate:
        raise RuntimeError("Кандидат всё ещё открыт другим процессом; очистка остановлена.")

    installed_now = verify_bundle(app)
    rollback_now = verify_bundle(rollback_path)
    if (
        installed_now["executable_sha256"] != installed_before["executable_sha256"]
        or rollback_now["executable_sha256"] != rollback_before["executable_sha256"]
    ):
        raise RuntimeError("Установленная или откатная версия изменилась; очистка остановлена.")

    # Recheck all targets before deleting the first one. Stay strictly within
    # Updates.noindex; no backup, build, database, preference or source paths.
    targets = []
    for row in rows:
        target = Path(row["path"])
        if target.is_symlink() or target.resolve().parent != state.resolve():
            raise RuntimeError("Путь кандидата изменился; очистка остановлена.")
        targets.append((target, row["size"]))
    before_free = shutil.disk_usage(home).free
    for target, _ in targets:
        shutil_rmtree(target)
    installed_after = verify_bundle(app)
    rollback_after = verify_bundle(rollback_path)
    if installed_after["executable_sha256"] != installed_before["executable_sha256"]:
        raise RuntimeError("Установленный пакет изменился после очистки.")
    if rollback_after["executable_sha256"] != rollback_before["executable_sha256"]:
        raise RuntimeError("Копия для отката изменилась после очистки.")
    after_free = shutil.disk_usage(home).free
    return {
        "removed": [target.name for target, _ in targets],
        "logical_bytes": sum(size for _, size in targets),
        "free_before": before_free,
        "free_after": after_free,
        "freed_bytes": max(0, after_free - before_free),
        "installed_build": installed_after["build"],
        "rollback_build": rollback_after["build"],
    }


def shutil_rmtree(path: Path) -> None:
    shutil.rmtree(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="применить подтверждённый preview")
    parser.add_argument("--expected-token", help="token из предыдущего preview")
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps(preview_updates(), ensure_ascii=False, indent=2))
        return 0
    if not args.expected_token:
        parser.error("для --apply требуется --expected-token из предварительного просмотра")
    result = cleanup_updates(args.expected_token)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
