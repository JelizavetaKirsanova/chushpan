"""Fresh macOS city observations and a persistent manual override."""

import asyncio
import math
import time
from datetime import UTC, datetime

from backend.automation.models import Place, normalize_phrase
from backend.tools.registry import ToolError

LOCATION_TTL_S = 300
MAX_CITY_ACCURACY_M = 3000
CITY_PLACES = {
    "tallinn": "dorm",
    "таллин": "dorm",
    "таллинн": "dorm",
    "sillamäe": "home",
    "sillamae": "home",
    "силламяэ": "home",
    "силламяе": "home",
}


def observation_age(observed):
    try:
        stamp = observed.get("measured_at") or observed["updated_at"]
        return (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()
    except (ValueError, KeyError, TypeError):
        return float("inf")


def native_observation(result):
    """Keep coarse coordinates and the original measurement time from the native helper."""
    value = {
        "status": "error",
        "source": "macos",
        "error_code": result.get("error", "location_unavailable"),
    }
    try:
        if type(result.get("accuracy_m")) not in (int, float):
            return value
        accuracy = float(result["accuracy_m"])
        latitude, longitude = float(result["latitude"]), float(result["longitude"])
        if not (
            math.isfinite(accuracy)
            and 0 <= accuracy <= MAX_CITY_ACCURACY_M
            and math.isfinite(latitude)
            and -90 <= latitude <= 90
            and math.isfinite(longitude)
            and -180 <= longitude <= 180
            and isinstance(result.get("city"), str)
            and 0 < len(result["city"].strip()) <= 120
            and result.get("measured_at")
            and 0 <= observation_age(result) <= LOCATION_TTL_S
        ):
            return value
        return {
            "city": result["city"].strip(),
            "country_code": result.get("country_code", ""),
            "latitude": round(latitude, 1),
            "longitude": round(longitude, 1),
            "accuracy_m": accuracy,
            "measured_at": result["measured_at"],
            "status": "ready",
            "source": "macos",
            "updated_at": datetime.now(UTC).isoformat(),
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        return value


class Locations:
    def __init__(self, memory, settings=None):
        self.memory, self.db = memory, memory.db
        self.provider = None
        self.refresher = self.read_native if settings is not None else None
        self.personal = None
        self.settings = settings
        self._refresh_lock = asyncio.Lock()
        self._last_attempt = None
        self._stable = None
        self._candidate = None
        self.signal_observation = {}
        self.signal_reader = self.read_signals if settings is not None else None
        self._signal_attempt = None
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS scenario_places (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        with memory.memory_transaction():
            if memory.memory_available:
                for identifier, name in [("dorm", "Общага"), ("home", "Дом")]:
                    place = Place(id=identifier, revision=1, name=name)
                    self.db.execute(
                        "INSERT OR IGNORE INTO scenario_places VALUES (?,?)",
                        (identifier, place.model_dump_json()),
                    )
                self.db.execute("INSERT OR IGNORE INTO scenario_meta VALUES ('places_seeded','1')")

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
            if identifier in {"home", "dorm"}:
                raise ValueError("Места Дом и Общага нужны для автоматического выбора версии.")
            if self.get(identifier).revision != revision:
                raise ValueError("Место изменилось. Обнови настройки.")
            if any(v.place_id == identifier for s in scenarios for v in s.variants):
                raise ValueError(
                    "Сначала удали или перенеси версии команд, использующие это место."
                )
            self.db.execute("DELETE FROM scenario_places WHERE id=?", (identifier,))
            selection = self.memory.preference("automation", "location") or {}
            if selection.get("place_id") == identifier:
                self.memory.set_preference(
                    "automation", "location", {"mode": "manual", "place_id": "general"}
                )

    def select(self, mode, identifier=""):
        if mode not in {"auto", "manual"}:
            raise ValueError("Выбери автоматическое или ручное определение места.")
        if mode == "manual" and identifier != "general":
            self.get(identifier)
        self.memory.set_preference("automation", "location", {"mode": mode, "place_id": identifier})

    def observation(self):
        return (
            self.provider() if self.provider else self.memory.preference("dashboard", "location")
        ) or {}

    def current(self):
        selection = self.memory.preference("automation", "location") or {"mode": "auto"}
        if selection.get("mode") == "manual":
            identifier = selection.get("place_id")
            found = next((p for p in self.list() if p.id == identifier), None)
            if found or identifier == "general":
                return {
                    "place_id": found.id if found else None,
                    "name": found.name if found else "Общий",
                    "mode": "manual",
                    "status": "ready",
                }
            return {
                "place_id": None,
                "name": None,
                "mode": "manual",
                "status": "unknown",
                "message": "Место недоступно — используется Общий. Выбери место вручную.",
            }
        observed = self.observation()
        unknown = {
            "place_id": None,
            "name": None,
            "city": None,
            "mode": "auto",
            "status": "unknown",
            "geolocation_enabled": self.memory.preference("dashboard", "location_enabled") is True,
            "message": "Место неизвестно — используется Общий.",
        }
        signals = self.signal_observation
        if signals.get("source") == "macos" and 0 <= observation_age(signals) <= 60:
            seen = {(v["kind"], v["key"]) for v in signals.get("signals", [])}
            matches = [
                p for p in self.list() if any((v["kind"], v["key"]) in seen for v in p.signals)
            ]
            if len(matches) == 1:
                return {
                    "place_id": matches[0].id,
                    "name": matches[0].name,
                    "city": None,
                    "mode": "auto",
                    "status": "ready",
                    "reason": "explicit_signal_binding",
                    "message": "Явная привязка к текущему Wi-Fi или подключённому устройству.",
                }
            if len(matches) > 1:
                return {
                    **unknown,
                    "reason": "signal_conflict",
                    "message": "Сигналы указывают на разные места — используется Общий.",
                }
        if (
            observed.get("status") != "ready"
            or not 0 <= observation_age(observed) <= LOCATION_TTL_S
        ):
            return {**unknown, "reason": observed.get("error_code", "location_stale")}
        if observed.get("source") != "macos":
            return {**unknown, "reason": "location_source"}
        if self.memory.preference("dashboard", "location_enabled") is not True:
            return unknown
        checked = native_observation(observed)
        if checked["status"] != "ready":
            return {**unknown, "reason": checked["error_code"]}
        city = observed.get("city")
        if not isinstance(city, str) or not city.strip():
            return unknown
        key = normalize_phrase(city)
        identifier = (
            CITY_PLACES.get(key) if observed.get("country_code", "EE") in ("", "EE") else None
        )
        # Two different fresh measurements confirm a changed nearby city. Re-publishing
        # the same cached fix never counts as a second measurement. During uncertainty
        # General is used; an old local version is never treated as current authority.
        identity = identifier or key
        stamp = observed.get("measured_at") or observed["updated_at"]
        if (
            self._stable
            and self._stable[0] != identity
            and {self._stable[0], identity} != {"dorm", "home"}
            and observation_age({"updated_at": self._stable[1]}) <= LOCATION_TTL_S
        ):
            candidate = self._candidate
            if not candidate or candidate[0] != identity:
                self._candidate = (identity, stamp)
                return {
                    **unknown,
                    "status": "confirming",
                    "message": "Уточняю смену города — используется Общий.",
                }
            try:
                gap = (
                    datetime.fromisoformat(stamp) - datetime.fromisoformat(candidate[1])
                ).total_seconds()
            except (ValueError, TypeError):
                gap = 0
            if gap < 2:
                return {
                    **unknown,
                    "status": "confirming",
                    "message": "Уточняю смену города — используется Общий.",
                }
        self._stable, self._candidate = (identity, stamp), None
        place = next((p for p in self.list() if p.id == identifier), None)
        return {
            "place_id": place.id if place else None,
            "name": place.name if place else "Общий",
            "city": city.strip(),
            "mode": "auto",
            "status": "ready",
            "geolocation_enabled": unknown["geolocation_enabled"],
        }

    async def read_native(self, request_access=False):
        from backend.personal import PersonalContext

        self.personal = self.personal or PersonalContext(self.settings)
        result = (
            await self.personal.read(
                request_access=request_access, parts=("location",), city_only=True
            )
        ).get("location", {})
        observed = native_observation(result)
        self.memory.set_preference("dashboard", "location", observed)
        if observed["status"] == "ready":
            self.memory.set_preference("dashboard", "location_enabled", True)

    async def refresh(self, *, request_access=False, force=False):
        if (self.memory.preference("automation", "location") or {}).get("mode") == "manual":
            return self.current()
        if any(p.signals for p in self.list()):
            await self.refresh_signals(force=force)
        if (
            not request_access
            and self.memory.preference("dashboard", "location_enabled") is not True
        ):
            return self.current()

        async with self._refresh_lock:
            current = self.current()
            if not force and current["status"] == "ready":
                return current
            now = time.monotonic()
            if not force and self._last_attempt is not None and now - self._last_attempt < 30:
                return current
            if self.refresher is None:
                return current
            self._last_attempt = now
            try:
                async with asyncio.timeout(30):
                    await self.refresher(request_access)
            except (OSError, ToolError, TimeoutError, ValueError):
                if self.provider is None:
                    self.memory.set_preference(
                        "dashboard",
                        "location",
                        {
                            "source": "macos",
                            "status": "error",
                            "error_code": "location_unavailable",
                        },
                    )
                # The dashboard refresher marks its own observation loading/error before awaiting.
            return self.current()

    async def refresh_signals(self, *, force=False):
        if (
            not force
            and self._signal_attempt is not None
            and time.monotonic() - self._signal_attempt < 30
        ):
            return self.signal_observation
        self._signal_attempt = time.monotonic()
        self.signal_observation = {
            "source": "macos",
            "updated_at": datetime.now(UTC).isoformat(),
            "signals": [],
        }
        if self.signal_reader:
            try:
                async with asyncio.timeout(8):
                    observed = await self.signal_reader()
                    self.signal_observation = {
                        **observed,
                        "source": "macos",
                        "updated_at": datetime.now(UTC).isoformat(),
                    }
            except (OSError, ToolError, TimeoutError, ValueError):
                self.signal_observation["error"] = (
                    "Текущие сигналы недоступны. Старые привязки не считаются наблюдением."
                )
        return self.signal_observation

    async def read_signals(self):
        from backend.context_signals import read_signals

        return await read_signals(self.settings)
