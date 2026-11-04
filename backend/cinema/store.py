"""Versioned cinema tables in the assistant's SQLite file, on a separate connection.

All service calls run on one worker; no SQLite I/O blocks STT or the HUD loop.
"""

import json
import math
import re
import sqlite3
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
]


def now():
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class CinemaStore:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=3)
        self.db.row_factory = sqlite3.Row
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

    def preferences(self):
        row = self.db.execute(
            "SELECT value FROM cinema_settings WHERE key='preferences'"
        ).fetchone()
        return Preferences.model_validate_json(row[0]) if row else Preferences()

    def configure(self, values):
        prefs = Preferences.model_validate({**self.preferences().model_dump(), **values})
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO cinema_settings VALUES ('preferences',?)",
                (prefs.model_dump_json(),),
            )
        return prefs.model_dump()

    def sources(self):
        return [
            Source.model_validate_json(row[0]).model_dump()
            for row in self.db.execute("SELECT data FROM cinema_sources ORDER BY rowid")
        ]

    def source(self, identifier):
        row = self.db.execute(
            "SELECT data FROM cinema_sources WHERE id=?", (identifier,)
        ).fetchone()
        if not row:
            raise ValueError("Источник не найден.")
        return Source.model_validate_json(row[0])

    def save_source(self, values):
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
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO cinema_sources VALUES (?,?)",
                (source.id, source.model_dump_json()),
            )
        return source.model_dump()

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
        with self.db:
            row = self.db.execute(
                "SELECT id FROM cinema_titles WHERE identity=? "
                "OR (catalogue_key!='' AND catalogue_key=?)",
                (identity, entry.catalogue_key),
            ).fetchone()
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
                        identity,
                        entry.title,
                        entry.kind,
                        entry.year,
                        entry.poster_url,
                        entry.description,
                        entry.catalogue_key,
                        now(),
                    ),
                )
                self.db.execute("INSERT INTO cinema_library(title_id) VALUES (?)", (title_id,))
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
            "u.user_rating,u.completed_at FROM cinema_titles t "
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
        return item

    def delete(self, identifier):
        self.title(identifier)
        with self.db:
            self.db.execute(
                "DELETE FROM cinema_history WHERE episode_id IN "
                "(SELECT id FROM cinema_episodes WHERE title_id=?)",
                (identifier,),
            )
            self.db.execute("DELETE FROM cinema_titles WHERE id=?", (identifier,))

    def replay(self, identifier):
        self.title(identifier)
        with self.db:
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
        with self.db:
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
        if set(values) - {"status", "favorite", "user_rating"} or not values:
            raise ValueError("Нельзя изменить эти поля.")
        if "status" in values and values["status"] not in {"watching", "planned", "watched"}:
            raise ValueError("Неизвестный статус.")
        if "favorite" in values and type(values["favorite"]) is not bool:
            raise ValueError("Избранное должно быть логическим значением.")
        if (
            "user_rating" in values
            and values["user_rating"] is not None
            and (type(values["user_rating"]) is not int or not 1 <= values["user_rating"] <= 10)
        ):
            raise ValueError("Оценка должна быть от 1 до 10.")
        with self.db:
            for key, value in values.items():
                self.db.execute(
                    f"UPDATE cinema_library SET {key}=? WHERE title_id=?",
                    (value, identifier),
                )
            self.db.execute("UPDATE cinema_titles SET updated_at=? WHERE id=?", (now(), identifier))
            if values.get("status") == "watched":
                self.db.execute(
                    "UPDATE cinema_library SET completed_at=? WHERE title_id=?",
                    (now(), identifier),
                )
        return self.detail(identifier)

    def select(self, episode_id, link_id):
        row = self.db.execute(
            "SELECT e.title_id FROM cinema_episodes e JOIN cinema_links l ON l.episode_id=e.id "
            "WHERE e.id=? AND l.id=?",
            (episode_id, link_id),
        ).fetchone()
        if not row:
            raise ValueError("Серия и источник не совпадают.")
        with self.db:
            self.db.execute(
                "UPDATE cinema_episodes SET last_link_id=? WHERE id=?", (link_id, episode_id)
            )
            self.db.execute(
                "UPDATE cinema_titles SET last_episode_id=? WHERE id=?", (episode_id, row[0])
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
        with self.db:
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
            status = "watched" if title["kind"] == "movie" and complete else "watching"
            self.db.execute(
                "UPDATE cinema_titles SET last_episode_id=?,updated_at=? WHERE id=?",
                (episode_id, stamp, ep["title_id"]),
            )
            self.db.execute(
                "UPDATE cinema_library SET status=?,completed_at=COALESCE(?,completed_at) "
                "WHERE title_id=?",
                (status, stamp if status == "watched" else None, ep["title_id"]),
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
            row["source_name"] = json.loads(row.pop("source_data") or "{}").get("name", "")
        return {
            "items": self.library(),
            "sources": self.sources(),
            "history": history,
            "preferences": self.preferences().model_dump(),
        }
