"""Public identities, selective permissions and replay-safe command reservations."""

import json
import re
import time

from backend.center import rows, text

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_peers (id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
public_key TEXT NOT NULL, approved_at REAL NOT NULL, revoked_at REAL, disconnected INTEGER NOT NULL DEFAULT 0,
last_seen REAL, last_sync REAL, categories TEXT NOT NULL DEFAULT '[]', capabilities TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS sync_nonces (peer_id TEXT NOT NULL, nonce TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(peer_id,nonce));
CREATE TABLE IF NOT EXISTS sync_commands (peer_id TEXT NOT NULL, command_id TEXT NOT NULL, operation TEXT NOT NULL,
created_at REAL NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL, result TEXT, PRIMARY KEY(peer_id,command_id));
CREATE TABLE IF NOT EXISTS sync_conflicts (id INTEGER PRIMARY KEY, peer_id TEXT NOT NULL, memory_id INTEGER NOT NULL,
revision INTEGER NOT NULL, content TEXT NOT NULL, at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
"""
CATEGORIES = {
    "memory": "Подтверждённая общая память",
    "preferences": "Обычные предпочтения",
    "scenarios": "Каталог сценариев",
    "devices": "Список разрешённых устройств",
    "history": "Разговоры этого устройства",
    "media_progress": "Прогресс медиатеки",
}
CAPABILITIES = {"chat", "scenario", "devices", "open_app"}


class SyncStore:
    def __init__(self, memory):
        self.memory, self.db = memory, memory.db
        self.db.executescript(SCHEMA)

    def peer(self, identity, *, connected=True):
        row = self.db.execute("SELECT * FROM sync_peers WHERE id=?", (identity,)).fetchone()
        if not row or row["revoked_at"] or (connected and row["disconnected"]):
            raise ValueError("Доверие отозвано или устройство отключено.")
        return {
            **dict(row),
            "public_key": json.loads(row["public_key"]),
            "categories": json.loads(row["categories"]),
            "capabilities": json.loads(row["capabilities"]),
        }

    def trust(self, pending):
        with self.memory.memory_transaction():
            self.db.execute(
                "INSERT INTO sync_peers(id,name,kind,public_key,approved_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,public_key=excluded.public_key,"
                "approved_at=excluded.approved_at,revoked_at=NULL,disconnected=0,categories='[]',capabilities='[]'",
                (
                    pending["id"],
                    text(pending["name"], 80),
                    pending["kind"],
                    json.dumps(pending["key"]),
                    time.time(),
                ),
            )

    def nonce(self, peer, nonce):
        if not isinstance(nonce, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", nonce):
            raise ValueError("Некорректный nonce.")
        with self.memory.memory_transaction():
            self.db.execute("DELETE FROM sync_nonces WHERE at<?", (time.time() - 120,))
            try:
                self.db.execute("INSERT INTO sync_nonces VALUES(?,?,?)", (peer, nonce, time.time()))
            except __import__("sqlite3").IntegrityError:
                raise ValueError("Повторный запрос отклонён.") from None

    def reserve(self, peer, command, operation, expires):
        if not isinstance(command, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", command):
            raise ValueError("Нужен idempotency key.")
        now = time.time()
        if type(expires) not in (int, float) or not now < expires <= now + 120:
            raise ValueError("Команда устарела или имеет слишком большой срок действия.")
        with self.memory.memory_transaction():
            previous = self.db.execute(
                "SELECT * FROM sync_commands WHERE peer_id=? AND command_id=?", (peer, command)
            ).fetchone()
            if previous:
                if previous["operation"] != operation:
                    raise ValueError("ID уже использован другой командой.")
                return dict(previous)
            self.db.execute(
                "INSERT INTO sync_commands VALUES(?,?,?,?,?,'running',NULL)",
                (peer, command, operation, now, expires),
            )
            self.db.execute(
                "DELETE FROM sync_commands WHERE created_at<? AND status!='running'",
                (now - 30 * 86400,),
            )
        return None

    def finish(self, peer, command, status, result):
        with self.memory.memory_transaction():
            self.db.execute(
                "UPDATE sync_commands SET status=?,result=? WHERE peer_id=? AND command_id=?",
                (status, json.dumps(result, ensure_ascii=False), peer, command),
            )

    def peers(self):
        result = rows(
            self.db,
            "SELECT id,name,kind,approved_at,revoked_at,disconnected,last_seen,last_sync,categories,capabilities FROM sync_peers ORDER BY approved_at DESC LIMIT 100",
        )
        for row in result:
            row["categories"], row["capabilities"] = (
                json.loads(row["categories"]),
                json.loads(row["capabilities"]),
            )
            row["online"] = (
                not row["revoked_at"]
                and not row["disconnected"]
                and (row["last_seen"] or 0) > time.time() - 20
            )
        return result
