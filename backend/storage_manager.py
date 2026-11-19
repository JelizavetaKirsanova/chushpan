"""On-demand inventory and conservative cleanup for Chushpan-owned artifacts."""

from __future__ import annotations

import fcntl
import getpass
import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

APP_NAME = "Чушпан.app"
APP = Path("/Applications") / APP_NAME
STALE_BUILD_AGE_SECONDS = 24 * 60 * 60
BACKUP_KEEP_RECENT = 3
BACKUP_KEEP_OLDER = 1
BACKUP_MAX_COUNT = 8
BACKUP_MAX_BYTES = 16 * 1024**3


def roots(home: Path | None = None) -> dict[str, Path]:
    home = home or Path.home()
    support = home / "Library/Application Support/Chushpan"
    return {
        "builds": home / "Library/Caches/Chushpan/Builds.noindex",
        "updates": support / "Updates.noindex",
        "backups": support / "Backups.noindex",
        "cache": home / "Library/Caches/Chushpan",
    }


def logical_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for base, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
        for name in files:
            p = Path(base) / name
            try:
                if not p.is_symlink():
                    total += p.stat().st_size
            except OSError:
                continue
    return total


def allocated_size(path: Path) -> int:
    """Allocated blocks from st_blocks; APFS clones/hard links may still overstate reclaim."""
    if not path.exists():
        return 0
    if path.is_file():
        return getattr(path.stat(), "st_blocks", 0) * 512
    total = 0
    seen = set()
    for base, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
        for name in files:
            p = Path(base) / name
            try:
                if not p.is_symlink():
                    stat = p.stat()
                    key = (stat.st_dev, stat.st_ino)
                    if key not in seen:
                        seen.add(key)
                        total += getattr(stat, "st_blocks", 0) * 512
            except OSError:
                continue
    return total


def verified_backup(path: Path) -> bool:
    """Verify every manifest hash; does not modify or restore over user data."""
    try:
        doc = json.loads((path / "manifest.json").read_text())
        files = doc["sha256"]
        if not isinstance(files, dict) or not files:
            return False
        digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        if digest != doc["digest"]:
            return False
        for relative, expected in files.items():
            p = (path / relative).resolve()
            if not p.is_relative_to(path.resolve()) or not p.is_file():
                return False
            h = hashlib.sha256()
            with p.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(block)
            if h.hexdigest() != expected:
                return False
            if p.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                with sqlite3.connect(p.as_uri() + "?mode=ro", uri=True) as db:
                    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def restore_backup(path: Path, destination: Path) -> Path:
    """Restore only into a new/empty directory, then verify hashes and SQLite integrity."""
    if not verified_backup(path):
        raise ValueError("Резервная копия не прошла проверку целостности.")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Для безопасного восстановления нужна пустая папка назначения.")
    destination.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((path / "manifest.json").read_text())
    for relative in manifest["sha256"]:
        source = path / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if target.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            with sqlite3.connect(target) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Восстановленная база SQLite не прошла quick_check.")
    return destination


def backup_catalog(base: Path) -> list[dict]:
    if base.is_symlink():
        return []
    state = base.parent / "storage-policy.json"
    try:
        pins = set(json.loads(state.read_text()).get("pinned", []))
    except (OSError, ValueError):
        pins = set()
    rows = []
    for path in sorted(base.glob("snapshot-*"), reverse=True):
        if path.is_symlink():
            continue
        good = verified_backup(path)
        try:
            manifest = json.loads((path / "manifest.json").read_text())
            contents = [
                {"path": relative, "size": (path / relative).stat().st_size}
                for relative in manifest.get("sha256", {})
                if (path / relative).resolve().is_relative_to(path.resolve())
                and (path / relative).is_file()
            ]
        except (OSError, ValueError, TypeError):
            contents = []
        rows.append(
            {
                "path": str(path),
                "size": logical_size(path),
                "file_count": len(contents),
                "contents_preview": contents[:8],
                "verified": good,
                "pinned": path.name in pins,
                "restorable": good,
            }
        )
    return rows


def open_paths() -> set[Path] | None:
    """Return lsof paths; None means inspection failed and cleanup must fail closed."""
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-Fn", "-nP", "-u", getpass.getuser()],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            return None
        managed = [str(path.absolute()).rstrip("/") for path in roots().values()]
        paths = set()
        for line in result.stdout.splitlines():
            if not line.startswith("n/"):
                continue
            name = line[1:]
            if any(name == root or name.startswith(root + "/") for root in managed):
                paths.add(Path(name))
        return paths
    except (OSError, subprocess.TimeoutExpired):
        return None


def in_use(path: Path, opened: set[Path] | None) -> bool:
    return opened is None or any(
        p == path.resolve() or p.is_relative_to(path.resolve()) for p in opened
    )


def storage_report(home: Path | None = None, app: Path | None = None) -> dict:
    dirs = roots(home)
    app = app or APP
    report = {
        key: {
            "path": str(path),
            "logical_bytes": logical_size(path),
            "allocated_bytes": allocated_size(path),
        }
        for key, path in dirs.items()
    }
    # Builds are nested below the cache root and are shown separately.
    report["cache"]["logical_bytes"] = max(
        0, report["cache"]["logical_bytes"] - report["builds"]["logical_bytes"]
    )
    report["cache"]["allocated_bytes"] = max(
        0, report["cache"]["allocated_bytes"] - report["builds"]["allocated_bytes"]
    )
    report["application"] = {
        "path": str(app),
        "logical_bytes": logical_size(app),
        "allocated_bytes": allocated_size(app),
    }
    report["backups_list"] = backup_catalog(dirs["backups"])
    report["backups"]["count"] = len(report["backups_list"])
    report["backups"]["verified_count"] = sum(
        1 for row in report["backups_list"] if row["verified"]
    )
    report["updates"]["count"] = (
        sum(1 for path in dirs["updates"].iterdir() if path.is_dir() and not path.is_symlink())
        if dirs["updates"].is_dir() and not dirs["updates"].is_symlink()
        else 0
    )
    report["builds"]["count"] = (
        sum(
            1
            for path in dirs["builds"].glob("release-*")
            if path.is_dir() and not path.is_symlink()
        )
        if dirs["builds"].is_dir() and not dirs["builds"].is_symlink()
        else 0
    )
    try:
        report["auto_cleanup"] = json.loads(
            (dirs["backups"].parent / "storage-policy.json").read_text()
        ).get("auto_cleanup", False)
    except (OSError, ValueError):
        report["auto_cleanup"] = False
    try:
        policy = json.loads((dirs["backups"].parent / "storage-policy.json").read_text())
    except (OSError, ValueError):
        policy = {}
    report["backup_max_count"] = int(policy.get("backup_max_count", BACKUP_MAX_COUNT))
    report["backup_max_bytes"] = int(policy.get("backup_max_bytes", BACKUP_MAX_BYTES))
    report["backup_limit_exceeded"] = report["backups"]["count"] > report["backup_max_count"] or (
        report["backups"]["logical_bytes"] > report["backup_max_bytes"]
    )
    report["total_logical_bytes"] = (
        sum(report[k]["logical_bytes"] for k in dirs) + report["application"]["logical_bytes"]
    )
    disk = shutil.disk_usage(home or Path.home())
    report["free_bytes"] = disk.free
    report["free_percent"] = round(100 * disk.free / max(1, disk.total), 1)
    report["low_space"] = disk.free < 10 * 1024**3 or report["free_percent"] < 10
    return report


def snapshot_contents(path: str, home: Path | None = None) -> dict:
    base = roots(home)["backups"]
    raw = Path(path)
    if raw.is_symlink() or raw.name[:9] != "snapshot-":
        raise ValueError("Можно просматривать только каталог snapshot-*.")
    target = raw.resolve()
    if target.parent != base.resolve() or not target.is_dir() or not verified_backup(target):
        raise ValueError("Снимок отсутствует или не прошёл проверку целостности.")
    manifest = json.loads((target / "manifest.json").read_text())
    files = []
    for relative in sorted(manifest["sha256"]):
        candidate = (target / relative).resolve()
        if candidate.is_relative_to(target) and candidate.is_file():
            files.append({"path": relative, "size": candidate.stat().st_size})
    return {"path": str(target), "files": files, "size": sum(row["size"] for row in files)}


def prune_update_history_after_success(home: Path | None = None, app: Path | None = None) -> dict:
    """Delete only obsolete successful update transactions after a committed install."""
    home, app = home or Path.home(), app or APP
    dirs = roots(home)
    state = dirs["updates"]
    if state.is_symlink() or not state.is_dir():
        return {"removed": [], "deferred": ["update directory unavailable"]}
    opened = open_paths()
    if opened is None:
        return {"removed": [], "deferred": ["process scan unavailable"]}
    rows = _updates_plan(state, app)
    removed, deferred = [], []
    root = state.resolve()
    for row in rows:
        folder = Path(row["path"])
        if folder.is_symlink() or folder.resolve().parent != root:
            deferred.append(str(folder))
            continue
        if in_use(folder, opened):
            deferred.append(str(folder))
            continue
        allocated = allocated_size(folder)
        shutil.rmtree(folder)
        removed.append({**row, "allocated_size": allocated})
    return {"removed": removed, "deferred": deferred}


def cleanup_stale_builds(home: Path | None = None) -> dict:
    """Remove abandoned release directories only when no builder or user has them open."""
    dirs = roots(home)
    base = dirs["builds"]
    if base.is_symlink() or not base.exists():
        return {"removed": [], "deferred": []}
    lock = (base.parent / "build.lock").open("a")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"removed": [], "deferred": ["build.lock"]}
        opened = open_paths()
        if opened is None:
            return {"removed": [], "deferred": ["process scan unavailable"]}
        removed, deferred = [], []
        now = time.time()
        for folder in base.glob("release-*"):
            if folder.is_symlink() or not folder.is_dir():
                continue
            if in_use(folder, opened):
                deferred.append(str(folder))
                continue
            completion = folder / "build-complete.json"
            marker_mtime = completion.stat().st_mtime if completion.is_file() else 0
            if now - max(folder.stat().st_mtime, marker_mtime) < STALE_BUILD_AGE_SECONDS:
                deferred.append(str(folder))
                continue
            shutil.rmtree(folder)
            removed.append(str(folder))
        return {"removed": removed, "deferred": deferred}
    finally:
        lock.close()


def _updates_plan(state: Path, installed: Path) -> list[dict]:
    receipts = []
    for receipt in state.glob("*/receipt.json"):
        try:
            value = json.loads(receipt.read_text())
        except (OSError, ValueError):
            continue
        if value.get("status") == "installed":
            receipts.append((receipt, value))
    latest = None
    try:
        last_install = json.loads((state / "last-install.json").read_text())
        transaction = Path(last_install.get("transaction") or "")
        if (
            transaction.parent.resolve() == state.resolve()
            and not transaction.is_symlink()
            and transaction.is_dir()
        ):
            latest = next(
                (pair for pair in receipts if pair[0].parent.resolve() == transaction.resolve()),
                None,
            )
    except (OSError, ValueError, TypeError):
        pass
    latest = latest or max(receipts, key=lambda pair: pair[0].parent.name, default=None)
    keep = Path(latest[1].get("previous") or "").resolve() if latest else None
    candidates = []
    for receipt, value in receipts:
        folder = receipt.parent
        if folder.is_symlink() or (latest and folder == latest[0].parent):
            continue
        backup = Path(value.get("previous") or "")
        # Historical packages are disposable only when the bundle metadata proves
        # they are Chushpan and the current rollback bundle remains independently intact.
        if backup.name != APP_NAME or not backup.is_dir() or backup.resolve() == keep:
            continue
        try:
            info = plistlib.loads((backup / "Contents/Info.plist").read_bytes())
            if info.get("CFBundleIdentifier") != "ee.chushpan.desktop":
                continue
        except (OSError, ValueError):
            continue
        candidates.append(
            {
                "path": str(folder),
                "size": logical_size(folder),
                "kind": "старый пакет обновления",
                "build": value.get("build"),
            }
        )
    return candidates


def _update_recovery_ready(state: Path, installed: Path) -> tuple[bool, str | None]:
    """Require the installed bundle and latest rollback package to verify before pruning."""
    try:
        receipts = []
        for path in state.glob("*/receipt.json"):
            try:
                value = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if value.get("status") == "installed":
                receipts.append((path, value))
        latest = None
        last_install = json.loads((state / "last-install.json").read_text())
        transaction = Path(last_install.get("transaction") or "")
        if transaction.parent.resolve() == state.resolve() and not transaction.is_symlink():
            latest = next(
                (item for item in receipts if item[0].parent.resolve() == transaction.resolve()),
                None,
            )
        latest = latest or max(receipts, key=lambda item: item[0].parent.name, default=None)
        if not latest:
            return False, "Нет подтверждённой успешной транзакции обновления."
        from scripts.macos_release import verify_bundle

        current = verify_bundle(installed)
        if current["build"] != str(latest[1].get("build")) or current[
            "executable_sha256"
        ] != latest[1].get("executable_sha256"):
            return False, "Установленное приложение не совпадает с последним receipt."
        previous = Path(latest[1].get("previous") or "")
        if previous != latest[0].parent / APP_NAME or not previous.is_dir():
            return False, "Рабочая копия для отката отсутствует или находится вне транзакции."
        rollback = verify_bundle(previous)
        if rollback["bundle_id"] != "ee.chushpan.desktop":
            return False, "Копия для отката не прошла проверку bundle ID."
        expected = latest[1].get("previous_code_identity") or {}
        actual = rollback.get("code_identity") or {}
        for key in ("Identifier", "CDHash", "requirement"):
            if expected.get(key) and actual.get(key) != expected[key]:
                return False, "Идентичность копии для отката не совпадает с receipt."
        return True, None
    except Exception as exc:
        return (
            False,
            f"Проверка установленной или откатной версии не прошла ({type(exc).__name__}).",
        )


def preview_cleanup(
    home: Path | None = None,
    app: Path | None = None,
    *,
    backup_lock_held: bool = False,
) -> dict:
    home, app = home or Path.home(), app or APP
    dirs = roots(home)
    state = dirs["updates"]
    update_integrity_ok, update_integrity_error = _update_recovery_ready(state, app)
    candidates = _updates_plan(state, app) if update_integrity_ok and not state.is_symlink() else []
    opened = open_paths()
    protected = [row["path"] for row in candidates if in_use(Path(row["path"]), opened)]
    candidates = [row for row in candidates if row["path"] not in protected]
    backup_candidates = []
    backup_rows = []
    backup_summary = {"verified_count": 0, "unverified_count": 0, "pinned_count": 0, "bytes": 0}
    backup_lock = None
    backup_lock_ok = backup_lock_held
    try:
        if not backup_lock_held:
            if dirs["backups"].exists():
                backup_lock = (dirs["backups"] / ".backup.lock").open("a")
                fcntl.flock(backup_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            backup_lock_ok = True
        if backup_lock_ok and opened is not None and not dirs["backups"].is_symlink():
            backup_rows = backup_catalog(dirs["backups"])
            verified = [row for row in backup_rows if row["verified"]]
            pinned = {row["path"] for row in backup_rows if row["pinned"]}
            backup_summary = {
                "verified_count": len(verified),
                "unverified_count": len(backup_rows) - len(verified),
                "pinned_count": len(pinned),
                "bytes": sum(row["size"] for row in backup_rows),
            }
            keep = {row["path"] for row in verified[:BACKUP_KEEP_RECENT]} | pinned
            if verified:
                keep.add(verified[-1]["path"])
            backup_candidates = [
                {
                    "path": row["path"],
                    "size": row["size"],
                    "allocated_size": allocated_size(Path(row["path"])),
                    "kind": "проверенный старый резерв",
                }
                for row in verified
                if row["path"] not in keep and not in_use(Path(row["path"]), opened)
            ]
    except (OSError, BlockingIOError):
        backup_lock_ok = False
    finally:
        if backup_lock:
            backup_lock.close()
    # Build artifacts are not eligible while a build lock exists. Keep the newest
    # successful build as the installation source; preview older release folders.
    build_base = dirs["builds"]
    build_candidates = []
    lock_path = build_base.parent / "build.lock"
    lock_busy = False
    try:
        lock = lock_path.open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_busy = True
        finally:
            lock.close()
    except OSError:
        lock_busy = True
    if not lock_busy and opened is not None and build_base.exists() and not build_base.is_symlink():
        dirs_found = sorted(
            build_base.glob("release-*"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        keep = dirs_found[0] if dirs_found else None
        for folder in dirs_found:
            if folder == keep:
                continue
            build_candidates.append(
                {
                    "path": str(folder),
                    "size": logical_size(folder),
                    "allocated_size": allocated_size(folder),
                    "kind": "старая сборка",
                }
            )
    entries = candidates + build_candidates + backup_candidates
    if opened is not None:
        protected.extend(row["path"] for row in backup_rows if in_use(Path(row["path"]), opened))
    token = hashlib.sha256(
        json.dumps([(x["path"], x["size"]) for x in entries], sort_keys=True).encode()
    ).hexdigest()
    return {
        "candidates": entries,
        "token": token,
        "estimated_logical_bytes": sum(x["size"] for x in entries),
        "estimated_allocated_bytes": sum(
            x.get("allocated_size", allocated_size(Path(x["path"]))) for x in entries
        ),
        "backup_candidates": backup_candidates,
        "backup_summary": backup_summary,
        "backup_scan_available": backup_lock_ok and opened is not None,
        "update_scan_available": update_integrity_ok,
        "update_scan_error": update_integrity_error,
        "protected_active_paths": protected,
        "process_scan_available": opened is not None,
        "message": "APFS snapshots и clones могут уменьшить реально освобождаемое место.",
    }


def apply_cleanup(
    home: Path | None = None, app: Path | None = None, expected_token: str | None = None
) -> dict:
    home, app = home or Path.home(), app or APP
    dirs = roots(home)
    if any(dirs[key].is_symlink() for key in ("updates", "builds", "backups")):
        raise ValueError("Очистка отключена: один из каталогов хранилища является ссылкой.")
    state = dirs["updates"]
    state.mkdir(parents=True, exist_ok=True)
    update_lock = (state / "update.lock").open("a")
    try:
        fcntl.flock(update_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        update_lock.close()
        raise ValueError("Очистка отложена: сейчас выполняется обновление.") from exc
    try:
        dirs["backups"].mkdir(parents=True, exist_ok=True, mode=0o700)
        backup_lock = (dirs["backups"] / ".backup.lock").open("a")
        try:
            fcntl.flock(backup_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            backup_lock.close()
            raise ValueError(
                "Очистка резервов отложена: выполняется резервное копирование."
            ) from exc
        plan = preview_cleanup(home, app, backup_lock_held=True)
        if not expected_token or expected_token != plan["token"]:
            raise ValueError(
                "Состав файлов изменился после просмотра. Обнови предварительный просмотр."
            )
        removed = []
        deferred = []
        for row in plan["candidates"]:
            raw_target = Path(row["path"])
            if raw_target.is_symlink():
                deferred.append(row["path"])
                continue
            target = raw_target.resolve()
            allowed = any(
                target.is_relative_to(dirs[key].resolve())
                for key in ("updates", "builds", "backups")
            )
            if not allowed or target == app.resolve() or target.is_symlink():
                continue
            if target.exists():
                if in_use(target, open_paths()):
                    deferred.append(str(target))
                    continue
                # Recheck build lock immediately before touching a build directory.
                allocated = allocated_size(target)
                if target.is_relative_to(dirs["backups"].resolve()) and not verified_backup(target):
                    deferred.append(str(target))
                    continue
                if target.is_relative_to(dirs["builds"].resolve()):
                    lock = (dirs["builds"].parent / "build.lock").open("a")
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        lock.close()
                        continue
                    shutil.rmtree(target)
                    lock.close()
                else:
                    shutil.rmtree(target)
                removed.append({**row, "freed_allocated_bytes": allocated})
        return {
            "removed": removed,
            "deferred": deferred,
            "freed_logical_bytes": sum(x["size"] for x in removed),
        }
    finally:
        if "backup_lock" in locals() and not backup_lock.closed:
            backup_lock.close()
        update_lock.close()


def set_backup_pinned(path: str, pinned: bool, home: Path | None = None) -> None:
    base = roots(home)["backups"]
    if base.is_symlink():
        raise ValueError("Закрепление отключено: каталог резервов является ссылкой.")
    target = Path(path).resolve()
    if not target.is_relative_to(base.resolve()) or not target.name.startswith("snapshot-"):
        raise ValueError("Можно закрепить только резервную копию Чушпана.")
    if pinned and not verified_backup(target):
        raise ValueError("Нельзя закрепить резервную копию с неподтверждённой целостностью.")
    policy = base.parent / "storage-policy.json"
    try:
        doc = json.loads(policy.read_text())
    except (OSError, ValueError):
        doc = {"pinned": []}
    pins = set(doc.get("pinned", []))
    (pins.add if pinned else pins.discard)(target.name)
    temporary = policy.with_name(policy.name + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "pinned": sorted(pins),
                "auto_cleanup": doc.get("auto_cleanup", False),
                **{
                    key: value
                    for key, value in doc.items()
                    if key not in {"pinned", "auto_cleanup"}
                },
            },
            indent=2,
        )
    )
    temporary.chmod(0o600)
    temporary.replace(policy)
