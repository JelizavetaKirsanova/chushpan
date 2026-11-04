"""Conversation history and long-term facts, stored in SQLite."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.brain.privacy import contains_secret, redact
from backend.context import RequestContext

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    tool_calls      TEXT,  -- JSON, on an assistant message that called tools
    tool_name       TEXT   -- on a tool message
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS dialogue_sessions (
    session_id TEXT PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id)
);
CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(namespace, key)
);
CREATE TABLE IF NOT EXISTS dashboard_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('note','goal','task')),
    content TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class MemoryStore:
    def __init__(self, path: Path | str) -> None:
        self.memory_available = True
        self.conversation_locks = {}
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self._migrate()
        from backend.memory.library import migrate

        migrate(self)
        from backend.performance import PerformanceHistory

        self.performance = PerformanceHistory(self)

    @classmethod
    def open_resilient(cls, path: Path | str) -> MemoryStore:
        try:
            return cls(path)
        except sqlite3.Error:
            # Same schema/API, session history only. Never claim durable writes succeeded.
            store = cls(":memory:")
            store.memory_available = False
            return store

    def _require_memory(self) -> None:
        if not self.memory_available:
            raise sqlite3.OperationalError("Долговременная память недоступна.")

    @contextmanager
    def memory_transaction(self):
        if self.db.in_transaction:
            yield
        else:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                yield

    def invalidate_memory_context(self) -> None:
        floor = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
        self.db.execute(
            "INSERT INTO preferences VALUES (?, ?, ?, ?) ON CONFLICT(namespace, key) "
            "DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("memory", "context_floor", json.dumps(floor), _now()),
        )

    def _migrate(self) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            conversation_cols = {
                r["name"] for r in self.db.execute("PRAGMA table_info(conversations)")
            }
            for name, definition in {
                "title": "TEXT NOT NULL DEFAULT ''",
                "pinned": "INTEGER NOT NULL DEFAULT 0",
                "deleted_at": "TEXT",
            }.items():
                if name not in conversation_cols:
                    self.db.execute(f"ALTER TABLE conversations ADD COLUMN {name} {definition}")
            self.db.create_function(
                "casefold", 1, lambda value: str(value or "").casefold(), deterministic=True
            )
            cols = {r["name"] for r in self.db.execute("PRAGMA table_info(messages)")}
            for col in (
                "tool_calls",
                "tool_name",
                "device_id",
                "device_type",
                "session_id",
                "request_id",
            ):
                if col not in cols:  # databases created before tool calls were persisted
                    self.db.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
            fields = {r["name"] for r in self.db.execute("PRAGMA table_info(facts)")}
            additions = {
                "category": "TEXT NOT NULL DEFAULT 'profile'",
                "source": "TEXT NOT NULL DEFAULT 'legacy_unknown'",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
                "confidence": "REAL NOT NULL DEFAULT 1.0",
                "status": "TEXT NOT NULL DEFAULT 'active'",
                "sensitive": "INTEGER NOT NULL DEFAULT 0",
                "semantic_key": "TEXT NOT NULL DEFAULT ''",
                "revision": "INTEGER NOT NULL DEFAULT 1",
            }
            for name, declaration in additions.items():
                if name not in fields:
                    self.db.execute(f"ALTER TABLE facts ADD COLUMN {name} {declaration}")
            self.db.execute("UPDATE facts SET updated_at=created_at WHERE updated_at=''")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS memory_suppression "
                "(digest TEXT PRIMARY KEY, created_at TEXT NOT NULL)"
            )

    def close(self) -> None:
        self.performance.close()
        self.db.close()

    def preference(self, namespace: str, key: str) -> Any:
        row = self.db.execute(
            "SELECT value FROM preferences WHERE namespace = ? AND key = ?", (namespace, key)
        ).fetchone()
        return json.loads(row["value"]) if row else None

    def set_preference(self, namespace: str, key: str, value: Any) -> None:
        self._require_memory()
        with self.memory_transaction():
            self.db.execute(
                "INSERT INTO preferences(namespace, key, value, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(namespace, key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (namespace, key, json.dumps(value, ensure_ascii=False), _now()),
            )

    def claim_operation(self, namespace: str, token: str, now: float, ttl: float = 600) -> bool:
        self._require_memory()
        with self.memory_transaction():
            lease = self.preference(namespace, "operation_lease")
            if lease and lease.get("until", 0) > now:
                return False
            self.db.execute(
                "INSERT INTO preferences VALUES (?, ?, ?, ?) "
                "ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (
                    namespace,
                    "operation_lease",
                    json.dumps({"token": token, "until": now + ttl}),
                    _now(),
                ),
            )
        return True

    def release_operation(self, namespace: str, token: str) -> None:
        with self.memory_transaction():
            lease = self.preference(namespace, "operation_lease")
            if lease and lease.get("token") == token:
                self.db.execute(
                    "DELETE FROM preferences WHERE namespace=? AND key=?",
                    (namespace, "operation_lease"),
                )

    def new_conversation(self) -> int:
        with self.db:
            cur = self.db.execute("INSERT INTO conversations (started_at) VALUES (?)", (_now(),))
        return int(cur.lastrowid)

    def desktop_session(self) -> str:
        """Issued locally, never accepted from an untrusted HTTP/WebSocket payload."""
        if not self.memory_available:
            return uuid4().hex
        with self.memory_transaction():
            session = self.preference("dialogue", "desktop_session")
            if not isinstance(session, str) or not session:
                session = uuid4().hex
                self.set_preference("dialogue", "desktop_session", session)
        return session

    def session_conversation(self, session_id: str, conversation_id: int | None = None) -> int:
        if conversation_id is None:
            row = self.db.execute(
                "SELECT conversation_id FROM dialogue_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row:
                return row[0]
        with self.memory_transaction():
            row = self.db.execute(
                "SELECT conversation_id FROM dialogue_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if conversation_id is None:
                if row:
                    return row[0]
                conversation_id = self.db.execute(
                    "INSERT INTO conversations(started_at) VALUES (?)", (_now(),)
                ).lastrowid
            self.db.execute(
                "INSERT INTO dialogue_sessions VALUES (?, ?) ON CONFLICT(session_id) "
                "DO UPDATE SET conversation_id=excluded.conversation_id",
                (session_id, conversation_id),
            )
        return int(conversation_id)

    def desktop_conversation(self) -> int:
        session = self.desktop_session()
        with self.memory_transaction():
            row = self.db.execute(
                "SELECT conversation_id FROM dialogue_sessions WHERE session_id=?", (session,)
            ).fetchone()
            # One-time upgrade: resume the existing transcript instead of discarding it.
            return row[0] if row else self.session_conversation(session, self.last_conversation())

    def conversation_lock(self, conversation_id: int):
        return self.conversation_locks.setdefault(conversation_id, asyncio.Lock())

    def clear_preference(self, namespace: str, key: str) -> None:
        with self.memory_transaction():
            self.db.execute("DELETE FROM preferences WHERE namespace=? AND key=?", (namespace, key))

    def claim_followup(self, conversation_id: int, proposal: dict) -> bool:
        with self.memory_transaction():
            current = self.preference("dialogue_pending", str(conversation_id))
            if current != proposal or current.get("state") == "executing":
                return False
            self.db.execute(
                "DELETE FROM preferences WHERE namespace=? AND key=?",
                ("dialogue_pending", str(conversation_id)),
            )
        return True

    def last_conversation(self) -> int | None:
        row = self.db.execute("SELECT MAX(id) AS id FROM conversations").fetchone()
        return row["id"]

    def add(
        self,
        conversation_id: int,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_name: str | None = None,
        *,
        source: RequestContext | None = None,
    ) -> None:
        from backend.latency import current

        profile = current.get()
        with self.memory_transaction():
            self.db.execute(
                "INSERT INTO messages "
                "(conversation_id, role, content, created_at, tool_calls, tool_name, "
                "device_id, device_type, session_id, request_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    role,
                    redact(content),
                    _now(),
                    json.dumps(redact(tool_calls), ensure_ascii=False) if tool_calls else None,
                    tool_name,
                    source.device.device_id if source else None,
                    source.device.device_type if source else None,
                    source.device.session_id if source else None,
                    source.request_id
                    if source
                    else profile.request_id
                    if profile and profile.repository
                    else None,
                ),
            )

    def recent(self, conversation_id: int, limit: int) -> list[dict[str, Any]]:
        floor = self.preference("memory", "context_floor") or 0
        rows = self.db.execute(
            "SELECT role, content, tool_calls, tool_name FROM messages "
            "WHERE conversation_id = ? AND id > ? ORDER BY id DESC LIMIT ?",
            (conversation_id, floor, limit),
        ).fetchall()
        messages: list[dict[str, Any]] = []
        for r in reversed(rows):
            msg: dict[str, Any] = {"role": r["role"], "content": r["content"]}
            if r["tool_calls"]:
                msg["tool_calls"] = json.loads(r["tool_calls"])
            if r["tool_name"]:
                msg["tool_name"] = r["tool_name"]
            messages.append(msg)
        # The limit may cut a turn after its question: a call or result without it.
        while messages and (messages[0]["role"] == "tool" or "tool_calls" in messages[0]):
            messages.pop(0)
        return messages

    def transcript(self, conversation_id: int) -> list[dict[str, str]]:
        """What was said, in order: questions and spoken answers, without tool traffic."""
        rows = self.db.execute(
            "SELECT role, content, created_at FROM messages WHERE conversation_id = ? "
            "AND role IN ('user', 'assistant') AND tool_calls IS NULL AND content != '' "
            "ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def conversation_list(self, query: str = "", *, trash: bool = False) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT c.id, c.started_at, c.title, c.pinned, c.deleted_at, "
                "COALESCE((SELECT substr(x.content,1,100) FROM messages x "
                "WHERE x.conversation_id=c.id "
                "AND x.role='user' ORDER BY x.id LIMIT 1),'') AS preview, "
                "COUNT(m.id) AS messages FROM conversations c "
                "LEFT JOIN messages m ON m.conversation_id=c.id AND m.role IN ('user','assistant') "
                "AND m.tool_calls IS NULL AND m.content != '' "
                "WHERE (c.deleted_at IS NOT NULL)=? "
                "AND (?='' OR instr(casefold(c.title),casefold(?))>0 "
                "OR EXISTS(SELECT 1 FROM messages s WHERE s.conversation_id=c.id "
                "AND s.role IN ('user','assistant') AND s.tool_calls IS NULL "
                "AND instr(casefold(s.content),casefold(?))>0)) GROUP BY c.id "
                "ORDER BY c.pinned DESC,c.id DESC LIMIT 100",
                (int(trash), query, query, query),
            ).fetchall()
        ]

    def conversation_page(self, conversation_id: int) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT id, role, content, created_at, request_id, "
                "EXISTS(SELECT 1 FROM request_traces t WHERE t.request_id=page.request_id) "
                "AS has_trace FROM ("
                "SELECT id, role, content, created_at, request_id "
                "FROM messages WHERE conversation_id=? AND role IN ('user','assistant') "
                "AND tool_calls IS NULL AND content != '' ORDER BY id DESC LIMIT 200) "
                "AS page ORDER BY id",
                (conversation_id,),
            ).fetchall()
        ]

    # Long-term memory: facts kept across conversations ("remember that...")

    def add_fact(self, content: str) -> tuple[int, bool]:
        """Store a fact; returns its id and whether it is new (False: already known)."""
        self._require_memory()
        content = " ".join(content.split())
        if contains_secret(content):
            raise ValueError("Секреты нельзя записывать в память.")
        for fact_id, known in self.facts():
            if known.casefold() == content.casefold():
                return fact_id, False
        with self.db:
            cur = self.db.execute(
                "INSERT INTO facts (content, created_at, updated_at) VALUES (?, ?, ?)",
                (content, _now(), _now()),
            )
        return int(cur.lastrowid), True

    def facts(self) -> list[tuple[int, str]]:
        rows = self.db.execute(
            "SELECT id, content FROM facts WHERE status='active' AND project_id IS NULL "
            "AND (expires_at IS NULL OR expires_at>?) ORDER BY id",
            (_now(),),
        ).fetchall()
        return [(r["id"], r["content"]) for r in rows]

    def forget_fact(self, fact_id: int, *, expected_revision: int | None = None) -> bool:
        self._require_memory()
        with self.memory_transaction():
            row = self.db.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
            if row and expected_revision is not None and row["revision"] != expected_revision:
                return False
            if row:
                self._suppress(row["content"], row["semantic_key"])
            cur = self.db.execute(
                "DELETE FROM facts WHERE id=? AND revision=?",
                (fact_id, row["revision"] if row else -1),
            )
            if row:
                self.invalidate_memory_context()
            return cur.rowcount > 0

    def replace_fact(self, fact_id: int, content: str) -> tuple[int, bool]:
        """Deletion and deduplicated insertion succeed or roll back together."""
        self._require_memory()
        content = " ".join(content.split())
        if contains_secret(content):
            raise ValueError("Секреты нельзя записывать в память.")
        with self.memory_transaction():
            if not self.db.execute("SELECT 1 FROM facts WHERE id=?", (fact_id,)).fetchone():
                raise ValueError("no memory with id")
            old = self.db.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
            self._suppress(old["content"])
            self.db.execute("DELETE FROM facts WHERE id=?", (fact_id,))
            self.invalidate_memory_context()
            for row in self.db.execute(
                "SELECT id, content FROM facts WHERE project_id IS ?", (old["project_id"],)
            ):
                if row["content"].casefold() == content.casefold():
                    return row["id"], False
            cursor = self.db.execute(
                "INSERT INTO facts(content, created_at, updated_at, category, source, "
                "confidence, sensitive, semantic_key, revision, project_id, expires_at, confirmed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    content,
                    _now(),
                    _now(),
                    old["category"],
                    old["source"],
                    old["confidence"],
                    old["sensitive"],
                    old["semantic_key"],
                    old["revision"] + 1,
                    old["project_id"],
                    old["expires_at"],
                    old["confirmed"],
                ),
            )
            return int(cursor.lastrowid), True

    def forget_all_facts(self) -> int:
        self._require_memory()
        with self.db:
            for row in self.db.execute("SELECT * FROM facts").fetchall():
                self._suppress(row["content"], row["semantic_key"])
            cur = self.db.execute("DELETE FROM facts")
            self.invalidate_memory_context()
        return cur.rowcount

    @staticmethod
    def memory_digest(value: str) -> str:
        return hashlib.sha256(" ".join(value.casefold().split()).encode()).hexdigest()

    def _suppress(self, content: str, key: str = "") -> None:
        for value in (content, "slot:" + key if key else content):
            self.db.execute(
                "INSERT OR IGNORE INTO memory_suppression VALUES (?, ?)",
                (self.memory_digest(value), _now()),
            )

    def memories(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM facts ORDER BY id")]

    def save_memory(
        self,
        content: str,
        *,
        category: str = "profile",
        key: str = "",
        source: str = "explicit_user",
        confidence: float = 1.0,
        sensitive: bool = False,
        automatic: bool = False,
        expected_revision: int | None = None,
        project_id: int | None = None,
    ) -> tuple[int, bool]:
        """Extend the existing fact table; slot updates and deduplication are atomic."""
        self._require_memory()
        if category not in {
            "profile",
            "preference",
            "habit",
            "project",
            "episode",
            "correction",
            "personality",
        }:
            raise ValueError("Неизвестная категория памяти.")
        content = " ".join(content.split())
        if not content or len(content) > 300 or contains_secret(content):
            raise ValueError("Пустая, слишком длинная запись или секрет: память не изменена.")
        if not 0 <= confidence <= 1 or (sensitive and automatic):
            raise ValueError("Недопустимая уверенность или автоматическая чувствительная запись.")
        with self.memory_transaction():
            if automatic:
                values = (content, "slot:" + key) if key else (content,)
                if any(
                    self.db.execute(
                        "SELECT 1 FROM memory_suppression WHERE digest=?", (self.memory_digest(v),)
                    ).fetchone()
                    for v in values
                ):
                    return 0, False
            if not automatic:
                self.db.execute(
                    "DELETE FROM memory_suppression WHERE digest IN (?, ?)",
                    (self.memory_digest(content), self.memory_digest("slot:" + key)),
                )
            old = (
                self.db.execute(
                    "SELECT * FROM facts WHERE semantic_key=? AND project_id IS ?",
                    (key, project_id),
                ).fetchone()
                if key
                else None
            )
            if old and expected_revision is not None and old["revision"] != expected_revision:
                raise ValueError("Воспоминание уже изменилось; прочитай актуальную версию.")
            if old and old["content"].casefold() != content.casefold():
                self._suppress(old["content"])
                self.invalidate_memory_context()
                self.db.execute(
                    "UPDATE facts SET content=?, category=?, source=?, updated_at=?, "
                    "confidence=?, sensitive=?, status='active', revision=revision+1 "
                    "WHERE id=?",
                    (content, category, source, _now(), confidence, int(sensitive), old["id"]),
                )
                return old["id"], False
            duplicate = next(
                (
                    r
                    for r in self.db.execute(
                        "SELECT id, content FROM facts WHERE project_id IS ?", (project_id,)
                    )
                    if r["content"].casefold() == content.casefold()
                ),
                None,
            )
            new = duplicate is None
            if duplicate:
                fact_id = duplicate["id"]
            else:
                fact_id = self.db.execute(
                    "INSERT INTO facts(content, created_at, updated_at, project_id) "
                    "VALUES (?, ?, ?, ?)",
                    (content, _now(), _now(), project_id),
                ).lastrowid
            if new or key:
                self.db.execute(
                    "UPDATE facts SET category=?, source=?, confidence=?, sensitive=?, "
                    "semantic_key=?, status='active' WHERE id=?",
                    (category, source, confidence, int(sensitive), key, fact_id),
                )
            return fact_id, new

    def count(self, conversation_id: int) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        return int(row["n"])
