"""Versioned, validated cinema-only backups. Import merges without deleting data."""

import json
import math
import re
from datetime import datetime
from uuid import uuid4

from backend.cinema.models import Entry, Metadata, Preferences, Source, local_video, origin, web_url

LIMIT = 5_000_000
LIBRARY_FIELDS = (
    "status",
    "favorite",
    "user_rating",
    "user_note",
    "completed_at",
    "added_at",
    "library_updated_at",
    "favorite_added_at",
    "manual_watched",
    "watchlist",
)
ENTRY_FIELDS = ("title", "kind", "year", "poster_url", "description", "catalogue_key")
EVENTS = {"manual", "periodic", "pause", "ended", "switch", "close", "seek", "playing"}


def date(value, optional=True):
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > 50:
        raise ValueError("Некорректная дата в копии.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("В дате нужен часовой пояс.")
    return value


def number(value, maximum=604800):
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError("Некорректная позиция или длительность.")
    return value


def unique(rows):
    ids = [r["id"] for r in rows]
    if any(not isinstance(v, str) or not 1 <= len(v) <= 160 for v in ids) or len(ids) != len(
        set(ids)
    ):
        raise ValueError("Повторные или некорректные идентификаторы в копии.")


def export_library(store):
    from backend.cinema.store import now

    items = []
    for item in store.library():
        items.append(
            {
                key: item.get(key)
                for key in (
                    "id",
                    *ENTRY_FIELDS,
                    "metadata",
                    *LIBRARY_FIELDS,
                    "last_episode_id",
                    "episodes",
                    "source_memory",
                )
            }
        )
    return {
        "schema": "chushpan.cinema",
        "version": 1,
        "exported_at": now(),
        "items": items,
        "sources": [store.source(s["id"]).model_dump() for s in store.sources()],
        "history": [dict(r) for r in store.db.execute("SELECT * FROM cinema_history ORDER BY id")],
        "ratings": [
            dict(r) for r in store.db.execute("SELECT * FROM cinema_rating_events ORDER BY id")
        ],
        "preferences": store.preferences().model_dump(),
    }


def validate_document(document):
    try:
        return _validate_document(document)
    except (KeyError, TypeError, AttributeError, OverflowError):
        raise ValueError("Некорректная структура копии медиатеки.") from None


def _validate_document(document):
    if (
        not isinstance(document, dict)
        or document.get("schema") != "chushpan.cinema"
        or document.get("version") != 1
        or set(document)
        - {
            "schema",
            "version",
            "exported_at",
            "items",
            "sources",
            "history",
            "ratings",
            "preferences",
        }
    ):
        raise ValueError("Нужна копия медиатеки Чушпана версии 1.")
    if len(json.dumps(document, ensure_ascii=False).encode()) > LIMIT:
        raise ValueError("Размер копии должен быть до 5 МБ.")
    data = json.loads(json.dumps(document))
    for key, maximum in (
        ("items", 10000),
        ("sources", 500),
        ("history", 10000),
        ("ratings", 20000),
    ):
        if not isinstance(data.get(key), list) or len(data[key]) > maximum:
            raise ValueError("В копии слишком много записей.")
    data["preferences"] = Preferences.model_validate(data.get("preferences", {})).model_dump()
    data["sources"] = [Source.model_validate(s).model_dump() for s in data["sources"]]
    unique(data["sources"])
    unique(data["items"])
    sources = {s["id"]: s for s in data["sources"]}
    episode_ids, link_ids, identities = set(), set(), set()
    episode_owner, link_owner = {}, {}
    titles = {i["id"] for i in data["items"]}
    for item in data["items"]:
        clean = Entry.model_validate({key: item[key] for key in ENTRY_FIELDS}).model_dump()
        item.update({key: clean[key] for key in ENTRY_FIELDS})
        identity = item["catalogue_key"] or (
            item["kind"],
            item["year"],
            re.sub(r"[\W_]+", "", item["title"].casefold()),
        )
        if identity in identities:
            raise ValueError("В копии дублируется одно произведение.")
        identities.add(identity)
        item["metadata"] = Metadata.model_validate(item.get("metadata", {})).model_dump()
        if item["status"] not in {"planned", "watching", "watched"} or item["favorite"] not in (
            0,
            1,
            False,
            True,
        ):
            raise ValueError("Некорректный статус или избранное.")
        rating = item.get("user_rating")
        if rating is not None and (
            type(rating) not in {int, float}
            or not math.isfinite(rating)
            or not 1 <= rating <= 10
            or rating * 2 != int(rating * 2)
        ):
            raise ValueError("Некорректная оценка.")
        note = item.setdefault("user_note", "")
        item.setdefault("manual_watched", 1 if item["status"] == "watched" else None)
        item.setdefault("watchlist", int(item["status"] == "planned"))
        if item["manual_watched"] not in (None, 0, 1) or item["watchlist"] not in (0, 1):
            raise ValueError("Некорректная ручная отметка.")
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError("Слишком длинная заметка.")
        for key in ("completed_at", "added_at", "library_updated_at", "favorite_added_at"):
            date(item.get(key))
        if not isinstance(item.get("episodes"), list) or not 1 <= len(item["episodes"]) <= 3000:
            raise ValueError("Некорректный список эпизодов.")
        coordinates = set()
        for ep in item["episodes"]:
            unique([ep])
            if ep["id"] in episode_ids:
                raise ValueError("Повторная серия.")
            episode_ids.add(ep["id"])
            episode_owner[ep["id"]] = item["id"]
            coordinate = (ep["season"], ep["episode"])
            Entry(
                title=item["title"], kind=item["kind"], season=ep["season"], episode=ep["episode"]
            )
            if coordinate in coordinates or (item["kind"] == "movie" and coordinate != (0, 0)):
                raise ValueError("Повторная или неверная нумерация серии.")
            coordinates.add(coordinate)
            number(ep["position"])
            number(ep["duration"])
            if ep["duration"] and ep["position"] > ep["duration"] + 2:
                raise ValueError("Позиция выходит за пределы длительности.")
            if ep["watched"] not in (0, 1, False, True):
                raise ValueError("Некорректная отметка серии.")
            date(ep.get("last_viewed"))
            if not isinstance(ep.get("links"), list) or len(ep["links"]) > 1000:
                raise ValueError("Некорректные ссылки.")
            if not isinstance(ep.get("name", ""), str) or len(ep.get("name", "")) > 200:
                raise ValueError("Некорректное название серии.")
            link_keys = set()
            for link in ep["links"]:
                unique([link])
                if link["id"] in link_ids or link["source_id"] not in sources:
                    raise ValueError("Повторная ссылка или неизвестный источник.")
                link_ids.add(link["id"])
                link_owner[link["id"]] = ep["id"]
                link_key = (link["source_id"], link["voice"])
                if link_key in link_keys:
                    raise ValueError("Повторная озвучка источника у одной серии.")
                link_keys.add(link_key)
                source = sources[link["source_id"]]
                if source["adapter"] == "local":
                    local_video(link["url"])
                elif origin(web_url(link["url"])) != origin(source["base_url"]):
                    raise ValueError("Ссылка не принадлежит источнику.")
                if not isinstance(link.get("voice"), str) or len(link["voice"]) > 100:
                    raise ValueError("Некорректная озвучка.")
        memory = item.get("source_memory") or {}
        if any(
            memory.get(key) and memory[key] not in sources
            for key in ("last_source_id", "pinned_source_id")
        ):
            raise ValueError("Неизвестный источник продолжения.")
        for ep in item["episodes"]:
            if ep.get("last_link_id") and link_owner.get(ep["last_link_id"]) != ep["id"]:
                raise ValueError("Последняя ссылка не принадлежит серии.")
    for item in data["items"]:
        if item.get("last_episode_id") and episode_owner.get(item["last_episode_id"]) != item["id"]:
            raise ValueError("Текущая серия относится к другому названию.")
        memory = item.get("source_memory") or {}
        if memory.get("episode_id") and episode_owner.get(memory["episode_id"]) != item["id"]:
            raise ValueError("Источник продолжения относится к другой серии.")
    for event in data["history"]:
        if event["episode_id"] not in episode_ids or event["event"] not in EVENTS:
            raise ValueError("История относится к неизвестной серии.")
        if event.get("link_id") and link_owner.get(event["link_id"]) != event["episode_id"]:
            raise ValueError("История относится к неизвестной ссылке.")
        number(event["position"])
        number(event["duration"])
        date(event["created_at"], False)
        if (
            not isinstance(event.get("source_label", ""), str)
            or len(event.get("source_label", "")) > 80
        ):
            raise ValueError("Некорректное имя источника в истории.")
    for event in data["ratings"]:
        if event["title_id"] not in titles:
            raise ValueError("Оценка относится к неизвестному названию.")
        rating = event["rating"]
        if rating is not None and (
            type(rating) not in {int, float}
            or not math.isfinite(rating)
            or not 1 <= rating <= 10
            or rating * 2 != int(rating * 2)
        ):
            raise ValueError("Некорректная история оценок.")
        date(event["created_at"], False)
    return data


def import_library(store, document):
    from backend.cinema.store import now

    data = validate_document(document)  # No writes before all records are validated.
    old_titles = {i["id"] for i in store.library()}
    had_sources = bool(store.sources())
    source_map, title_map, episode_map, link_map = {}, {}, {}, {}
    new_titles = set()
    with store.transaction():
        for source in data["sources"]:
            existing = next(
                (
                    s
                    for s in store.sources()
                    if s["adapter"] == source["adapter"]
                    and (
                        source["adapter"] == "local"
                        or origin(s["base_url"]) == origin(source["base_url"])
                    )
                ),
                None,
            )
            saved = existing or store.save_source(
                {
                    **source,
                    "id": "",
                    "preferred": source["preferred"] if not had_sources else False,
                    "priority": len(store.sources()) if had_sources else source["priority"],
                }
            )
            source_map[source["id"]] = saved["id"]
        for item in data["items"]:
            entry = {key: item[key] for key in ENTRY_FIELDS}
            entry.update(
                season=item["episodes"][0]["season"], episode=item["episodes"][0]["episode"]
            )
            saved = store.add(entry)
            identifier = saved["id"]
            title_map[item["id"]] = identifier
            fresh = identifier not in old_titles
            if fresh:
                new_titles.add(identifier)
                store.add({**entry, "title_id": identifier, "metadata": item["metadata"]})
                store.db.execute(
                    "UPDATE cinema_library SET status=?,favorite=?,user_rating=?,user_note=?,"
                    "completed_at=?,added_at=?,updated_at=?,favorite_added_at=?,"
                    "manual_watched=?,watchlist=? WHERE title_id=?",
                    (
                        item["status"],
                        bool(item["favorite"]),
                        item["user_rating"],
                        item["user_note"],
                        item.get("completed_at"),
                        item.get("added_at") or now(),
                        item.get("library_updated_at") or now(),
                        item.get("favorite_added_at"),
                        item["manual_watched"],
                        item["watchlist"],
                        identifier,
                    ),
                )
            for ep in item["episodes"]:
                current = store.db.execute(
                    "SELECT id FROM cinema_episodes WHERE title_id=? AND season=? AND episode=?",
                    (identifier, ep["season"], ep["episode"]),
                ).fetchone()
                fresh_episode = current is None or fresh
                ep_id = current[0] if current else uuid4().hex
                store.db.execute(
                    "INSERT OR IGNORE INTO cinema_episodes(id,title_id,season,episode,name) "
                    "VALUES (?,?,?,?,?)",
                    (ep_id, identifier, ep["season"], ep["episode"], str(ep.get("name", ""))[:200]),
                )
                episode_map[ep["id"]] = ep_id
                for link in ep["links"]:
                    source_id = source_map[link["source_id"]]
                    current_link = store.db.execute(
                        "SELECT id FROM cinema_links "
                        "WHERE episode_id=? AND source_id=? AND voice=?",
                        (ep_id, source_id, link["voice"]),
                    ).fetchone()
                    link_id = current_link[0] if current_link else uuid4().hex
                    store.db.execute(
                        "INSERT OR IGNORE INTO cinema_links VALUES (?,?,?,?,?)",
                        (link_id, ep_id, source_id, link["url"], link["voice"]),
                    )
                    link_map[link["id"]] = link_id
                if fresh_episode:
                    store.db.execute(
                        "UPDATE cinema_episodes SET position=?,duration=?,watched=?,"
                        "last_viewed=?,last_link_id=? WHERE id=?",
                        (
                            ep["position"],
                            ep["duration"],
                            bool(ep["watched"]),
                            ep.get("last_viewed"),
                            link_map.get(ep.get("last_link_id")),
                            ep_id,
                        ),
                    )
            if fresh:
                store.db.execute(
                    "UPDATE cinema_titles SET last_episode_id=? WHERE id=?",
                    (episode_map.get(item.get("last_episode_id")), identifier),
                )
                memory = item.get("source_memory") or {}
                if memory.get("last_source_id"):
                    ep_id = (
                        episode_map.get(memory.get("episode_id"))
                        or store.detail(identifier)["current"]["id"]
                    )
                    ep = next(e for e in store.detail(identifier)["episodes"] if e["id"] == ep_id)
                    link = next(
                        (
                            link
                            for link in ep["links"]
                            if link["source_id"] == source_map[memory["last_source_id"]]
                            and (
                                link["id"] == ep["last_link_id"]
                                or link["url"] == memory.get("page_url")
                            )
                        ),
                        next(
                            (
                                link
                                for link in ep["links"]
                                if link["source_id"] == source_map[memory["last_source_id"]]
                            ),
                            None,
                        ),
                    )
                    store.remember_source(
                        identifier,
                        source_map[memory["last_source_id"]],
                        ep_id,
                        link["id"] if link else None,
                    )
                    if memory.get("pinned_source_id"):
                        store.db.execute(
                            "UPDATE cinema_source_memory SET pinned_source_id=? WHERE title_id=?",
                            (source_map[memory["pinned_source_id"]], identifier),
                        )
        for event in data["history"]:
            values = (
                episode_map[event["episode_id"]],
                link_map.get(event.get("link_id")),
                event["position"],
                event["duration"],
                event["event"],
                event["created_at"],
            )
            if not store.db.execute(
                "SELECT 1 FROM cinema_history WHERE episode_id=? AND link_id IS ? "
                "AND position=? AND duration=? AND event=? AND created_at=?",
                values,
            ).fetchone():
                store.db.execute(
                    "INSERT INTO cinema_history"
                    "(episode_id,link_id,position,duration,event,created_at,source_label) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (*values, event.get("source_label", "")),
                )
        for event in data["ratings"]:
            if title_map[event["title_id"]] in new_titles:
                store.db.execute(
                    "INSERT INTO cinema_rating_events(title_id,rating,created_at) VALUES (?,?,?)",
                    (title_map[event["title_id"]], event["rating"], event["created_at"]),
                )
        if not old_titles:
            store.configure(data["preferences"])
    return {"added": len(new_titles), "preserved": len(old_titles)}
