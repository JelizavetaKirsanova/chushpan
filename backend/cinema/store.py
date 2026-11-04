"""Versioned cinema tables in the assistant's SQLite file, on a separate connection.

All service calls run on one worker; no SQLite I/O blocks STT or the HUD loop.
"""

import json
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from backend.cinema.models import Entry, Metadata, Preferences, Source, origin

MIGRATIONS = [
    """
    CREATE TABLE cinema_titles (
      id TEXT PRIMARY KEY, identity TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
      kind TEXT NOT NULL, year INTEGER NOT NULL, poster_url TEXT NOT NULL,
      description TEXT NOT NULL, catalogue_key TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'planned', favorite INTEGER NOT NULL DEFAULT 0,
      last_episode_id TEXT, updated_at TEXT NOT NULL);
    CREATE TABLE cinema_sources (id TEXT PRIMARY KEY, data TEXT NOT NULL);
    CREATE TABLE cinema_episodes (
      id TEXT PRIMARY KEY, title_id TEXT NOT NULL REFERENCES cinema_titles(id) ON DELETE CASCADE,
      season INTEGER NOT NULL, episode INTEGER NOT NULL,
      position REAL NOT NULL DEFAULT 0, duration REAL NOT NULL DEFAULT 0,
      watched INTEGER NOT NULL DEFAULT 0, last_viewed TEXT, last_link_id TEXT,
      UNIQUE(title_id,season,episode));
    CREATE TABLE cinema_links (
      id TEXT PRIMARY KEY,
      episode_id TEXT NOT NULL REFERENCES cinema_episodes(id) ON DELETE CASCADE,
      source_id TEXT NOT NULL REFERENCES cinema_sources(id), url TEXT NOT NULL, voice TEXT NOT NULL,
      UNIQUE(episode_id,source_id,voice));
    CREATE TABLE cinema_history (
      id INTEGER PRIMARY KEY,
      episode_id TEXT NOT NULL REFERENCES cinema_episodes(id) ON DELETE CASCADE,
      link_id TEXT REFERENCES cinema_links(id), position REAL NOT NULL, duration REAL NOT NULL,
      event TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE INDEX cinema_recent ON cinema_episodes(last_viewed DESC);
    CREATE INDEX cinema_events ON cinema_history(created_at DESC);
    CREATE TABLE cinema_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
    """
    CREATE TABLE cinema_library (
      title_id TEXT PRIMARY KEY REFERENCES cinema_titles(id) ON DELETE CASCADE,
      status TEXT NOT NULL DEFAULT 'planned', favorite INTEGER NOT NULL DEFAULT 0,
      user_rating INTEGER, completed_at TEXT);
    INSERT INTO cinema_library(title_id,status,favorite,completed_at)
      SELECT id,status,favorite,CASE WHEN status='watched' THEN updated_at END FROM cinema_titles;
    CREATE TABLE cinema_metadata (
      title_id TEXT PRIMARY KEY REFERENCES cinema_titles(id) ON DELETE CASCADE,
      data TEXT NOT NULL);
    ALTER TABLE cinema_episodes ADD COLUMN name TEXT NOT NULL DEFAULT '';
    """,
    """
    ALTER TABLE cinema_library RENAME TO cinema_library_v2;
    CREATE TABLE cinema_library (
      title_id TEXT PRIMARY KEY REFERENCES cinema_titles(id) ON DELETE CASCADE,
      status TEXT NOT NULL DEFAULT 'planned', favorite INTEGER NOT NULL DEFAULT 0,
      user_rating REAL CHECK(user_rating IS NULL OR
        (user_rating BETWEEN 1 AND 10 AND user_rating*2=CAST(user_rating*2 AS INTEGER))),
      completed_at TEXT, added_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      favorite_added_at TEXT);
    INSERT INTO cinema_library
      SELECT u.title_id,u.status,u.favorite,u.user_rating,u.completed_at,
        t.updated_at,t.updated_at,CASE WHEN u.favorite THEN t.updated_at END
      FROM cinema_library_v2 u JOIN cinema_titles t ON t.id=u.title_id;
    DROP TABLE cinema_library_v2;
    CREATE INDEX cinema_tmdb_identity ON cinema_titles(catalogue_key) WHERE catalogue_key!='';
    """,
    """
    CREATE TABLE cinema_source_health (
      source_id TEXT PRIMARY KEY REFERENCES cinema_sources(id) ON DELETE CASCADE,
      status TEXT NOT NULL DEFAULT 'unknown', checked_at TEXT, last_success TEXT,
      reason TEXT NOT NULL DEFAULT '', successes INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE cinema_source_memory (
      title_id TEXT PRIMARY KEY REFERENCES cinema_titles(id) ON DELETE CASCADE,
      pinned_source_id TEXT REFERENCES cinema_sources(id) ON DELETE SET NULL,
      last_source_id TEXT REFERENCES cinema_sources(id) ON DELETE SET NULL,
      page_url TEXT NOT NULL DEFAULT '', episode_id TEXT, updated_at TEXT NOT NULL);
    CREATE TABLE cinema_feedback (catalogue_key TEXT PRIMARY KEY, created_at TEXT NOT NULL);
    CREATE TABLE cinema_rating_events (
      id INTEGER PRIMARY KEY, title_id TEXT NOT NULL REFERENCES cinema_titles(id) ON DELETE CASCADE,
      rating REAL, created_at TEXT NOT NULL);
    CREATE INDEX cinema_rating_dates ON cinema_rating_events(created_at);
    CREATE TABLE cinema_link_checks (
      link_id TEXT PRIMARY KEY REFERENCES cinema_links(id) ON DELETE CASCADE,
      status TEXT NOT NULL, checked_at TEXT NOT NULL, playable INTEGER NOT NULL DEFAULT 0);
    ALTER TABLE cinema_history ADD COLUMN source_label TEXT NOT NULL DEFAULT '';
    """,
    """
    ALTER TABLE cinema_library ADD COLUMN user_note TEXT NOT NULL DEFAULT '';
    CREATE INDEX cinema_import_history ON cinema_history(episode_id,created_at);
    """,
    """
    CREATE TABLE cinema_link_verification (
      link_id TEXT PRIMARY KEY REFERENCES cinema_links(id) ON DELETE CASCADE,
      data TEXT NOT NULL, checked_at TEXT NOT NULL);
    """,
    """
    CREATE TABLE cinema_playback_state (
      episode_id TEXT PRIMARY KEY REFERENCES cinema_episodes(id) ON DELETE CASCADE,
      link_id TEXT REFERENCES cinema_links(id) ON DELETE SET NULL,
      confirmed INTEGER NOT NULL DEFAULT 0, voice TEXT NOT NULL DEFAULT '',
      position REAL NOT NULL DEFAULT 0, duration REAL NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL);
    CREATE TABLE cinema_diagnostics (
      id INTEGER PRIMARY KEY, title_id TEXT, episode_id TEXT, session TEXT NOT NULL,
      stage TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL);
    ALTER TABLE cinema_source_memory ADD COLUMN last_voice TEXT NOT NULL DEFAULT '';
    """,
    """
    ALTER TABLE cinema_library ADD COLUMN manual_watched INTEGER;
    ALTER TABLE cinema_library ADD COLUMN watchlist INTEGER NOT NULL DEFAULT 0;
    UPDATE cinema_library SET watchlist=(status='planned'),
      manual_watched=CASE WHEN status='watched' THEN 1 END;
    """,
]


def now():
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class CinemaStore:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=3)
        self.db.row_factory = sqlite3.Row
        self._transaction_depth = 0
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS cinema_migrations (version INTEGER PRIMARY KEY)"
        )
        for version, script in enumerate(MIGRATIONS, 1):
            if not self.db.execute(
                "SELECT 1 FROM cinema_migrations WHERE version=?", (version,)
            ).fetchone():
                # One transaction including the migration marker, never PRAGMA user_version:
                # that belongs to the whole assistant database.
                with self.db:
                    self.db.execute("BEGIN IMMEDIATE")
                    for statement in script.split(";"):
                        if statement.strip():
                            self.db.execute(statement)
                    self.db.execute("INSERT INTO cinema_migrations VALUES (?)", (version,))

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        if self._transaction_depth:
            yield
            return
        self._transaction_depth += 1
        try:
            with self.db:
                yield
        finally:
            self._transaction_depth -= 1

    def export_library(self):
        from backend.cinema.transfer import export_library

        return export_library(self)

    def import_library(self, document):
        from backend.cinema.transfer import import_library

        return import_library(self, document)

    def import_preview(self, document):
        from backend.cinema.transfer import validate_document

        data = validate_document(document)
        return {
            "titles": len(data["items"]),
            "sources": len(data["sources"]),
            "events": len(data["history"]),
            "existing": len(self.library()),
        }

    def preferences(self):
        row = self.db.execute(
            "SELECT value FROM cinema_settings WHERE key='preferences'"
        ).fetchone()
        return Preferences.model_validate_json(row[0]) if row else Preferences()

    def configure(self, values):
        prefs = Preferences.model_validate({**self.preferences().model_dump(), **values})
        with self.transaction():
            self.db.execute(
                "INSERT OR REPLACE INTO cinema_settings VALUES ('preferences',?)",
                (prefs.model_dump_json(),),
            )
        return prefs.model_dump()

    def sources(self):
        sources = []
        for row in self.db.execute("SELECT data FROM cinema_sources ORDER BY rowid"):
            source = Source.model_validate_json(row[0]).model_dump()
            health = self.db.execute(
                "SELECT * FROM cinema_source_health WHERE source_id=?", (source["id"],)
            ).fetchone()
            source["health"] = dict(health) if health else {"status": "unknown"}
            sources.append(source)
        return sorted(sources, key=lambda s: s["priority"])

    def source(self, identifier):
        row = self.db.execute(
            "SELECT data FROM cinema_sources WHERE id=?", (identifier,)
        ).fetchone()
        if not row:
            raise ValueError("Источник не найден.")
        return Source.model_validate_json(row[0])

    def save_source(self, values):
        values = dict(values)
        values.pop("health", None)
        values.pop("capabilities", None)
        source = Source.model_validate(values)
        source.id = source.id or uuid4().hex
        known = self.db.execute("SELECT 1 FROM cinema_sources WHERE id=?", (source.id,)).fetchone()
        previous = self.source(source.id) if known else None
        if previous and (
            (previous.adapter == "local") != (source.adapter == "local")
            or (source.adapter != "local" and origin(previous.base_url) != origin(source.base_url))
        ):
            raise ValueError(
                "Добавь другой сайт как новый источник, чтобы сохранить старые ссылки."
            )
        with self.transaction():
            if source.preferred:
                for existing in self.sources():
                    if existing["preferred"] and existing["id"] != source.id:
                        old = self.source(existing["id"])
                        old.preferred = False
                        self.db.execute(
                            "UPDATE cinema_sources SET data=? WHERE id=?",
                            (old.model_dump_json(), old.id),
                        )
            self.db.execute(
                "INSERT INTO cinema_sources VALUES (?,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (source.id, source.model_dump_json()),
            )
        return source.model_dump()

    def reorder_sources(self, identifiers):
        if not isinstance(identifiers, list) or set(identifiers) != {
            s["id"] for s in self.sources()
        }:
            raise ValueError("Нужен полный список источников.")
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Повторный источник.")
        with self.transaction():
            for priority, identifier in enumerate(identifiers):
                source = self.source(identifier)
                source.priority = priority
                self.db.execute(
                    "UPDATE cinema_sources SET data=? WHERE id=?",
                    (source.model_dump_json(), identifier),
                )

    def delete_source(self, identifier):
        source = self.source(identifier)
        with self.transaction():
            self.db.execute(
                "UPDATE cinema_history SET source_label=? WHERE link_id IN "
                "(SELECT id FROM cinema_links WHERE source_id=?)",
                (source.name, identifier),
            )
            self.db.execute(
                "UPDATE cinema_source_memory SET page_url='' WHERE last_source_id=?", (identifier,)
            )
            self.db.execute(
                "UPDATE cinema_history SET link_id=NULL WHERE link_id IN "
                "(SELECT id FROM cinema_links WHERE source_id=?)",
                (identifier,),
            )
            self.db.execute(
                "UPDATE cinema_episodes SET last_link_id=NULL WHERE last_link_id IN "
                "(SELECT id FROM cinema_links WHERE source_id=?)",
                (identifier,),
            )
            self.db.execute("DELETE FROM cinema_links WHERE source_id=?", (identifier,))
            self.db.execute("DELETE FROM cinema_sources WHERE id=?", (identifier,))

    def source_health(self, identifier, status, reason="", *, playback=False):
        self.source(identifier)
        if status not in {"available", "unavailable", "restricted", "unknown"}:
            raise ValueError("Неверный статус источника.")
        with self.transaction():
            self.db.execute(
                "INSERT INTO cinema_source_health"
                "(source_id,status,checked_at,last_success,reason,successes) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
                "status=excluded.status,checked_at=excluded.checked_at,reason=excluded.reason,"
                "last_success=COALESCE(excluded.last_success,cinema_source_health.last_success),"
                "successes=cinema_source_health.successes+excluded.successes",
                (
                    identifier,
                    status,
                    now(),
                    now() if status == "available" else None,
                    reason,
                    int(playback),
                ),
            )

    def remember_source(self, title_id, source_id, episode_id=None, link_id=None, *, pin=False):
        item = self.detail(title_id)
        source = self.source(source_id) if source_id else None
        ep = next((e for e in item["episodes"] if e["id"] == episode_id), item["current"])
        if episode_id and ep["id"] != episode_id:
            raise ValueError("Серия не найдена.")
        link = next((link for link in ep["links"] if link["id"] == link_id), None)
        if link_id and (not link or link["source_id"] != source_id):
            raise ValueError("Источник и ссылка не совпадают.")
        with self.transaction():
            self.db.execute(
                "INSERT INTO cinema_source_memory"
                "(title_id,last_source_id,page_url,episode_id,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(title_id) DO UPDATE SET "
                "last_source_id=excluded.last_source_id,page_url=excluded.page_url,"
                "episode_id=excluded.episode_id,updated_at=excluded.updated_at",
                (
                    title_id,
                    source.id if source else None,
                    link["url"] if link else "",
                    ep["id"],
                    now(),
                ),
            )
            if pin:
                self.db.execute(
                    "UPDATE cinema_source_memory SET pinned_source_id=? WHERE title_id=?",
                    (source_id or None, title_id),
                )
            if link:
                self.db.execute(
                    "UPDATE cinema_source_memory SET last_voice=? WHERE title_id=?",
                    (link["voice"], title_id),
                )
            if link:
                self.select(ep["id"], link["id"])

    def feedback(self, key):
        if not isinstance(key, str) or len(key) > 160:
            raise ValueError("Неверное название рекомендации.")
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO cinema_feedback VALUES (?,?)", (key, now()))

    def unpin_source(self, title_id):
        self.title(title_id)
        with self.transaction():
            self.db.execute(
                "UPDATE cinema_source_memory SET pinned_source_id=NULL WHERE title_id=?",
                (title_id,),
            )

    def link_check(self, link_id, status, playable=False):
        with self.transaction():
            self.db.execute(
                "INSERT OR REPLACE INTO cinema_link_checks VALUES (?,?,?,?)",
                (link_id, status, now(), bool(playable)),
            )

    def verify_link(self, link_id, data=None):
        if data is not None:
            with self.transaction():
                self.db.execute(
                    "INSERT OR REPLACE INTO cinema_link_verification VALUES (?,?,?)",
                    (link_id, json.dumps(data, ensure_ascii=False), now()),
                )
        row = self.db.execute(
            "SELECT * FROM cinema_link_verification WHERE link_id=?", (link_id,)
        ).fetchone()
        return {**json.loads(row["data"]), "checked_at": row["checked_at"]} if row else {}

    def diagnostic(self, stage, status, message, title_id="", episode_id="", session=""):
        if stage not in {
            "SEARCH",
            "VERIFY",
            "OPEN_SAFARI",
            "DETECT_PLAYER",
            "RESTORE_POSITION",
            "PLAY",
            "SAVE_PROGRESS",
        }:
            raise ValueError("Неизвестный этап воспроизведения.")
        with self.transaction():
            self.db.execute(
                "INSERT INTO "
                "cinema_diagnostics(title_id,episode_id,session,stage,status,message,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (title_id, episode_id, session, stage, status, str(message)[:600], now()),
            )
            self.db.execute(
                "DELETE FROM cinema_diagnostics WHERE id NOT IN (SELECT id FROM "
                "cinema_diagnostics ORDER BY id DESC LIMIT 300)"
            )

    def diagnostics(self, title_id=""):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM cinema_diagnostics WHERE (?='' OR title_id=?) ORDER BY id "
                "DESC LIMIT 40",
                (title_id, title_id),
            )
        ]

    def playback_state(self, episode_id, link_id, position, duration, confirmed):
        link = self.db.execute(
            "SELECT voice FROM cinema_links WHERE id=? AND episode_id=?", (link_id, episode_id)
        ).fetchone()
        if not link:
            raise ValueError("Источник не принадлежит серии.")
        with self.transaction():
            self.db.execute(
                "INSERT OR REPLACE INTO cinema_playback_state VALUES (?,?,?,?,?,?,?)",
                (episode_id, link_id, bool(confirmed), link["voice"], position, duration, now()),
            )

    def reset_recommendations(self):
        with self.transaction():
            self.db.execute("DELETE FROM cinema_feedback")
            self.db.execute(
                "INSERT OR REPLACE INTO cinema_settings VALUES ('recommendation_reset_at',?)",
                (now(),),
            )

    def recommendation_data(self):
        reset = self.db.execute(
            "SELECT value FROM cinema_settings WHERE key='recommendation_reset_at'"
        ).fetchone()
        return {
            "dismissed": [
                r[0] for r in self.db.execute("SELECT catalogue_key FROM cinema_feedback")
            ],
            "reset_at": reset[0] if reset else "",
        }

    def ready_keys(self):
        from pathlib import Path

        sources = {s["id"]: s for s in self.sources() if s["enabled"]}
        keys = []
        prefs = self.preferences()
        for item in self.library():
            for ep in item["episodes"]:
                for link in ep["links"]:
                    source = sources.get(link["source_id"])
                    if not source:
                        continue
                    if (
                        prefs.free_only
                        and source["adapter"] != "local"
                        and source["access_type"] != "free"
                    ):
                        continue
                    checked = self.db.execute(
                        "SELECT * FROM cinema_link_checks WHERE link_id=?", (link["id"],)
                    ).fetchone()
                    recent = (
                        checked
                        and (
                            datetime.now(UTC) - datetime.fromisoformat(checked["checked_at"])
                        ).total_seconds()
                        < 300
                    )
                    if (source["adapter"] == "local" and Path(link["url"]).is_file()) or (
                        recent and checked["status"] == "available" and checked["playable"]
                    ):
                        keys.append(item["catalogue_key"] or f"local:{item['id']}")
        return keys

    def analytics(self):
        from backend.cinema.personal import profile

        events = [
            dict(r)
            for r in self.db.execute(
                "SELECT h.created_at,e.title_id FROM cinema_history h "
                "JOIN cinema_episodes e ON e.id=h.episode_id ORDER BY h.id"
            )
        ]
        ratings = [
            dict(r) for r in self.db.execute("SELECT * FROM cinema_rating_events ORDER BY id")
        ]
        return profile(self.library(), events, ratings)

    def add(self, values):
        values = dict(values)
        metadata = Metadata.model_validate(values.pop("metadata", {}))
        title_id = values.pop("title_id", None)
        entry = Entry.model_validate(values)
        if entry.source_id:
            source = self.source(entry.source_id)
            if source.adapter == "local":
                if not entry.url.startswith("/"):
                    raise ValueError("Нужен путь к локальному видео.")
            elif entry.url.startswith("/") or origin(entry.url) != origin(source.base_url):
                raise ValueError("Страница должна принадлежать выбранному источнику.")
        identity = f"{entry.kind}:{entry.year}:" + re.sub(r"[\W_]+", "", entry.title.casefold())
        with self.transaction():
            row = self.db.execute(
                "SELECT id FROM cinema_titles WHERE catalogue_key!='' AND catalogue_key=?",
                (entry.catalogue_key,),
            ).fetchone()
            if not row:
                # Legacy/manual titles have a normalized name identity. Only merge
                # that fallback if no conflicting TMDB identity is known.
                candidates = self.db.execute(
                    "SELECT id,title,catalogue_key FROM cinema_titles WHERE kind=? AND year=?",
                    (entry.kind, entry.year),
                )
                row = next(
                    (
                        (candidate["id"],)
                        for candidate in candidates
                        if (not entry.catalogue_key or not candidate["catalogue_key"])
                        and re.sub(r"[\W_]+", "", candidate["title"].casefold())
                        == re.sub(r"[\W_]+", "", entry.title.casefold())
                    ),
                    None,
                )
            if title_id:
                existing = self.title(title_id)
                if existing["kind"] != entry.kind:
                    raise ValueError("Тип привязки должен совпадать с фильмом или сериалом.")
                row = (title_id,)
            title_id = row[0] if row else uuid4().hex
            if not row:
                self.db.execute(
                    "INSERT INTO cinema_titles (id,identity,title,kind,year,poster_url,description,"
                    "catalogue_key,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        title_id,
                        entry.catalogue_key or identity,
                        entry.title,
                        entry.kind,
                        entry.year,
                        entry.poster_url,
                        entry.description,
                        entry.catalogue_key,
                        now(),
                    ),
                )
                self.db.execute(
                    "INSERT INTO cinema_library(title_id,added_at,updated_at,watchlist) "
                    "VALUES (?,?,?,1)",
                    (title_id, now(), now()),
                )
            if metadata.model_fields_set:
                # Metadata enrichment never changes status, history or playback position.
                self.db.execute(
                    "INSERT OR REPLACE INTO cinema_metadata VALUES (?,?)",
                    (title_id, metadata.model_dump_json()),
                )
                self.db.execute(
                    "UPDATE cinema_titles SET poster_url=?,description=?,catalogue_key=? "
                    "WHERE id=?",
                    (entry.poster_url, entry.description, entry.catalogue_key, title_id),
                )
            self.db.execute(
                "INSERT OR IGNORE INTO cinema_episodes(id,title_id,season,episode) "
                "VALUES (?,?,?,?)",
                (uuid4().hex, title_id, entry.season, entry.episode),
            )
            episode_id = self.db.execute(
                "SELECT id FROM cinema_episodes WHERE title_id=? AND season=? AND episode=?",
                (title_id, entry.season, entry.episode),
            ).fetchone()[0]
            if not entry.source_id:
                return self.detail(title_id)
            link = self.db.execute(
                "SELECT id FROM cinema_links WHERE episode_id=? AND source_id=? AND voice=?",
                (episode_id, entry.source_id, entry.voice),
            ).fetchone()
            link_id = link[0] if link else uuid4().hex
            self.db.execute(
                "INSERT INTO cinema_links VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET url=excluded.url",
                (link_id, episode_id, entry.source_id, entry.url, entry.voice),
            )
        return self.detail(title_id)

    def title(self, identifier):
        row = self.db.execute(
            "SELECT t.*,u.status AS user_status,u.favorite AS user_favorite,"
            "u.user_rating,u.user_note,u.completed_at,u.added_at,u.favorite_added_at,"
            "u.manual_watched,u.watchlist,"
            "u.updated_at AS library_updated_at FROM cinema_titles t "
            "JOIN cinema_library u ON u.title_id=t.id WHERE t.id=?",
            (identifier,),
        ).fetchone()
        if not row:
            raise ValueError("Фильм или сериал не найден.")
        item = dict(row)
        item["status"] = item.pop("user_status")
        item["favorite"] = item.pop("user_favorite")
        meta = self.db.execute(
            "SELECT data FROM cinema_metadata WHERE title_id=?", (identifier,)
        ).fetchone()
        item["metadata"] = json.loads(meta[0]) if meta else Metadata().model_dump()
        rated = self.db.execute(
            "SELECT MAX(created_at) FROM cinema_rating_events WHERE title_id=?", (identifier,)
        ).fetchone()
        item["rating_updated_at"] = rated[0]
        memory = self.db.execute(
            "SELECT * FROM cinema_source_memory WHERE title_id=?", (identifier,)
        ).fetchone()
        item["source_memory"] = dict(memory) if memory else {}
        return item

    def delete(self, identifier):
        self.title(identifier)
        with self.transaction():
            self.db.execute(
                "DELETE FROM cinema_history WHERE episode_id IN "
                "(SELECT id FROM cinema_episodes WHERE title_id=?)",
                (identifier,),
            )
            self.db.execute("DELETE FROM cinema_titles WHERE id=?", (identifier,))

    def replay(self, identifier):
        self.title(identifier)
        with self.transaction():
            self.db.execute(
                "UPDATE cinema_episodes SET position=0,watched=0 WHERE title_id=?", (identifier,)
            )
            self.db.execute(
                "UPDATE cinema_titles SET last_episode_id=NULL WHERE id=?", (identifier,)
            )
            self.update_title(identifier, {"status": "watching"})
        return self.detail(identifier)

    def season(self, identifier, episodes):
        item = self.title(identifier)
        if item["kind"] != "series":
            raise ValueError("Сезоны доступны только сериалам.")
        with self.transaction():
            for ep in episodes:
                entry = Entry(title=item["title"], season=ep["season"], episode=ep["episode"])
                self.db.execute(
                    "INSERT INTO cinema_episodes(id,title_id,season,episode,name) "
                    "VALUES (?,?,?,?,?) "
                    "ON CONFLICT(title_id,season,episode) DO UPDATE SET name=excluded.name",
                    (
                        uuid4().hex,
                        identifier,
                        entry.season,
                        entry.episode,
                        str(ep.get("name", ""))[:200],
                    ),
                )
        return self.detail(identifier)

    def options(self, title_id, episode_id=None):
        from pathlib import Path

        item = self.detail(title_id)
        ep = next(
            (ep for ep in item["episodes"] if ep["id"] == episode_id),
            item["current"] if episode_id is None else None,
        )
        if not ep:
            raise ValueError("Серия не найдена.")
        options = []
        for source in self.sources():
            links = [link for link in ep["links"] if link["source_id"] == source["id"]]
            for link in links or [None]:
                available = bool(source["enabled"] and link)
                reason = (
                    ""
                    if available
                    else "Источник отключён."
                    if not source["enabled"]
                    else "Привяжи страницу фильма или файл один раз."
                )
                if available and source["adapter"] == "local" and not Path(link["url"]).is_file():
                    available, reason = False, "Видеофайл недоступен. Проверь путь."
                options.append(
                    {"source": source, "link": link, "available": available, "reason": reason}
                )
        return {"title_id": title_id, "episode_id": ep["id"], "options": options}

    def detail(self, identifier):
        item = self.title(identifier)
        episodes = []
        for row in self.db.execute(
            "SELECT * FROM cinema_episodes WHERE title_id=? ORDER BY season,episode", (identifier,)
        ):
            episode = dict(row)
            state = self.db.execute(
                "SELECT * FROM cinema_playback_state WHERE episode_id=?", (row["id"],)
            ).fetchone()
            episode["playback_state"] = dict(state) if state else None
            episode["links"] = [
                {**dict(link), "source_name": self.source(link["source_id"]).name}
                for link in self.db.execute(
                    "SELECT * FROM cinema_links WHERE episode_id=?", (row["id"],)
                )
            ]
            episodes.append(episode)
        item["episodes"] = episodes
        item["current"] = next(
            (ep for ep in episodes if ep["id"] == item["last_episode_id"]),
            next((ep for ep in episodes if not ep["watched"]), episodes[0] if episodes else None),
        )
        return item

    def library(self, query=""):
        rows = self.db.execute("SELECT id FROM cinema_titles ORDER BY updated_at DESC").fetchall()
        return [
            item
            for row in rows
            if query.casefold() in (item := self.detail(row[0]))["title"].casefold()
        ]

    def recommendation_context(self, kind="all"):
        rows = self.db.execute(
            "SELECT t.catalogue_key,t.kind,t.title,t.year,u.status,u.favorite,"
            "u.user_rating,t.updated_at "
            "FROM cinema_titles t JOIN cinema_library u ON u.title_id=t.id "
            "ORDER BY u.favorite DESC,"
            "COALESCE(u.user_rating,0) DESC,t.updated_at DESC"
        ).fetchall()
        seeds = [
            r["catalogue_key"]
            for r in rows
            if r["catalogue_key"].startswith("tmdb:")
            and (kind == "all" or r["kind"] == kind)
            and (
                r["favorite"]
                or (r["user_rating"] or 0) >= 7
                or r["status"] in {"watching", "watched"}
            )
            and (r["user_rating"] is None or r["user_rating"] >= 7)
        ][:2]
        excluded = [r["catalogue_key"] for r in rows if r["status"] == "watched"]
        excluded_titles = [
            (r["kind"], " ".join(r["title"].casefold().split()), r["year"])
            for r in rows
            if r["status"] == "watched"
        ]
        return {"seeds": seeds, "excluded": excluded, "excluded_titles": excluded_titles}

    def latest(self, kind=None, unfinished=False):
        row = self.db.execute(
            "SELECT e.title_id FROM cinema_episodes e JOIN cinema_titles t ON t.id=e.title_id "
            "JOIN cinema_library u ON u.title_id=t.id WHERE e.last_viewed IS NOT NULL "
            "AND (?=0 OR u.status='watching') AND (? IS NULL OR t.kind=?) "
            "ORDER BY e.last_viewed DESC LIMIT 1",
            (unfinished, kind, kind),
        ).fetchone()
        if not row:
            raise ValueError("Ещё нет истории. Добавь фильм или сериал в разделе «Кино».")
        return self.detail(row[0])

    def target(self, title_id=None, episode_id=None, link_id=None):
        item = self.detail(title_id) if title_id else self.latest()
        episode = next(
            (ep for ep in item["episodes"] if ep["id"] == episode_id),
            item["current"] if episode_id is None else None,
        )
        if episode is None:
            raise ValueError("Серия не найдена.")
        link = next(
            (v for v in episode["links"] if v["id"] == (link_id or episode["last_link_id"])),
            episode["links"][0] if not link_id and episode["links"] else None,
        )
        if not link:
            raise ValueError("Добавь страницу этой серии и источник.")
        source = self.source(link["source_id"])
        if not source.enabled:
            raise ValueError("Этот источник отключён.")
        return {"item": item, "episode": episode, "link": link, "source": source.model_dump()}

    def next_target(self, title_id, episode_id):
        episodes = self.detail(title_id)["episodes"]
        index = next((i for i, ep in enumerate(episodes) if ep["id"] == episode_id), -1)
        if index < 0 or index + 1 >= len(episodes):
            raise ValueError(
                "Следующая серия ещё не добавлена. Укажи её страницу в карточке сериала."
            )
        return self.target(title_id, episodes[index + 1]["id"])

    def update_title(self, identifier, values):
        self.title(identifier)
        if (
            set(values) - {"status", "favorite", "user_rating", "user_note", "watched", "watchlist"}
            or not values
        ):
            raise ValueError("Нельзя изменить эти поля.")
        values = dict(values)
        if "watched" in values and "status" in values:
            raise ValueError("Укажи либо ручную отметку, либо статус.")
        for key in ("watched", "watchlist"):
            if key in values and type(values[key]) is not bool:
                raise ValueError("Отметка должна быть логическим значением.")
        if "status" in values and values["status"] not in {"watching", "planned", "watched"}:
            raise ValueError("Неизвестный статус.")
        if "favorite" in values and type(values["favorite"]) is not bool:
            raise ValueError("Избранное должно быть логическим значением.")
        if "user_note" in values and (
            not isinstance(values["user_note"], str) or len(values["user_note"]) > 2000
        ):
            raise ValueError("Заметка должна быть текстом до 2000 символов.")
        if (
            "user_rating" in values
            and values["user_rating"] is not None
            and (
                type(values["user_rating"]) not in {int, float}
                or not math.isfinite(values["user_rating"])
                or not 1 <= values["user_rating"] <= 10
                or values["user_rating"] * 2 != int(values["user_rating"] * 2)
            )
        ):
            raise ValueError("Оценка должна быть от 1 до 10 с шагом 0.5.")
        with self.transaction():
            if "watched" in values:
                watched = values.pop("watched")
                if watched:
                    values["watchlist"] = False
                started = self.db.execute(
                    "SELECT 1 FROM cinema_episodes WHERE title_id=? "
                    "AND (last_viewed IS NOT NULL OR position>0 OR watched=1) LIMIT 1",
                    (identifier,),
                ).fetchone()
                values["status"] = "watched" if watched else "watching" if started else "planned"
                self.db.execute(
                    "UPDATE cinema_library SET manual_watched=? WHERE title_id=?",
                    (watched, identifier),
                )
            elif "status" in values:
                # Existing explicit status controls also remain stable during playback.
                self.db.execute(
                    "UPDATE cinema_library SET manual_watched=?,watchlist=? WHERE title_id=?",
                    (
                        1 if values["status"] == "watched" else None,
                        values["status"] == "planned",
                        identifier,
                    ),
                )
            if "user_rating" in values:
                previous = self.title(identifier).get("user_rating")
                if previous != values["user_rating"]:
                    self.db.execute(
                        "INSERT INTO cinema_rating_events(title_id,rating,created_at) "
                        "VALUES (?,?,?)",
                        (identifier, values["user_rating"], now()),
                    )
            if "favorite" in values:
                self.db.execute(
                    "UPDATE cinema_library SET favorite_added_at=CASE WHEN ? "
                    "THEN COALESCE(favorite_added_at,?) ELSE NULL END WHERE title_id=?",
                    (values["favorite"], now(), identifier),
                )
            for key, value in values.items():
                self.db.execute(
                    f"UPDATE cinema_library SET {key}=? WHERE title_id=?",
                    (value, identifier),
                )
            self.db.execute("UPDATE cinema_titles SET updated_at=? WHERE id=?", (now(), identifier))
            self.db.execute(
                "UPDATE cinema_library SET updated_at=? WHERE title_id=?", (now(), identifier)
            )
            if values.get("status") == "watched":
                self.db.execute(
                    "UPDATE cinema_library SET completed_at=? WHERE title_id=?",
                    (now(), identifier),
                )
        return self.detail(identifier)

    def select(self, episode_id, link_id):
        row = self.db.execute(
            "SELECT e.title_id,l.source_id,l.url,l.voice FROM cinema_episodes e "
            "JOIN cinema_links l ON l.episode_id=e.id "
            "WHERE e.id=? AND l.id=?",
            (episode_id, link_id),
        ).fetchone()
        if not row:
            raise ValueError("Серия и источник не совпадают.")
        with self.transaction():
            self.db.execute(
                "UPDATE cinema_episodes SET last_link_id=? WHERE id=?", (link_id, episode_id)
            )
            self.db.execute(
                "UPDATE cinema_titles SET last_episode_id=? WHERE id=?", (episode_id, row[0])
            )
            self.db.execute(
                "INSERT INTO cinema_source_memory"
                "(title_id,last_source_id,page_url,episode_id,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(title_id) DO UPDATE SET "
                "last_source_id=excluded.last_source_id,page_url=excluded.page_url,"
                "episode_id=excluded.episode_id,updated_at=excluded.updated_at",
                (row[0], row["source_id"], row["url"], episode_id, now()),
            )
            self.db.execute(
                "UPDATE cinema_source_memory SET last_voice=? WHERE title_id=?",
                (row["voice"], row[0]),
            )

    def progress(self, episode_id, position, duration, event="manual", link_id=None, watched=None):
        if watched is not None and type(watched) is not bool:
            raise ValueError("Статус серии должен быть логическим значением.")
        if event not in {
            "manual",
            "periodic",
            "pause",
            "ended",
            "switch",
            "close",
            "seek",
            "playing",
        }:
            raise ValueError("Неизвестное событие плеера.")
        if any(
            type(v) not in (float, int) or not math.isfinite(v) or v < 0
            for v in (position, duration)
        ):
            raise ValueError("Время должно быть конечным неотрицательным числом секунд.")
        if duration and position > duration + 2:
            raise ValueError("Позиция превышает длительность.")
        ep = self.db.execute("SELECT * FROM cinema_episodes WHERE id=?", (episode_id,)).fetchone()
        if not ep:
            raise ValueError("Серия не найдена.")
        link_id = link_id or ep["last_link_id"]
        if (
            link_id
            and not self.db.execute(
                "SELECT 1 FROM cinema_links WHERE id=? AND episode_id=?", (link_id, episode_id)
            ).fetchone()
        ):
            raise ValueError("Источник не принадлежит этой серии.")
        completed = bool(duration and position / duration >= self.preferences().watched_threshold)
        # Automatic sampling must never unmark a completed episode while credits loop.
        complete = (
            (watched or completed) if watched is not None else (completed or bool(ep["watched"]))
        )
        stamp = now()
        with self.transaction():
            self.db.execute(
                "UPDATE cinema_episodes SET position=?,duration=?,watched=?,last_viewed=?,"
                "last_link_id=COALESCE(?,last_link_id) WHERE id=?",
                (
                    min(position, duration) if duration else position,
                    duration,
                    complete,
                    stamp,
                    link_id,
                    episode_id,
                ),
            )
            # Finishing one episode never marks the whole series as watched.
            title = self.title(ep["title_id"])
            # A title-level manual mark never rewrites individual episode progress.
            # Explicit removal also survives further samples of completed media.
            override = title["manual_watched"]
            status = (
                "watched"
                if override == 1
                else "watching"
                if override == 0
                else "watched"
                if title["kind"] == "movie" and complete
                else "watching"
            )
            self.db.execute(
                "UPDATE cinema_titles SET last_episode_id=?,updated_at=? WHERE id=?",
                (episode_id, stamp, ep["title_id"]),
            )
            self.db.execute(
                "UPDATE cinema_library SET status=?,updated_at=?,"
                "completed_at=COALESCE(?,completed_at) "
                "WHERE title_id=?",
                (
                    status,
                    stamp,
                    stamp if status == "watched" and override is None else None,
                    ep["title_id"],
                ),
            )
            self.db.execute(
                "INSERT INTO cinema_history(episode_id,link_id,position,duration,event,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (episode_id, link_id, position, duration, event, stamp),
            )
            # Bound the periodic journal; per-episode progress remains durable.
            self.db.execute(
                "DELETE FROM cinema_history WHERE id < (SELECT MAX(id)-10000 FROM cinema_history)"
            )
        return self.detail(ep["title_id"])

    def history(self):
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT h.*,t.title,t.kind,e.title_id,e.season,e.episode,e.watched,"
                "s.data AS source_data FROM cinema_history h "
                "JOIN cinema_episodes e ON e.id=h.episode_id "
                "JOIN cinema_titles t ON t.id=e.title_id "
                "LEFT JOIN cinema_links l ON l.id=h.link_id "
                "LEFT JOIN cinema_sources s ON s.id=l.source_id ORDER BY h.id DESC LIMIT 200"
            )
        ]

    def snapshot(self):
        history = self.history()
        for row in history:
            label = row.pop("source_label", "")
            row["source_name"] = json.loads(row.pop("source_data") or "{}").get("name", "") or label
        return {
            "items": self.library(),
            "sources": self.sources(),
            "history": history,
            "preferences": self.preferences().model_dump(),
        }
