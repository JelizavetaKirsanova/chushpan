"""Projects, FTS retrieval and a desktop library over the existing facts table."""

import json
import sqlite3
import time
from datetime import UTC, datetime
from difflib import SequenceMatcher
from time import perf_counter
from uuid import uuid4
from zoneinfo import ZoneInfo

from backend.center import Center, identifier, rows, text
from backend.memory.profile import _SENSITIVE, CATEGORY_LABELS, tokens
from backend.memory.store import _now

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS memory_projects (id INTEGER PRIMARY KEY, name TEXT NOT NULL "
    "UNIQUE, description TEXT NOT NULL DEFAULT '', access TEXT NOT NULL DEFAULT 'isolated', "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS memory_sources (memory_id INTEGER PRIMARY KEY REFERENCES "
    "facts(id) ON DELETE CASCADE, source TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS memory_relations (memory_id INTEGER NOT NULL REFERENCES "
    "facts(id) ON DELETE CASCADE, related_id INTEGER NOT NULL REFERENCES facts(id) ON DELETE "
    "CASCADE, kind TEXT NOT NULL, PRIMARY KEY(memory_id,related_id,kind))",
    "CREATE TABLE IF NOT EXISTS memory_access_log (id INTEGER PRIMARY KEY, memory_id INTEGER "
    "REFERENCES facts(id) ON DELETE CASCADE, at TEXT NOT NULL, request_id TEXT, purpose TEXT "
    "NOT NULL)",
    "CREATE INDEX IF NOT EXISTS memory_access_at ON memory_access_log(at)",
    "CREATE TABLE IF NOT EXISTS memory_project_conversations (project_id INTEGER NOT NULL "
    "REFERENCES memory_projects(id) ON DELETE CASCADE, conversation_id INTEGER NOT NULL "
    "REFERENCES conversations(id) ON DELETE CASCADE, PRIMARY KEY(project_id,conversation_id))",
)


def migrate(store):
    db = store.db
    with store.memory_transaction():
        for statement in SCHEMA:
            db.execute(statement)
        fields = {r["name"] for r in db.execute("PRAGMA table_info(facts)")}
        for name, declaration in {
            "project_id": "INTEGER REFERENCES memory_projects(id)",
            "expires_at": "TEXT",
            "confirmed": "INTEGER NOT NULL DEFAULT 1",
        }.items():
            if name not in fields:
                db.execute(f"ALTER TABLE facts ADD COLUMN {name} {declaration}")
        db.execute("CREATE INDEX IF NOT EXISTS memory_project ON facts(project_id,status)")
        existed = db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_fts'").fetchone()
        try:
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(content, "
                "content='facts', content_rowid='id', tokenize='unicode61')"
            )
            for event, body in {
                "INSERT": "INSERT INTO memory_fts(rowid,content) VALUES(new.id,new.content);",
                "DELETE": "INSERT INTO memory_fts(memory_fts,rowid,content) "
                "VALUES('delete',old.id,old.content);",
                "UPDATE": "INSERT INTO memory_fts(memory_fts,rowid,content) "
                "VALUES('delete',old.id,old.content); INSERT INTO memory_fts(rowid,content) "
                "VALUES(new.id,new.content);",
            }.items():
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS memory_fts_{event.lower()} AFTER "
                    f"{event} ON facts BEGIN {body} END"
                )
            if not existed:
                db.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_vocabulary USING "
                "fts5vocab(memory_fts,'row')"
            )
            store.memory_fts = True
        except sqlite3.OperationalError:
            store.memory_fts = False
        db.execute("INSERT OR IGNORE INTO memory_sources SELECT id,source FROM facts")
        db.execute(
            "CREATE TRIGGER IF NOT EXISTS memory_source_insert AFTER INSERT ON facts BEGIN "
            "INSERT INTO memory_sources VALUES(new.id,new.source); END"
        )
        db.execute(
            "CREATE TRIGGER IF NOT EXISTS memory_source_update AFTER UPDATE OF source ON "
            "facts BEGIN UPDATE memory_sources SET source=new.source WHERE memory_id=new.id; END"
        )


def search(
    store,
    query,
    project=None,
    limit=100,
    *,
    context=False,
    category="",
    entity=None,
    include_inactive=False,
    fuzzy=True,
    offset=0,
    sort="recent",
):
    """Only a bounded candidate set; project facts never enter global context implicitly."""
    started = perf_counter()
    limit = min(200, max(1, limit))
    arguments = []
    where = ["1=1"]
    if not include_inactive:
        where.extend(["f.status='active'", "(f.expires_at IS NULL OR f.expires_at>?)"])
        arguments.append(_now())
    if category:
        where.append("f.category=?")
        arguments.append(category)
    if entity is not None:
        where.append(
            "EXISTS(SELECT 1 FROM memory_entity_links l WHERE l.memory_id=f.id AND l.entity_id=?)"
        )
        arguments.append(identifier(entity))
    if project is not None:
        where.append("f.project_id=?")
        arguments.append(identifier(project))
    elif context:
        where.append("f.project_id IS NULL")
    terms = sorted(tokens(query))[:20]
    if "education" in terms:
        terms.extend(["уч", "универс", "taltech"])
    join = ""
    order = {
        "oldest": "f.created_at,f.id",
        "confidence": "f.confirmed DESC,f.confidence DESC,f.updated_at DESC",
    }.get(sort, "f.updated_at DESC,f.id DESC")
    if terms and store.memory_fts:
        join = " JOIN memory_fts ON memory_fts.rowid=f.id"
        where.append("memory_fts MATCH ?")
        arguments.append(" OR ".join('"' + term + '"*' for term in terms))
        order = "bm25(memory_fts),f.updated_at DESC"
    elif terms:
        where.append("(" + " OR ".join("casefold(f.content) LIKE ?" for _ in terms) + ")")
        arguments.extend("%" + term + "%" for term in terms)
    elif query.strip():
        where.append("casefold(f.content) LIKE ?")
        arguments.append("%" + query.casefold().replace("%", "")[:200] + "%")
    result = rows(
        store.db,
        "SELECT f.* FROM facts f"
        + join
        + " WHERE "
        + " AND ".join(where)
        + " ORDER BY "
        + order
        + " LIMIT ? OFFSET ?",
        (*arguments, limit, min(1000000, max(0, int(offset)))),
    )
    if not result and terms and store.memory_fts and fuzzy:
        # Indexed vocabulary prefix; never scan the entire fact table for typos.
        alternatives = set()
        for term in terms[:6]:
            if len(term) < 4:
                continue
            candidates = store.db.execute(
                "SELECT term FROM memory_vocabulary WHERE term>=? AND term<? LIMIT 128",
                (term[:2], term[:2] + "\uffff"),
            )
            for candidate in candidates:
                word = candidate[0][:6]
                if (
                    abs(len(word) - len(term)) <= 1
                    and SequenceMatcher(None, word, term).ratio() >= 0.78
                ):
                    alternatives.add(word)
        if alternatives:
            result = search(
                store,
                " ".join(sorted(alternatives)[:8]),
                project,
                limit,
                context=context,
                category=category,
                entity=entity,
                include_inactive=include_inactive,
                fuzzy=False,
            )
            for item in result:
                item["_fuzzy"] = True
    from backend.memory.intelligence import metric

    metric(store, "search", started, len(result))
    return result


def log_access(store, items):
    from backend.context import request_context

    context = request_context.get()
    if not context or not context.device.authenticated or not items:
        return
    key = context.request_id
    with store.memory_transaction():
        for item in items:
            store.db.execute(
                "INSERT INTO memory_access_log(memory_id,at,request_id,purpose) "
                "SELECT ?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM memory_access_log "
                "WHERE memory_id=? AND request_id=?)",
                (item["id"], _now(), key, "answer_context", item["id"], key),
            )
        store.db.execute(
            "DELETE FROM memory_access_log WHERE id NOT IN "
            "(SELECT id FROM memory_access_log ORDER BY id DESC LIMIT 10000)"
        )


class MemoryLibrary(Center):
    channel = "memory_library"

    def __init__(self, store, settings, bus):
        self.store, self.settings = store, settings
        self.query, self.project, self.category = "", None, ""
        self.sort = "recent"
        self.page = 0
        self.entity = None
        self.selected_conversation = None
        self.pending_import = None
        self.bind(bus)

    def snapshot(self):
        db = self.store.db
        records = search(
            self.store,
            self.query,
            self.project,
            category=self.category,
            include_inactive=True,
            entity=self.entity,
            offset=self.page * 100,
            sort=self.sort,
        )
        if self.sort == "oldest":
            records.sort(key=lambda r: r["created_at"])
        elif self.sort == "confidence":
            records.sort(
                key=lambda r: (r["confirmed"], r["confidence"], r["updated_at"]), reverse=True
            )
        today = datetime.now(ZoneInfo(self.settings.location.tz_name)).date()
        used = rows(
            db, "SELECT DISTINCT memory_id,at FROM memory_access_log ORDER BY id DESC LIMIT 1000"
        )
        return {
            "items": records,
            "page": self.page,
            "counts": {
                r["category"]: r["n"]
                for r in db.execute("SELECT category,COUNT(*) n FROM facts GROUP BY category")
            },
            "confirmed": db.execute("SELECT COUNT(*) FROM facts WHERE confirmed=1").fetchone()[0],
            "unconfirmed": db.execute("SELECT COUNT(*) FROM facts WHERE confirmed=0").fetchone()[0],
            "entities": rows(
                db,
                (
                    "SELECT e.*,COUNT(l.memory_id) memories FROM memory_entities e "
                    "JOIN memory_entity_links l ON l.entity_id=e.id GROUP BY e.id "
                    "ORDER BY memories DESC LIMIT 100"
                ),
            ),
            "entity_links": rows(db, "SELECT * FROM memory_entity_links LIMIT 2000"),
            "relations": rows(
                db,
                (
                    "SELECT r.*,f.content,g.content AS related_content FROM "
                    "memory_relations r JOIN facts f ON f.id=r.memory_id JOIN facts g "
                    "ON g.id=r.related_id LIMIT 100"
                ),
            ),
            "summaries": rows(db, "SELECT * FROM conversation_summaries LIMIT 100"),
            "history_matches": self.history_matches(),
            "selected_conversation": self.selected_conversation,
            "messages": self.store.conversation_page(self.selected_conversation)
            if self.selected_conversation
            else [],
            "evidence": rows(
                db,
                (
                    "SELECT e.memory_id,m.conversation_id,m.id AS message_id FROM "
                    "memory_evidence e JOIN messages m ON m.id=e.message_id LIMIT 3000"
                ),
            ),
            "versions": rows(db, "SELECT * FROM memory_versions ORDER BY id DESC LIMIT 200"),
            "actions": rows(db, "SELECT * FROM memory_actions ORDER BY id DESC LIMIT 100"),
            "metrics": rows(
                db,
                (
                    "SELECT stage,ROUND(AVG(duration_ms),3) AS mean_ms,COUNT(*) AS "
                    "samples,SUM(count) AS items FROM memory_metrics GROUP BY stage"
                ),
            ),
            "backups": self.backups(),
            "import_preview": self.pending_import[2] if self.pending_import else None,
            "storage": {
                "facts": db.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
                "messages": db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "bytes": db.execute("PRAGMA page_count").fetchone()[0]
                * db.execute("PRAGMA page_size").fetchone()[0],
            },
            "projects": rows(
                db,
                "SELECT p.*,COUNT(f.id) AS memories FROM memory_projects p "
                "LEFT JOIN facts f ON f.project_id=p.id GROUP BY p.id ORDER BY p.updated_at DESC",
            ),
            "total": db.execute("SELECT COUNT(*) FROM facts WHERE status='active'").fetchone()[0],
            "used_today": len(
                {
                    r["memory_id"]
                    for r in used
                    if datetime.fromisoformat(r["at"])
                    .astimezone(ZoneInfo(self.settings.location.tz_name))
                    .date()
                    == today
                }
            ),
            "access": rows(
                db,
                "SELECT a.*,f.content FROM memory_access_log a JOIN facts f ON "
                "f.id=a.memory_id ORDER BY a.id DESC LIMIT 100",
            ),
            "pending": self.store.preference("memory", "pending") or [],
            "fts": self.store.memory_fts,
            "project": self.project,
            "query": self.query,
            "categories": CATEGORY_LABELS,
            "policy": self.store.preference("settings", "memory") or {},
            "conversations": self.store.conversation_list(self.query)[:100],
            "bindings": rows(db, "SELECT * FROM memory_project_conversations LIMIT 200"),
        }

    def project_id(self, value):
        if value in (None, ""):
            return None
        value = identifier(value)
        if not self.store.db.execute(
            "SELECT 1 FROM memory_projects WHERE id=?", (value,)
        ).fetchone():
            raise ValueError("Проект не найден.")
        return value

    async def handle(self, value):
        op, store = value.get("op", "list"), self.store
        if op not in {"list", "export"}:
            store._require_memory()
        message = None
        if op == "list":
            self.query = str(value.get("query", ""))[:200]
            self.project = self.project_id(value.get("project"))
            self.category = str(value.get("category", ""))
            self.sort = value.get("sort", "recent")
            self.page = min(10000, max(0, int(value.get("page", 0))))
            self.entity = identifier(value["entity"]) if value.get("entity") else None
        elif op == "conversation_open":
            cid = identifier(value.get("id"))
            if not store.db.execute(
                "SELECT 1 FROM conversations WHERE id=? AND deleted_at IS NULL", (cid,)
            ).fetchone():
                raise ValueError("Разговор не найден.")
            self.selected_conversation = cid
        elif op == "conversation_delete":
            from backend.memory.intelligence import delete_conversation

            if value.get("confirmed") is not True:
                raise ValueError("Подтверди удаление разговора.")
            delete_conversation(
                store, identifier(value.get("id")), facts=value.get("delete_facts") is True
            )
            self.selected_conversation = None
            message = (
                "Разговор удалён."
                if not value.get("delete_facts")
                else "Разговор и связанные факты удалены."
            )
        elif op == "conversation_private":
            from backend.memory.intelligence import disable_conversation

            disable_conversation(store, identifier(value.get("id")))
        elif op == "policy":
            from backend.settings_models import MemoryPolicy

            policy = MemoryPolicy.model_validate(value.get("value"))
            store.set_preference("settings", "memory", policy.model_dump())
        elif op == "link":
            item = identifier(value.get("id"))
            other = identifier(value.get("related_id"))
            kind = value.get("kind", "related")
            if item == other or kind not in {"related", "corrects", "depends_on", "solves"}:
                raise ValueError("Укажи две разные записи и вид связи.")
            with store.memory_transaction():
                scopes = rows(
                    store.db, "SELECT id,project_id FROM facts WHERE id IN (?,?)", (item, other)
                )
                if len(scopes) != 2 or scopes[0]["project_id"] != scopes[1]["project_id"]:
                    raise ValueError("Связь возможна между записями одного пространства.")
                store.db.execute(
                    "INSERT OR IGNORE INTO memory_relations VALUES(?,?,?)", (item, other, kind)
                )
        elif op in {"import_preview", "backup_preview"}:
            if op == "backup_preview":
                from backend.memory.backup import read

                raw = json.dumps(
                    read(self.backup_folder(), value.get("name"), value.get("password")),
                    ensure_ascii=False,
                )
            else:
                raw = value.get("content", "")
            # Execute the exact importer in a savepoint and roll it back, including FTS.
            store.db.execute("SAVEPOINT memory_preview")
            try:
                result = await self.handle(
                    {"op": "import", "content": raw, "consent_sensitive": True}
                )
                candidate = json.loads(raw)
                preview = {
                    "token": uuid4().hex,
                    "sensitive": sum(
                        bool(r.get("sensitive") or _SENSITIVE.search(r.get("content", "")))
                        for r in candidate["items"]
                    ),
                    "count": len(candidate["items"]),
                    "items": [
                        {"content": r["content"], "category": r.get("category", "profile")}
                        for r in candidate["items"][:100]
                    ],
                }
            finally:
                store.db.execute("ROLLBACK TO memory_preview")
                store.db.execute("RELEASE memory_preview")
            self.pending_import = (raw, time.monotonic() + 300, preview)
            message = "Проверь записи и подтверди импорт."
        elif op == "import_apply":
            staged = self.pending_import
            if (
                not staged
                or staged[1] < time.monotonic()
                or staged[2]["token"] != value.get("token")
                or value.get("confirmed") is not True
            ):
                raise ValueError("Предпросмотр устарел. Выбери файл заново.")
            if staged[2]["sensitive"] and value.get("consent_sensitive") is not True:
                raise ValueError("Для чувствительных записей нужно отдельное согласие.")
            result = await self.handle(
                {
                    "op": "import",
                    "content": staged[0],
                    "consent_sensitive": value.get("consent_sensitive") is True,
                }
            )
            self.pending_import = None
            return {**result, "data": self.snapshot()}
        elif op == "import_cancel":
            self.pending_import = None
        elif op == "backup":
            import asyncio

            from backend.memory.backup import create

            name = await asyncio.to_thread(
                create, self.backup_folder(), self.document(), value.get("password")
            )
            message = "Создана зашифрованная локальная копия: " + name
        elif op == "backup_delete":
            from backend.memory.backup import selected

            if value.get("confirmed") is not True:
                raise ValueError("Подтверди удаление копии.")
            selected(self.backup_folder(), value.get("name")).unlink()
        elif op in {"add", "update", "confirm", "delete"}:
            store._require_memory()
            if op == "add":
                content = text(value.get("content"))
                sensitive = bool(_SENSITIVE.search(content)) or value.get("sensitive") is True
                if sensitive and value.get("consent_sensitive") is not True:
                    raise ValueError("Для чувствительной записи нужно отдельное явное согласие.")
                project = self.project_id(value.get("project"))
                with store.memory_transaction():
                    # A scoped key prevents deduplication across independent projects.
                    key = "library:" + str(project) + ":" + store.memory_digest(content)
                    item, _ = store.save_memory(
                        content,
                        category=value.get("category", "profile"),
                        key=key,
                        source="desktop_library",
                        sensitive=sensitive,
                        project_id=project,
                    )
                    self.metadata(item, value)
            else:
                item = identifier(value.get("id"))
                row = store.db.execute("SELECT * FROM facts WHERE id=?", (item,)).fetchone()
                if not row or row["revision"] != value.get("revision"):
                    raise ValueError("Запись изменилась. Обнови список.")
                if op == "delete":
                    if value.get("confirmed") is not True:
                        raise ValueError("Подтверди удаление выбранного воспоминания.")
                    store.forget_fact(item, expected_revision=row["revision"])
                elif op == "confirm":
                    with store.memory_transaction():
                        store.db.execute(
                            "UPDATE facts SET confirmed=1,confidence=1,"
                            "revision=revision+1,updated_at=?,confirmed_at=? "
                            "WHERE id=?",
                            (_now(), _now(), item),
                        )
                else:
                    content = text(value.get("content"))
                    sensitive = bool(_SENSITIVE.search(content)) or value.get("sensitive") is True
                    if sensitive and value.get("consent_sensitive") is not True:
                        raise ValueError("Подтверди сохранение чувствительной записи.")
                    with store.memory_transaction():
                        store._suppress(row["content"])
                        store.db.execute(
                            "UPDATE facts SET "
                            "content=?,category=?,sensitive=?,revision=revision+1,updated_at=?,confirmed_at=?"
                            " WHERE id=?",
                            (
                                content,
                                value.get("category", row["category"]),
                                int(sensitive),
                                _now(),
                                _now(),
                                item,
                            ),
                        )
                        self.metadata(item, value)
                        store.invalidate_memory_context()
            message = "Память обновлена."
        elif op == "project_add":
            name, description = text(value.get("name"), 80), str(value.get("description", ""))
            if description:
                description = text(description, 1000)
            with store.memory_transaction():
                store.db.execute(
                    "INSERT INTO "
                    "memory_projects(name,description,created_at,updated_at) "
                    "VALUES(?,?,?,?)",
                    (name, description, _now(), _now()),
                )
            message = "Создано отдельное пространство памяти."
        elif op == "project_bind":
            project = self.project_id(value.get("project"))
            conversation = identifier(value.get("conversation"))
            if (
                project is None
                or not store.db.execute(
                    "SELECT 1 FROM conversations WHERE id=? AND deleted_at IS NULL", (conversation,)
                ).fetchone()
            ):
                raise ValueError("Укажи существующий проект и разговор.")
            with store.memory_transaction():
                store.db.execute(
                    "DELETE FROM memory_project_conversations WHERE conversation_id=?",
                    (conversation,),
                )
                store.db.execute(
                    "INSERT INTO memory_project_conversations VALUES(?,?)", (project, conversation)
                )
                store.invalidate_memory_context()
        elif op == "export":
            result = self.document()
            return {
                "data": self.snapshot(),
                "download": {
                    "name": "chushpan-memory.json",
                    "mime": "application/json",
                    "content": json.dumps(result, ensure_ascii=False, indent=2),
                },
            }
        elif op == "import":
            raw = value.get("content", "")
            if not isinstance(raw, str) or len(raw) > 1_000_000:
                raise ValueError("Импорт: файл до 1 МБ.")
            data = json.loads(raw)
            if (
                not isinstance(data, dict)
                or data.get("schema") != 1
                or not isinstance(data.get("items"), list)
                or len(data["items"]) > 1000
                or not isinstance(data.get("projects", []), list)
                or len(data.get("projects", [])) > 100
            ):
                raise ValueError("Нужен экспорт памяти версии 1, до 1000 записей.")
            with store.memory_transaction():
                projects = {}
                for p in data.get("projects", [])[:100]:
                    name = text(p.get("name"), 80)
                    store.db.execute(
                        "INSERT OR IGNORE INTO "
                        "memory_projects(name,description,created_at,updated_at) "
                        "VALUES(?,?,?,?)",
                        (name, text(p.get("description") or name, 1000), _now(), _now()),
                    )
                    projects[p["id"]] = store.db.execute(
                        "SELECT id FROM memory_projects WHERE name=?", (name,)
                    ).fetchone()[0]
                mapping = {}
                for row in data["items"]:
                    content = text(row.get("content"))
                    sensitive = bool(_SENSITIVE.search(content) or row.get("sensitive"))
                    if sensitive and value.get("consent_sensitive") is not True:
                        raise ValueError(
                            "Чувствительные записи импортируй отдельно с явным согласием."
                        )
                    project = projects.get(row.get("project_id"))
                    if row.get("project_id") is not None and project is None:
                        raise ValueError("В файле отсутствует проект записи.")
                    existing = store.db.execute(
                        "SELECT id FROM facts WHERE project_id IS ? AND casefold(content)=?",
                        (project, content.casefold()),
                    ).fetchone()
                    if existing:
                        if row.get("id") is not None:
                            mapping[row["id"]] = existing[0]
                        continue
                    item, _ = store.save_memory(
                        content,
                        category=row.get("category", "profile"),
                        sensitive=sensitive,
                        key="import:" + str(project) + ":" + store.memory_digest(content),
                        source="user_import",
                        project_id=project,
                        confidence=float(row.get("confidence", 1)),
                    )
                    if row.get("id") is not None:
                        mapping[row["id"]] = item
                    confirmed = row.get("confirmed", 0)
                    status = row.get("status", "active")
                    if confirmed not in (0, 1) or status not in {"active", "stale"}:
                        raise ValueError("Некорректный статус импортируемой записи.")
                    self.metadata(
                        item,
                        {
                            "project": project,
                            "category": row.get("category", "profile"),
                            "expires_at": row.get("expires_at"),
                        },
                    )
                    store.db.execute(
                        "UPDATE facts SET confirmed=?,status=? WHERE id=?",
                        (confirmed, status, item),
                    )
                entities = {e["id"]: text(e["name"], 80) for e in data.get("entities", [])[:2000]}
                from backend.memory.intelligence import link_entities

                for link in data.get("entity_links", [])[:5000]:
                    if (
                        link.get("memory_id") not in mapping
                        or link.get("entity_id") not in entities
                    ):
                        raise ValueError("Неверная связь сущности в импорте.")
                    link_entities(store, mapping[link["memory_id"]], [entities[link["entity_id"]]])
                for relation in data.get("relations", [])[:5000]:
                    left, right = (
                        mapping.get(relation.get("memory_id")),
                        mapping.get(relation.get("related_id")),
                    )
                    kind = relation.get("kind")
                    if (
                        not left
                        or not right
                        or left == right
                        or kind not in {"related", "corrects", "depends_on", "solves"}
                    ):
                        raise ValueError("Неверная связь записей в импорте.")
                    scopes = rows(
                        store.db, "SELECT project_id FROM facts WHERE id IN (?,?)", (left, right)
                    )
                    if scopes[0]["project_id"] != scopes[1]["project_id"]:
                        raise ValueError("Разные пространства памяти в связи.")
                    store.db.execute(
                        "INSERT OR IGNORE INTO memory_relations VALUES(?,?,?)", (left, right, kind)
                    )
            message = "Импорт завершён."
        elif op == "clear":
            if value.get("confirmed") is not True:
                raise ValueError("Подтверди очистку выбранной категории.")
            category = value.get("category")
            if category and category not in CATEGORY_LABELS:
                raise ValueError("Неизвестная категория.")
            with store.memory_transaction():
                targets = rows(
                    store.db,
                    "SELECT * FROM facts" + (" WHERE category=?" if category else ""),
                    (category,) if category else (),
                )
                for row in targets:
                    store.forget_fact(row["id"])
                store.clear_preference("memory", "pending")
            message = (
                "Записи исключены из поиска и контекста. Старые резервные копии удаляются отдельно."
            )
        else:
            raise ValueError("Неизвестное действие памяти.")
        return {"data": self.snapshot(), "message": message}

    def metadata(self, item, value):
        category = value.get("category", "profile")
        if category not in CATEGORY_LABELS:
            raise ValueError("Неизвестная категория.")
        project = self.project_id(value.get("project"))
        expires = value.get("expires_at") or None
        if expires:
            try:
                parsed = datetime.fromisoformat(expires)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=ZoneInfo(self.settings.location.tz_name))
                expires = parsed.astimezone(UTC).isoformat(timespec="seconds")
            except (ValueError, TypeError):
                raise ValueError("Неверный срок актуальности.") from None
        self.store.db.execute(
            "UPDATE facts SET project_id=?,expires_at=? WHERE id=?", (project, expires, item)
        )
        from backend.memory.intelligence import infer_entities, link_entities

        content = self.store.db.execute("SELECT content FROM facts WHERE id=?", (item,)).fetchone()[
            0
        ]
        entities = value.get("entities", infer_entities(content))
        if not isinstance(entities, list) or any(not isinstance(name, str) for name in entities):
            raise ValueError("Укажи список имён или названий.")
        self.store.db.execute("DELETE FROM memory_entity_links WHERE memory_id=?", (item,))
        link_entities(self.store, item, entities)
        if project:
            self.store.db.execute(
                "UPDATE memory_projects SET updated_at=? WHERE id=?", (_now(), project)
            )

    def history_matches(self):
        from backend.memory.intelligence import history

        return history(self.store, self.query, project=self.project) if self.query else []

    def backup_folder(self):
        return self.settings.data_path / "memory-backups"

    def backups(self):
        from backend.memory.backup import listing

        return listing(self.backup_folder())

    def document(self):
        return {
            "schema": 1,
            "items": self.store.memories(),
            "projects": rows(self.store.db, "SELECT * FROM memory_projects"),
            "entities": rows(
                self.store.db,
                (
                    "SELECT * FROM memory_entities WHERE id IN (SELECT entity_id FROM "
                    "memory_entity_links)"
                ),
            ),
            "entity_links": rows(self.store.db, "SELECT * FROM memory_entity_links"),
            "relations": rows(self.store.db, "SELECT * FROM memory_relations"),
        }
