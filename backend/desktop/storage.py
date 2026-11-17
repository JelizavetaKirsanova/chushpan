"""Private desktop preferences and explicit, copy-only migration."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start_hidden: bool = False
    auto_listen: bool = False
    restore_session: bool = True
    show_window: bool = True
    close_to_menu: bool = True
    background_voice: bool = True
    energy_saving: bool = False
    recover_engine: bool = True
    ambient_hotkey_enabled: bool = False
    ambient_hotkey_key: Literal["f9", "f10", "f11", "f12"] = "f12"


def load_web_port(root: Path) -> int:
    """Keep the WebKit origin stable; damaged state must never create a new library."""
    path = root / "web-origin.json"
    if path.is_symlink():
        raise RuntimeError("Файл адреса интерфейса не должен быть символической ссылкой.")
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return 0
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError(
            "Сохранённый адрес интерфейса повреждён; данные браузера сохранены."
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"port"}
        or type(value["port"]) is not int
        or not 1024 <= value["port"] <= 65535
    ):
        raise RuntimeError("Сохранённый адрес интерфейса некорректен; данные браузера сохранены.")
    return value["port"]


def save_web_port(root: Path, port: int) -> None:
    """Publish the first bound port atomically, without replacing a known origin."""
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Неверный локальный порт интерфейса.")
    existing = load_web_port(root)
    if existing:
        if existing != port:
            raise RuntimeError("Адрес интерфейса изменился; прежнее хранилище сохранено.")
        return
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=root, prefix=".web-origin-")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump({"port": port}, out)
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(temporary, root / "web-origin.json")
        except FileExistsError:
            if load_web_port(root) != port:
                raise RuntimeError(
                    "Адрес интерфейса изменился; прежнее хранилище сохранено."
                ) from None
    finally:
        temporary.unlink(missing_ok=True)


def load_preferences(root: Path) -> Preferences:
    try:
        return Preferences.model_validate_json((root / "application.json").read_text())
    except FileNotFoundError:
        return Preferences()
    except (ValidationError, UnicodeError):
        logging.getLogger(__name__).warning("Invalid application preferences; using safe defaults")
        return Preferences()


def save_preferences(root: Path, value: dict) -> Preferences:
    prefs = Preferences.model_validate(value)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=root, prefix=".application-")
    try:
        with os.fdopen(fd, "w") as out:
            out.write(prefs.model_dump_json(indent=2))
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, root / "application.json")
    finally:
        Path(name).unlink(missing_ok=True)
    return prefs


def migrate(source: Path, destination: Path) -> list[str]:
    """Import only named runtime files, never symlinks or code, without overwriting.

    SQLite's online backup includes committed WAL transactions. An incomplete migration
    remains retryable; each file is published atomically only after a successful copy.
    The source is never modified or removed.
    """
    source, destination = source.resolve(), destination.resolve()
    if source == destination or not (source / "config.yaml").is_file():
        raise ValueError("Выбери папку существующего проекта my-jarvis.")
    candidates = [Path(".env"), Path("config.local.yaml"), Path("personality.md")]
    for folder in ("data", "private", "models"):
        base = source / folder
        if base.is_symlink():
            raise ValueError(f"Символическая ссылка не переносится: {folder}")
        if base.is_dir():
            candidates.extend(p.relative_to(source) for p in base.rglob("*") if p.is_file())
    imported = []
    for relative in candidates:
        src, dst = source / relative, destination / relative
        if any(p.is_symlink() for p in (src, *src.parents)):
            raise ValueError(f"Символическая ссылка не переносится: {relative}")
        if any(p.endswith((".app", ".lock")) for p in relative.parts):
            continue
        if relative.parts[:2] in {("data", "native"), ("data", "logs")}:
            continue
        if src.suffix in {".lock", ".log"} or src.name.endswith(("-wal", "-shm")):
            continue
        if not src.exists() or dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=dst.parent, prefix=".migration-")
        os.close(fd)
        temporary = Path(name)
        try:
            if src.suffix in {".db", ".sqlite", ".sqlite3"}:
                with closing(sqlite3.connect(src.as_uri() + "?mode=ro", uri=True)) as original:
                    backup = sqlite3.connect(temporary)
                    try:
                        original.backup(backup)
                        if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise ValueError(f"SQLite повреждена: {relative}")
                        backup.execute("PRAGMA journal_mode=DELETE")
                    finally:
                        backup.close()
            else:
                shutil.copyfile(src, temporary)
            temporary.chmod(0o600)
            # link is atomic and cannot replace an existing user file.
            os.link(temporary, dst)
            imported.append(str(relative))
        finally:
            temporary.unlink(missing_ok=True)
    return imported
