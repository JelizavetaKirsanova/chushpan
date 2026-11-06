"""Dependent indexes and provenance for the existing SQLite memory, never a second store."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from time import perf_counter

from backend.brain.privacy import contains_secret, redact
from backend.memory.store import _now

CATEGORIES = {
    "people": "Люди",
    "study": "Учёба",
    "work": "Работа",
    "device": "Устройства",
    "home": "Дом и сценарии",
    "entertainment": "Развлечения",
    "conversation": "Разговоры",
    "temporary": "Временная память",
}

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS memory_versions (id INTEGER PRIMARY KEY, memory_id INTEGER "
    "NOT NULL REFERENCES facts(id) ON DELETE CASCADE, revision INTEGER NOT NULL, content TEXT "
    "NOT NULL, source TEXT NOT NULL, changed_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS memory_evidence (memory_id INTEGER NOT NULL REFERENCES "
    "facts(id) ON DELETE CASCADE, message_id INTEGER NOT NULL REFERENCES messages(id) ON "
    "DELETE CASCADE, PRIMARY KEY(memory_id,message_id))",
    "CREATE TABLE IF NOT EXISTS memory_entities (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
    "normalized TEXT NOT NULL, scope TEXT NOT NULL, UNIQUE(normalized,scope))",
    "CREATE TABLE IF NOT EXISTS memory_entity_links (memory_id INTEGER NOT NULL REFERENCES "
    "facts(id) ON DELETE CASCADE, entity_id INTEGER NOT NULL REFERENCES memory_entities(id) "
    "ON DELETE CASCADE, PRIMARY KEY(memory_id,entity_id))",
    "CREATE TABLE IF NOT EXISTS conversation_summaries (conversation_id INTEGER PRIMARY KEY "
    "REFERENCES conversations(id) ON DELETE CASCADE, content TEXT NOT NULL, through_id INTEGER "
    "NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS memory_actions (id INTEGER PRIMARY KEY, conversation_id INTEGER "
    "NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, message_id INTEGER REFERENCES "
    "messages(id) ON DELETE CASCADE, request_id TEXT NOT NULL, intent TEXT NOT NULL, tool TEXT "
    "NOT NULL, parameters TEXT NOT NULL, result TEXT NOT NULL, state TEXT NOT NULL, error TEXT, "
    "correction TEXT, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS memory_evidence_message ON memory_evidence(message_id)",
    (
        "CREATE INDEX IF NOT EXISTS memory_entities_reverse ON "
        "memory_entity_links(entity_id,memory_id)"
    ),
    "CREATE INDEX IF NOT EXISTS memory_action_conversation ON memory_actions(conversation_id,id)",
    "CREATE INDEX IF NOT EXISTS memory_fact_category ON facts(category,status,updated_at)",
    "CREATE TABLE IF NOT EXISTS memory_metrics (id INTEGER PRIMARY KEY, stage TEXT NOT NULL, "
    "duration_ms REAL NOT NULL, count INTEGER NOT NULL, created_at TEXT NOT NULL)",
)


def migrate(store):
    db = store.db
    db.execute("PRAGMA secure_delete=ON")
    with store.memory_transaction():
        for table, name, declaration in (
            ("facts", "confirmed_at", "TEXT"),
            ("conversations", "memory_disabled", "INTEGER NOT NULL DEFAULT 0"),
            ("messages", "memory_blocked", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        db.execute(
            "CREATE INDEX IF NOT EXISTS memory_content ON facts(project_id,casefold(content))"
        )
        db.execute("CREATE INDEX IF NOT EXISTS memory_slot ON facts(semantic_key,project_id)")
        db.execute("CREATE INDEX IF NOT EXISTS memory_fact_source ON facts(source)")
        for statement in SCHEMA:
            db.execute(statement)
        db.execute(
            "CREATE TRIGGER IF NOT EXISTS memory_version_update BEFORE UPDATE OF content ON "
            "facts WHEN old.content!=new.content BEGIN INSERT INTO memory_versions"
            "(memory_id,revision,content,source,changed_at) VALUES "
            "(old.id,old.revision,old.content,old.source,new.updated_at); END"
        )
        db.execute(
            "CREATE TRIGGER IF NOT EXISTS memory_entity_update AFTER UPDATE OF content ON facts "
            "WHEN old.content!=new.content BEGIN DELETE FROM memory_entity_links WHERE "
            "memory_id=new.id; DELETE FROM memory_relations WHERE memory_id=new.id OR "
            "related_id=new.id; END"
        )
        existed = db.execute("SELECT 1 FROM sqlite_master WHERE name='history_fts'").fetchone()
        try:
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(content, "
                "content='messages',content_rowid='id',tokenize='unicode61')"
            )
            # Blocked source messages stay in the user's transcript, but leave the derived FTS.
            for suffix in ("insert", "delete", "update", "update_delete", "update_insert"):
                db.execute(f"DROP TRIGGER IF EXISTS history_fts_{suffix}")
            for suffix, event, condition, body in (
                (
                    "insert",
                    "INSERT",
                    "new.memory_blocked=0",
                    "INSERT INTO history_fts(rowid,content) VALUES(new.id,new.content);",
                ),
                (
                    "delete",
                    "DELETE",
                    "old.memory_blocked=0",
                    (
                        "INSERT INTO history_fts(history_fts,rowid,content) "
                        "VALUES('delete',old.id,old.content);"
                    ),
                ),
                (
                    "update_delete",
                    "UPDATE",
                    "old.memory_blocked=0",
                    (
                        "INSERT INTO history_fts(history_fts,rowid,content) "
                        "VALUES('delete',old.id,old.content);"
                    ),
                ),
                (
                    "update_insert",
                    "UPDATE",
                    "new.memory_blocked=0",
                    "INSERT INTO history_fts(rowid,content) VALUES(new.id,new.content);",
                ),
            ):
                db.execute(
                    f"CREATE TRIGGER history_fts_{suffix} AFTER {event} ON messages "
                    f"WHEN {condition} BEGIN {body} END"
                )
            if not existed:
                db.execute(
                    "INSERT INTO history_fts(rowid,content) SELECT id,content "
                    "FROM messages WHERE memory_blocked=0"
                )
            store.history_fts = True
        except sqlite3.OperationalError:
            store.history_fts = False


def metric(store, stage, start, count):
    with store.memory_transaction():
        store.db.execute(
            "INSERT INTO memory_metrics(stage,duration_ms,count,created_at) VALUES(?,?,?,?)",
            (stage, (perf_counter() - start) * 1000, count, _now()),
        )
        store.db.execute(
            "DELETE FROM memory_metrics WHERE id <= "
            "(SELECT COALESCE(MAX(id),0)-1000 FROM memory_metrics)"
        )


def link_entities(store, memory_id, names):
    row = store.db.execute("SELECT project_id FROM facts WHERE id=?", (memory_id,)).fetchone()
    if not row:
        return
    with store.memory_transaction():
        for name in names[:12]:
            name = " ".join(str(name).split())[:80]
            from backend.memory.profile import _SENSITIVE

            if not name or contains_secret(name) or _SENSITIVE.search(name):
                continue
            store.db.execute(
                "INSERT OR IGNORE INTO memory_entities(name,normalized,scope) VALUES(?,?,?)",
                (name, name.casefold().replace("ё", "е"), str(row[0])),
            )
            entity = store.db.execute(
                "SELECT id FROM memory_entities WHERE normalized=? AND scope=?",
                (name.casefold().replace("ё", "е"), str(row[0])),
            ).fetchone()[0]
            store.db.execute(
                "INSERT OR IGNORE INTO memory_entity_links VALUES(?,?)", (memory_id, entity)
            )


def infer_entities(text):
    # Named tokens only. Relations are evidence of co-mention, not invented causality.
    return re.findall(
        r"\b(?:[A-ZА-ЯЁ][a-zа-яё][\w.-]+|[A-Za-z][\w.-]*[A-Z][\w.-]*|BLE|LED|WLED)\b", text
    )[:8]


def evidence(store, fact_id, message_id):
    if not fact_id or not message_id:
        return
    with store.memory_transaction():
        store.db.execute("INSERT OR IGNORE INTO memory_evidence VALUES(?,?)", (fact_id, message_id))
        fact = store.db.execute("SELECT content FROM facts WHERE id=?", (fact_id,)).fetchone()
        link_entities(store, fact_id, infer_entities(fact[0]))


def suppressed(store, content, key=""):
    values = (content, "slot:" + key) if key else (content,)
    return any(
        store.db.execute(
            "SELECT 1 FROM memory_suppression WHERE digest=?", (store.memory_digest(v),)
        ).fetchone()
        for v in values
    )


def forget_derivatives(store, fact):
    """Invalidate source retrieval, pending proposals and summaries before CASCADE deletion."""
    db = store.db
    ids = [
        r[0]
        for r in db.execute(
            "SELECT message_id FROM memory_evidence WHERE memory_id=?", (fact["id"],)
        )
    ]
    for mid in ids:
        db.execute("UPDATE messages SET memory_blocked=1 WHERE id=?", (mid,))
    # Include unlinked legacy source text; only retrieval is blocked, not the transcript.
    for content in [
        fact["content"],
        *[
            r[0]
            for r in db.execute(
                "SELECT content FROM memory_versions WHERE memory_id=?", (fact["id"],)
            )
        ],
    ]:
        db.execute(
            "UPDATE messages SET memory_blocked=1 WHERE memory_blocked=0 "
            "AND instr(casefold(content),casefold(?))>0",
            (content,),
        )
    # Summary snippets can paraphrase a fact. Drop all summaries rather than leave a derivative.
    db.execute("DELETE FROM conversation_summaries")
    pending = store.preference("memory", "pending") or []
    store.set_preference(
        "memory",
        "pending",
        [
            p
            for p in pending
            if p.get("key") != fact["semantic_key"]
            and p["content"].casefold() != fact["content"].casefold()
        ],
    )
    db.execute(
        "DELETE FROM memory_actions WHERE message_id IN "
        "(SELECT message_id FROM memory_evidence WHERE memory_id=?)",
        (fact["id"],),
    )


def summary(store, conversation_id):
    """Bounded extractive recap. Only literal user snippets, explicitly attributed."""
    from backend.settings_models import MemoryPolicy

    policy = MemoryPolicy.model_validate(store.preference("settings", "memory") or {})
    if policy.automatic == "off" or "conversation" in policy.excluded:
        return
    db = store.db
    row = db.execute(
        "SELECT memory_disabled,deleted_at FROM conversations WHERE id=?", (conversation_id,)
    ).fetchone()
    if not row or row["memory_disabled"] or row["deleted_at"]:
        return
    from backend.memory.profile import _SENSITIVE

    floor = store.preference("memory", "context_floor") or 0
    messages = db.execute(
        "SELECT id,content FROM messages WHERE conversation_id=? AND id>? "
        "AND role='user' AND memory_blocked=0 ORDER BY id DESC LIMIT 80",
        (conversation_id, floor),
    ).fetchall()
    if len(messages) < 12:
        return
    safe = [
        r
        for r in messages
        if not _SENSITIVE.search(r["content"])
        and not contains_secret(r["content"])
        and not suppressed(store, r["content"])
    ]
    content = "\n".join("Пользователь: " + r["content"][:180] for r in reversed(safe[:6]))[:1400]
    with store.memory_transaction():
        db.execute(
            "INSERT INTO conversation_summaries VALUES(?,?,?,?) ON CONFLICT"
            "(conversation_id) DO UPDATE SET content=excluded.content,"
            "through_id=excluded.through_id,updated_at=excluded.updated_at",
            (conversation_id, content, messages[0]["id"], _now()),
        )


def history(store, query="", *, project=None, device="local-mac", day=None, limit=40):
    from backend.memory.profile import tokens

    terms = sorted(tokens(query))[:12]
    args = [project, store.preference("memory", "context_floor") or 0, device, device]
    where = [
        "c.deleted_at IS NULL",
        "c.memory_disabled=0",
        "p.project_id IS ?",
        "m.id>?",
        "m.memory_blocked=0",
        "m.role IN ('user','assistant')",
        "m.tool_calls IS NULL",
        "(?='local-mac' OR m.device_id=?)",
    ]
    join = ""
    if terms:
        if store.history_fts:
            join = " JOIN history_fts ON history_fts.rowid=m.id"
            where.append("history_fts MATCH ?")
            args.append(" OR ".join('"' + term + '"*' for term in terms))
        else:
            where.append("(" + " OR ".join("casefold(m.content) LIKE ?" for _ in terms) + ")")
            args.extend("%" + w + "%" for w in terms)
    if day:
        where.append("m.created_at>=? AND m.created_at<?")
        args.extend(day)
    records = store.db.execute(
        "SELECT m.id,m.conversation_id,m.role,m.content,m.created_at "
        "FROM messages m JOIN conversations c ON c.id=m.conversation_id "
        "LEFT JOIN memory_project_conversations p ON p.conversation_id=c.id"
        + join
        + " WHERE "
        + " AND ".join(where)
        + " ORDER BY m.id DESC LIMIT ?",
        (*args, min(100, max(1, limit))),
    ).fetchall()
    from backend.memory.profile import _SENSITIVE

    return [
        dict(r)
        for r in records
        if not _SENSITIVE.search(r["content"])
        and not contains_secret(r["content"])
        and not suppressed(store, r["content"])
    ]


def action_state(result):
    if (
        result.get("error")
        or result.get("ok") is False
        or result.get("status") in {"error", "failed"}
    ):
        return "error"
    if result.get("physical_confirmed") is True or result.get("verified") is True:
        return "confirmed"
    delivery = result.get("delivery_state") or result.get("state")
    if delivery in {"confirmed", "result_confirmed"}:
        return "confirmed"
    if result.get("acknowledged") is True or delivery in {"acknowledged", "device_responded"}:
        return "acknowledged"
    if result.get("sent") is True or delivery in {"sent", "command_sent", "sent_unconfirmed"}:
        return "sent"
    return "unverified"


def record_action(store, name, parameters, result):
    from backend.context import request_context
    from backend.memory.profile import _SENSITIVE
    from backend.settings_models import MemoryPolicy

    c = request_context.get()
    if not c or not c.device.authenticated or name in {"remember", "forget", "manage_memory"}:
        return
    policy = MemoryPolicy.model_validate(store.preference("settings", "memory") or {})
    if policy.automatic == "off" or not policy.action_learning:
        return
    cid = store.session_conversation(c.device.session_id)
    if store.db.execute("SELECT memory_disabled FROM conversations WHERE id=?", (cid,)).fetchone()[
        0
    ]:
        return
    if name.startswith(("get_", "list_", "search_", "read_", "check_")):
        return
    if result.get("status") in {"confirmation_required", "denied", "blocked"}:
        return
    # Do not accumulate personal results, coordinates, arbitrary documents, or credentials.
    payload = json.dumps([c.text, parameters, result], ensure_ascii=False)
    if _SENSITIVE.search(payload) or contains_secret(payload):
        return
    with store.memory_transaction():
        store.db.execute(
            "INSERT INTO memory_actions(conversation_id,request_id,intent,tool,"
            "parameters,result,state,error,correction,created_at) VALUES"
            "(?,?,?,?,?,?,?,?,?,?)",
            (
                cid,
                c.request_id,
                c.text[:300],
                name,
                json.dumps(redact(parameters), ensure_ascii=False)[:1500],
                json.dumps(redact(result), ensure_ascii=False)[:3000],
                action_state(result),
                str(result["error"])[:300] if result.get("error") else None,
                str(result["correction"])[:300] if result.get("correction") else None,
                _now(),
            ),
        )
        cutoff = (datetime.now(UTC) - timedelta(days=policy.action_retention_days)).isoformat()
        store.db.execute("DELETE FROM memory_actions WHERE created_at<?", (cutoff,))
        store.db.execute(
            "DELETE FROM memory_actions WHERE id <= "
            "(SELECT COALESCE(MAX(id),0)-5000 FROM memory_actions)"
        )


def disable_conversation(store, cid):
    with store.memory_transaction():
        store.db.execute("UPDATE conversations SET memory_disabled=1 WHERE id=?", (cid,))
        store.db.execute(
            ("UPDATE messages SET memory_blocked=1 WHERE conversation_id=? AND memory_blocked=0"),
            (cid,),
        )
        for row in store.db.execute(
            "SELECT DISTINCT f.* FROM facts f JOIN memory_evidence e "
            "ON e.memory_id=f.id JOIN messages m ON m.id=e.message_id "
            "WHERE m.conversation_id=?",
            (cid,),
        ).fetchall():
            try:
                automatic = json.loads(row["source"]).get("kind") == "current_user_statement"
            except ValueError:
                automatic = False
            if automatic:
                store.forget_fact(row["id"])
        store.db.execute("DELETE FROM memory_actions WHERE conversation_id=?", (cid,))
        store.db.execute("DELETE FROM conversation_summaries WHERE conversation_id=?", (cid,))
        pending = store.preference("memory", "pending") or []
        store.set_preference(
            "memory", "pending", [p for p in pending if p.get("conversation_id") != cid]
        )
        store.invalidate_memory_context()


def delete_conversation(store, cid, *, facts=False):
    with store.memory_transaction():
        if facts:
            for row in store.db.execute(
                "SELECT DISTINCT memory_id FROM memory_evidence e JOIN "
                "messages m ON m.id=e.message_id WHERE m.conversation_id=?",
                (cid,),
            ).fetchall():
                store.forget_fact(row[0])
        # Facts can deliberately survive deletion of the transcript, with no dangling evidence.
        store.db.execute("DELETE FROM dialogue_sessions WHERE conversation_id=?", (cid,))
        store.db.execute("DELETE FROM conversations WHERE id=?", (cid,))
        store.clear_preference("dialogue_pending", str(cid))
        pending = store.preference("memory", "pending") or []
        store.set_preference(
            "memory", "pending", [p for p in pending if p.get("conversation_id") != cid]
        )
        store.invalidate_memory_context()


def action_context(store, text, project=None, device="local-mac"):
    """Only recent relevant observations, with explicit verification state, within one scope."""
    from backend.memory.profile import _SENSITIVE, tokens

    query = tokens(text)
    if not query:
        return []
    records = store.db.execute(
        "SELECT a.intent,a.tool,a.state,a.error,a.correction,a.created_at FROM memory_actions a "
        "JOIN conversations c ON c.id=a.conversation_id LEFT JOIN memory_project_conversations p "
        "ON p.conversation_id=c.id LEFT JOIN messages m ON m.id=a.message_id "
        "WHERE p.project_id IS ? AND c.deleted_at IS NULL AND c.memory_disabled=0 "
        "AND (?='local-mac' OR m.device_id=?) ORDER BY a.id DESC LIMIT 100",
        (project, device, device),
    ).fetchall()
    result = []
    labels = {
        "sent": "отправлена; результат не подтверждён",
        "acknowledged": "устройство ответило; результат не подтверждён",
        "confirmed": "результат проверен",
        "error": "ошибка",
        "unverified": "результат не подтверждён",
    }
    for r in records:
        content = " ".join(str(r[k] or "") for k in ("intent", "error", "correction"))
        if (
            query & tokens(content)
            and not _SENSITIVE.search(content)
            and not contains_secret(content)
        ):
            result.append(
                f"{r['tool']}: {r['intent']} — {labels[r['state']]}"
                + (f"; ошибка: {r['error']}" if r["error"] else "")
                + (f"; исправление: {r['correction']}" if r["correction"] else "")
            )
        if len(result) == 3:
            break
    return result
