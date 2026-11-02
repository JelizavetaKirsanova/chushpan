"""User-managed places, using existing city observations. No new location/network access."""

from datetime import UTC, datetime

from backend.automation.models import Place, normalize_phrase


class Locations:
    def __init__(self, memory):
        self.memory, self.db = memory, memory.db
        self.provider = None
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS scenario_places (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        with memory.memory_transaction():
            marker = self.db.execute(
                "SELECT 1 FROM scenario_meta WHERE key='places_seeded'"
            ).fetchone()
            if not marker and memory.memory_available:
                for identifier, name in [("dorm", "Общага"), ("home", "Дом")]:
                    place = Place(id=identifier, revision=1, name=name)
                    self.db.execute(
                        "INSERT OR IGNORE INTO scenario_places VALUES (?,?)",
                        (identifier, place.model_dump_json()),
                    )
                self.db.execute("INSERT INTO scenario_meta VALUES ('places_seeded','1')")

    def list(self):
        return [
            Place.model_validate_json(row[0])
            for row in self.db.execute("SELECT data FROM scenario_places ORDER BY rowid")
        ]

    def get(self, identifier):
        row = self.db.execute(
            "SELECT data FROM scenario_places WHERE id=?", (identifier,)
        ).fetchone()
        if not row:
            raise ValueError("Место удалено или не найдено.")
        return Place.model_validate_json(row[0])

    def save(self, place):
        self.memory._require_memory()
        with self.memory.memory_transaction():
            existing = self.db.execute(
                "SELECT data FROM scenario_places WHERE id=?", (place.id,)
            ).fetchone()
            revision = Place.model_validate_json(existing[0]).revision if existing else 0
            if place.revision != revision:
                raise ValueError("Настройки места изменились в другом окне.")
            names = {normalize_phrase(place.name)}
            aliases = {normalize_phrase(city) for city in place.cities}
            for other in self.list():
                if other.id == place.id:
                    continue
                if normalize_phrase(other.name) in names or aliases & {
                    normalize_phrase(c) for c in other.cities
                }:
                    raise ValueError("Название места или город уже привязаны к другому месту.")
            saved = place.model_copy(update={"revision": revision + 1})
            self.db.execute(
                "INSERT INTO scenario_places VALUES (?,?) ON CONFLICT(id) "
                "DO UPDATE SET data=excluded.data",
                (place.id, saved.model_dump_json()),
            )
        return saved

    def delete(self, identifier, revision, scenarios):
        self.memory._require_memory()
        with self.memory.memory_transaction():
            if self.get(identifier).revision != revision:
                raise ValueError("Место изменилось. Обнови настройки.")
            if any(v.place_id == identifier for s in scenarios for v in s.variants):
                raise ValueError(
                    "Сначала удали или перенеси версии команд, использующие это место."
                )
            self.db.execute("DELETE FROM scenario_places WHERE id=?", (identifier,))
            selection = self.memory.preference("automation", "location") or {}
            if selection.get("place_id") == identifier:
                self.memory.set_preference("automation", "location", {"mode": "auto"})

    def select(self, mode, identifier=""):
        if mode not in {"auto", "manual"}:
            raise ValueError("Выбери автоматическое или ручное определение места.")
        if mode == "manual":
            self.get(identifier)
        self.memory.set_preference("automation", "location", {"mode": mode, "place_id": identifier})

    def current(self):
        selection = self.memory.preference("automation", "location") or {"mode": "auto"}
        places = self.list()
        if selection["mode"] == "manual":
            found = next((p for p in places if p.id == selection.get("place_id")), None)
            if found:
                return {
                    "place_id": found.id,
                    "name": found.name,
                    "mode": "manual",
                    "status": "ready",
                }
        observed = (
            self.provider()
            if self.provider
            else self.memory.preference("dashboard", "location") or {}
        )
        city = observed.get("city") or ""
        fresh = False
        try:
            age = (
                datetime.now(UTC) - datetime.fromisoformat(observed["updated_at"])
            ).total_seconds()
            fresh = 0 <= age < 1800
        except (ValueError, KeyError, TypeError):
            pass
        if observed.get("status") == "ready" and fresh and city:
            from backend.hud.dashboard import city_zone

            aliases = [
                p
                for p in places
                if normalize_phrase(city) in {normalize_phrase(c) for c in p.cities}
            ]
            if not aliases:
                zone = city_zone(city)
                zone = {"В общаге": "Общага", "Дома": "Дом"}.get(zone, zone)
                aliases = [p for p in places if normalize_phrase(p.name) == normalize_phrase(zone)]
            if len(aliases) == 1:
                place = aliases[0]
                return {
                    "place_id": place.id,
                    "name": place.name,
                    "mode": "auto",
                    "status": "ready",
                    "city": city,
                }
        return {
            "place_id": None,
            "name": None,
            "mode": "auto",
            "status": "unknown",
            "message": "Место не определено однозначно. Выбери его в разделе «Мои команды».",
        }
