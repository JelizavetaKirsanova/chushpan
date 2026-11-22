"""Private, versioned SQLite memory storage for the isolated Core API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

CATEGORIES = {"profile", "preference", "project", "episode", "habit", "correction", "note"}
SCHEMA_VERSION = 2
APPLICATION_ID = 0x4353484D
MAX_CONTENT_LENGTH = 4000


class MemoryConflict(ValueError):
    """An entry was changed by another request after the client's revision."""


class MemoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path) if str(path) != ":memory:" else ":memory:"
        if self.path != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        app_id = self.db.execute("PRAGMA application_id").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("Memory database schema is newer than this server release.")
        if app_id not in (0, APPLICATION_ID):
            raise RuntimeError("The file is not a Chushpan Core memory database.")
        if version == SCHEMA_VERSION:
            return
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if version < 1:
                    for statement in (
                        """CREATE TABLE memory_entries (
                            entry_id TEXT PRIMARY KEY,
                            content TEXT NOT NULL,
                            content_hash TEXT NOT NULL,
                            category TEXT NOT NULL,
                            source TEXT NOT NULL,
                            created_at REAL NOT NULL,
                            updated_at REAL NOT NULL,
                            revision INTEGER NOT NULL CHECK(revision > 0),
                            UNIQUE(category, content_hash)
                        )""",
                        """CREATE TABLE memory_history (
                            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            entry_id TEXT NOT NULL REFERENCES memory_entries(entry_id)
                                ON DELETE CASCADE,
                            revision INTEGER NOT NULL,
                            action TEXT NOT NULL CHECK(action IN ('create','update')),
                            content TEXT NOT NULL,
                            category TEXT NOT NULL,
                            source TEXT NOT NULL,
                            changed_at REAL NOT NULL,
                            UNIQUE(entry_id, revision)
                        )""",
                        """CREATE VIRTUAL TABLE memory_fts USING fts5(
                            entry_id UNINDEXED, content, tokenize='unicode61 remove_diacritics 2'
                        )""",
                        """CREATE TRIGGER memory_entries_fts_insert AFTER INSERT ON memory_entries
                        BEGIN
                            INSERT INTO memory_fts(entry_id, content)
                            VALUES(new.entry_id, new.content);
                        END""",
                        """CREATE TRIGGER memory_entries_fts_update AFTER UPDATE OF content
                        ON memory_entries
                        BEGIN
                            DELETE FROM memory_fts WHERE entry_id=old.entry_id;
                            INSERT INTO memory_fts(entry_id, content)
                            VALUES(new.entry_id, new.content);
                        END""",
                        """CREATE TRIGGER memory_entries_fts_delete AFTER DELETE ON memory_entries
                        BEGIN
                            DELETE FROM memory_fts WHERE entry_id=old.entry_id;
                        END""",
                        "CREATE INDEX memory_history_entry ON memory_history(entry_id, revision)",
                    ):
                        self.db.execute(statement)
                    self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    self.db.execute("PRAGMA user_version=1")
                    version = 1
                if version < 2:
                    self.db.execute(
                        "CREATE TABLE memory_imports ("
                        "migration_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, "
                        "created_at REAL NOT NULL, created_entries TEXT NOT NULL, "
                        "duplicate_count INTEGER NOT NULL, rolled_back_at REAL)"
                    )
                    self.db.execute(
                        "CREATE INDEX memory_imports_created ON memory_imports(created_at)"
                    )
                    self.db.execute("PRAGMA user_version=2")
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self.db.close()

    @staticmethod
    def _content(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("Memory content must be text.")
        content = value.strip()
        if not content or len(content) > MAX_CONTENT_LENGTH:
            raise ValueError(f"Memory content must contain 1 to {MAX_CONTENT_LENGTH} characters.")
        if any(ord(char) < 32 and char not in "\n\t" for char in content):
            raise ValueError("Memory content contains unsupported control characters.")
        return content

    @staticmethod
    def _category(value: str) -> str:
        if value not in CATEGORIES:
            raise ValueError("Unsupported memory category.")
        return value

    @staticmethod
    def _source(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("Memory source must be text.")
        source = " ".join(value.split())
        if not source or len(source) > 100 or any(ord(char) < 32 for char in source):
            raise ValueError("Memory source must contain 1 to 100 printable characters.")
        return source

    @staticmethod
    def _hash(content: str) -> str:
        normalized = " ".join(content.casefold().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _entry(row: sqlite3.Row) -> dict:
        return {
            "id": row["entry_id"],
            "content": row["content"],
            "category": row["category"],
            "source": row["source"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "revision": row["revision"],
        }

    def create(
        self,
        content: str,
        *,
        category: str = "note",
        source: str = "client",
        private_mode: bool = False,
        now: float | None = None,
    ) -> dict:
        if type(private_mode) is not bool:
            raise ValueError("private_mode must be a boolean.")
        if private_mode:
            return {"stored": False, "private_mode": True}
        text = self._content(content)
        kind = self._category(category)
        origin = self._source(source)
        at = time.time() if now is None else now
        digest = self._hash(text)
        with self._lock, self.db:
            existing = self.db.execute(
                "SELECT * FROM memory_entries WHERE category=? AND content_hash=?",
                (kind, digest),
            ).fetchone()
            if existing is not None:
                return {"stored": True, "created": False, "entry": self._entry(existing)}
            identity = uuid4().hex
            self.db.execute(
                "INSERT INTO memory_entries(entry_id,content,content_hash,category,source,"
                "created_at,updated_at,revision) VALUES(?,?,?,?,?,?,?,1)",
                (identity, text, digest, kind, origin, at, at),
            )
            self.db.execute(
                "INSERT INTO memory_history(entry_id,revision,action,content,category,source,"
                "changed_at) VALUES(?,1,'create',?,?,?,?)",
                (identity, text, kind, origin, at),
            )
            row = self.db.execute(
                "SELECT * FROM memory_entries WHERE entry_id=?", (identity,)
            ).fetchone()
        return {"stored": True, "created": True, "entry": self._entry(row)}

    def get(self, identity: str) -> dict | None:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM memory_entries WHERE entry_id=?", (identity,)
            ).fetchone()
        return self._entry(row) if row else None

    def list(self, *, limit: int = 50) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100.")
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM memory_entries ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._entry(row) for row in rows]

    def count(self) -> int:
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0]

    def import_batch(
        self, migration_id: str, entries: list[dict], *, now: float | None = None
    ) -> dict:
        """Atomically import an explicitly selected, bounded batch from another client."""
        try:
            migration_id = str(UUID(migration_id))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("Invalid memory migration ID.") from None
        if not isinstance(entries, list) or not 1 <= len(entries) <= 50:
            raise ValueError("A memory import must contain 1 to 50 entries.")
        validated = []
        for item in entries:
            if not isinstance(item, dict) or set(item) != {"content", "category", "source"}:
                raise ValueError("Each imported memory needs content, category, and source.")
            validated.append(
                (
                    self._content(item["content"]),
                    self._category(item["category"]),
                    self._source(item["source"]),
                )
            )
        payload = json.dumps(validated, ensure_ascii=False, separators=(",", ":"))
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        at = time.time() if now is None else now
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                previous = self.db.execute(
                    "SELECT payload_hash,created_entries,duplicate_count,rolled_back_at "
                    "FROM memory_imports WHERE migration_id=?",
                    (migration_id,),
                ).fetchone()
                if previous is not None:
                    if previous["payload_hash"] != payload_hash:
                        raise ValueError("This memory migration ID was already used.")
                    self.db.commit()
                    return {
                        "migration_id": migration_id,
                        "created": json.loads(previous["created_entries"]),
                        "duplicates": previous["duplicate_count"],
                        "already_imported": True,
                        "rolled_back": previous["rolled_back_at"] is not None,
                    }

                created = []
                duplicates = 0
                for content, category, origin in validated:
                    digest = self._hash(content)
                    existing = self.db.execute(
                        "SELECT entry_id FROM memory_entries WHERE category=? AND content_hash=?",
                        (category, digest),
                    ).fetchone()
                    if existing is not None:
                        duplicates += 1
                        continue
                    identity = uuid4().hex
                    self.db.execute(
                        "INSERT INTO memory_entries(entry_id,content,content_hash,category,source,"
                        "created_at,updated_at,revision) VALUES(?,?,?,?,?,?,?,1)",
                        (identity, content, digest, category, f"mac_import:{origin}"[:100], at, at),
                    )
                    self.db.execute(
                        "INSERT INTO memory_history(entry_id,revision,action,content,category,"
                        "source,changed_at) VALUES(?,1,'create',?,?,?,?)",
                        (identity, content, category, f"mac_import:{origin}"[:100], at),
                    )
                    created.append({"id": identity, "revision": 1})
                self.db.execute(
                    "INSERT INTO memory_imports(migration_id,payload_hash,created_at,"
                    "created_entries,duplicate_count) VALUES(?,?,?,?,?)",
                    (
                        migration_id,
                        payload_hash,
                        at,
                        json.dumps(created, separators=(",", ":")),
                        duplicates,
                    ),
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return {
            "migration_id": migration_id,
            "created": created,
            "duplicates": duplicates,
            "already_imported": False,
            "rolled_back": False,
        }

    def rollback_import(self, migration_id: str, *, now: float | None = None) -> dict:
        """Remove entries from one import only if they remain unchanged since import."""
        try:
            migration_id = str(UUID(migration_id))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("Invalid memory migration ID.") from None
        at = time.time() if now is None else now
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                imported = self.db.execute(
                    "SELECT created_at,created_entries,rolled_back_at FROM memory_imports "
                    "WHERE migration_id=?",
                    (migration_id,),
                ).fetchone()
                if imported is None:
                    raise ValueError("Memory migration was not found.")
                if imported["rolled_back_at"] is not None:
                    self.db.commit()
                    return {
                        "migration_id": migration_id,
                        "deleted": 0,
                        "preserved_modified": 0,
                        "already_rolled_back": True,
                    }
                deleted = 0
                preserved = 0
                for item in json.loads(imported["created_entries"]):
                    cursor = self.db.execute(
                        "DELETE FROM memory_entries WHERE entry_id=? AND revision=1 "
                        "AND source LIKE 'mac_import:%' AND created_at=?",
                        (item["id"], imported["created_at"]),
                    )
                    if cursor.rowcount:
                        deleted += 1
                    else:
                        preserved += 1
                self.db.execute(
                    "UPDATE memory_imports SET rolled_back_at=? WHERE migration_id=?",
                    (at, migration_id),
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return {
            "migration_id": migration_id,
            "deleted": deleted,
            "preserved_modified": preserved,
            "already_rolled_back": False,
        }

    def search(self, query: str, *, limit: int = 20) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("Search query must contain 1 to 200 characters.")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50.")
        tokens = re.findall(r"[\w-]+", query.casefold(), flags=re.UNICODE)
        if not tokens:
            return []
        match = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens[:12])
        with self._lock:
            rows = self.db.execute(
                "SELECT e.* FROM memory_fts f JOIN memory_entries e USING(entry_id) "
                "WHERE memory_fts MATCH ? ORDER BY bm25(memory_fts), e.updated_at DESC LIMIT ?",
                (match, limit),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def update(
        self,
        identity: str,
        content: str,
        *,
        revision: int,
        category: str | None = None,
        source: str | None = None,
        now: float | None = None,
    ) -> dict:
        text = self._content(content)
        at = time.time() if now is None else now
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT * FROM memory_entries WHERE entry_id=?", (identity,)
            ).fetchone()
            if row is None:
                return None
            if type(revision) is not int or row["revision"] != revision:
                raise MemoryConflict("Memory entry revision is stale.")
            kind = self._category(category or row["category"])
            origin = self._source(source or row["source"])
            next_revision = revision + 1
            try:
                self.db.execute(
                    "UPDATE memory_entries SET content=?,content_hash=?,category=?,source=?,"
                    "updated_at=?,revision=? WHERE entry_id=?",
                    (text, self._hash(text), kind, origin, at, next_revision, identity),
                )
            except sqlite3.IntegrityError:
                raise MemoryConflict("An equivalent memory entry already exists.") from None
            self.db.execute(
                "INSERT INTO memory_history(entry_id,revision,action,content,category,source,"
                "changed_at) VALUES(?,?, 'update',?,?,?,?)",
                (identity, next_revision, text, kind, origin, at),
            )
            updated = self.db.execute(
                "SELECT * FROM memory_entries WHERE entry_id=?", (identity,)
            ).fetchone()
        return self._entry(updated)

    def history(self, identity: str) -> list[dict] | None:
        with self._lock:
            exists = self.db.execute(
                "SELECT 1 FROM memory_entries WHERE entry_id=?", (identity,)
            ).fetchone()
            if exists is None:
                return None
            rows = self.db.execute(
                "SELECT revision,action,content,category,source,changed_at "
                "FROM memory_history WHERE entry_id=? ORDER BY revision",
                (identity,),
            ).fetchall()
        return [dict(row) for row in rows]

    def forget(self, identity: str) -> bool:
        """Permanently remove an entry and its revision history from this store."""
        with self._lock, self.db:
            cursor = self.db.execute("DELETE FROM memory_entries WHERE entry_id=?", (identity,))
        return cursor.rowcount > 0

    def backup_to(self, destination: str | Path) -> Path:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.path != ":memory:" and target.resolve() == self.path.resolve():
            raise ValueError("Backup destination must differ from the active database.")
        temporary = target.with_name(target.name + ".tmp-" + uuid4().hex)
        try:
            with self._lock:
                backup_db = sqlite3.connect(str(temporary))
                try:
                    self.db.backup(backup_db)
                    backup_db.commit()
                finally:
                    backup_db.close()
            self.verify_backup(temporary)
            # Same-directory hard-link creation is atomic and never overwrites an
            # existing backup, including one created concurrently.
            os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    @staticmethod
    def verify_backup(source: str | Path) -> dict:
        uri = f"file:{Path(source).resolve().as_posix()}?mode=ro"
        backup = sqlite3.connect(uri, uri=True)
        try:
            check = backup.execute("PRAGMA quick_check").fetchone()[0]
            version = backup.execute("PRAGMA user_version").fetchone()[0]
            app_id = backup.execute("PRAGMA application_id").fetchone()[0]
            if check != "ok" or version != SCHEMA_VERSION or app_id != APPLICATION_ID:
                raise ValueError("Memory backup failed validation.")
            return {"valid": True, "schema_version": version}
        finally:
            backup.close()

    @classmethod
    def restore_to_empty(cls, source: str | Path, destination: str | Path) -> MemoryStore:
        """Restore a verified backup only when the destination does not yet exist."""
        cls.verify_backup(source)
        target = Path(destination)
        if target.exists() or target.is_symlink():
            raise FileExistsError("Restore destination already exists; refusing to overwrite it.")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            shutil.copyfile(source, target)
            cls.verify_backup(target)
            return cls(target)
        except Exception:
            target.unlink(missing_ok=True)
            raise
