"""Local desktop cards, using the assistant's store, native reader and guarded conversation."""

from __future__ import annotations

import asyncio
import hmac
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from statistics import mean
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from backend.automation.locations import CITY_PLACES, native_observation
from backend.automation.models import normalize_phrase
from backend.brain.privacy import contains_secret
from backend.memory.store import _now
from backend.personal import PersonalContext
from backend.tools.geocode import GeocodeError, geocode
from backend.tools.registry import ToolError

DEFAULT_SETTINGS = {
    "ambient_enabled": True,
    "ambient_minutes": 5,
    "ambient_display": "primary",
    "ambient_fullscreen": False,
    "reduce_on_battery": True,
}


def city_zone(city: str) -> str:
    normalized = city.casefold().strip()
    if normalized in {"tallinn", "таллин", "таллинн"}:
        return "В общаге"
    if normalized in {"sillamäe", "sillamae", "силламяэ", "силламяе"}:
        return "Дома"
    return city


def dashboard_settings(value: dict) -> dict:
    if set(value) - DEFAULT_SETTINGS.keys():
        raise ValueError("Неизвестная настройка.")
    merged = {**DEFAULT_SETTINGS, **value}
    for key in ("ambient_enabled", "ambient_fullscreen", "reduce_on_battery"):
        if type(merged[key]) is not bool:
            raise ValueError("Неверное значение переключателя.")
    if type(merged["ambient_minutes"]) is not int or merged["ambient_minutes"] not in {
        1,
        3,
        5,
        10,
        15,
    }:
        raise ValueError("Выбери задержку 1, 3, 5, 10 или 15 минут.")
    if merged["ambient_display"] not in {"primary", "external"}:
        raise ValueError("Выбери основной или внешний дисплей.")
    return merged


class Dashboard:
    def __init__(self, bus, settings, store, conv, weather, *, personal=None):
        self.bus, self.s, self.store, self.conv, self.weather = bus, settings, store, conv, weather
        self.personal = personal or PersonalContext(settings)
        self.loop = None
        self.jobs: dict[str, asyncio.Task] = {}
        self.timezone = settings.location.tz_name
        self.calendar: dict[str, dict] = {}
        self.weather_state = self._saved("weather", {"status": "unknown"})
        if self.weather_state.get("data"):
            self.weather_state = {
                **self.weather_state,
                "status": "stale",
                "message": "Последняя сохранённая погода. Проверяю обновление.",
            }
        self.location = self._saved("location", {"status": "unknown", "city": None})
        automations = getattr(self.conv.tools, "automations", None)
        if automations:
            automations.locations.provider = lambda: self.location
            automations.locations.refresher = lambda access: self.read_location(
                access, refresh_weather=False
            )
        if self.location.get("city"):
            self.location = {**self.location, "status": "stale"}
        try:
            self.prefs = dashboard_settings(self._saved("settings", {}))
        except ValueError:
            self.prefs = dict(DEFAULT_SETTINGS)
        self.bus.handle("dashboard", self.request)
        self.bus.handle("chat_message", self.chat)
        self.publish()

    def _saved(self, key, default):
        try:
            value = self.store.preference("dashboard", key)
            return value if isinstance(value, dict) else default
        except (sqlite3.Error, ValueError):
            return default

    def today(self):
        return datetime.now(ZoneInfo(self.timezone)).date().isoformat()

    def native_timezone(self, result):
        name = result.get("timezone")
        if isinstance(name, str):
            try:
                ZoneInfo(name)
                self.timezone = name
            except ZoneInfoNotFoundError:
                pass

    def publish(self):
        if self.location.get("source") == "macos" and self.location.get("status") == "ready":
            if native_observation(self.location)["status"] != "ready":
                self.location = {
                    **self.location,
                    "status": "stale",
                    "message": "Последний город устарел. Для сценариев используется Общий.",
                }
        pending = None
        confirmations = getattr(self.conv.tools, "confirmations", None)
        if confirmations:
            item = confirmations.pending.get(
                (self.conv.device.device_id, self.conv.device.session_id)
            )
            if item and item.expires > confirmations.clock():
                pending = {
                    "summary": item.summary,
                    "fingerprint": item.fingerprint,
                    "code": item.code,
                }
        self.bus.publish(
            {
                "type": "dashboard",
                "ready": self.loop is not None,
                "timezone": self.timezone,
                "date": self.today(),
                "settings": self.prefs,
                "location": {**self.location, "zone": city_zone(self.location.get("city") or "")},
                "weather": self.weather_state,
                "calendar": self.calendar.get(
                    self.today(), {"status": "unknown", "date": self.today(), "events": []}
                ),
                "items": [
                    dict(r)
                    for r in self.store.db.execute(
                        "SELECT * FROM dashboard_items WHERE deleted_at IS NULL "
                        "ORDER BY completed,id DESC"
                    )
                ],
                "memories": self.store.memories(),
                "confirmation": pending,
                "integrations": {
                    "led": "led_control" in self.conv.tools,
                    "night": "night_mode" in self.conv.tools,
                    "calendar_enabled": self.store.preference("dashboard", "calendar_enabled")
                    is True,
                    "location_enabled": self.store.preference("dashboard", "location_enabled")
                    is True,
                },
                "conversation_id": self.conv.id,
            }
        )

    def notice(self, message, error=True):
        self.bus.publish({"type": "notice", "error": error, "message": message})

    def spawn(self, key, work):
        if key in self.jobs and not self.jobs[key].done():
            work.close()
            return

        async def run():
            try:
                await work
            except (ValueError, ToolError, OSError, sqlite3.Error, TimeoutError):
                self.notice("Не удалось обновить данные. Проверь разрешения и подключение.")
            except Exception:
                self.notice("Обновление не завершено. Сохранённые данные не изменены.")

        self.jobs[key] = asyncio.create_task(run())

    def cancel_job(self, key):
        if task := self.jobs.get(key):
            task.cancel()

    def chat(self, value):
        if self.loop is None:
            self.notice("Чушпан ещё загружается.")
        else:
            self.loop.submit_text(value)

    def request(self, value):
        try:
            self._request(value)
        except (ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            self.notice(
                str(exc) if isinstance(exc, ValueError) else "Не удалось применить изменение."
            )

    @staticmethod
    def text(value, limit=1000):
        if (
            not isinstance(value, str)
            or not 0 < len(value.strip()) <= limit
            or contains_secret(value)
        ):
            raise ValueError("Проверь текст: он пуст, слишком длинный или содержит секрет.")
        return value.strip()

    @staticmethod
    def identifier(value):
        if type(value) is not int or value <= 0:
            raise ValueError("Неверный номер записи.")
        return value

    def _request(self, value):
        op = value.get("op")
        if self.loop is None and op not in {"refresh", "settings", "stats"}:
            raise ValueError("Чушпан ещё загружается.")
        if op == "refresh":
            self.publish()
            self.statistics(value.get("days", 1))
            self.bus.request("history", 0)
        elif op == "settings":
            self.prefs = dashboard_settings(value.get("value", {}))
            self.store.set_preference("dashboard", "settings", self.prefs)
            self.publish()
            self.notice("Настройки комнаты сохранены.", False)
        elif op == "stats":
            self.statistics(value.get("days", 1))
        elif op == "location_connect":
            self.spawn("location", self.read_location(True))
        elif op == "manual_city":
            city = self.text(value.get("city"), 80)
            if any(not (c.isalpha() or c in " ,-'ёЁäÄöÖüÜ") for c in city):
                raise ValueError("Укажи название города, без адреса и координат.")
            self.spawn("location", self.manual_city(city))
        elif op == "location_disable":
            self.cancel_job("location")
            self.store.set_preference("dashboard", "location_enabled", False)
            self.location = {
                **self.location,
                "status": "stale" if self.location.get("city") else "unknown",
                "message": "Автоопределение остановлено. Последний город сохранён.",
            }
            self.publish()
        elif op == "weather_refresh":
            self.spawn("weather", self.read_weather())
        elif op in {"calendar_connect", "calendar_day", "calendar_refresh"}:
            day = value.get("date", self.today())
            datetime.strptime(day, "%Y-%m-%d")
            self.spawn("calendar", self.read_calendar(day, op == "calendar_connect"))
        elif op == "calendar_disable":
            self.cancel_job("calendar")
            self.store.set_preference("dashboard", "calendar_enabled", False)
            self.calendar.clear()
            self.publish()
        elif op == "calendar_open":
            self.spawn("calendar_open", self.open_calendar(value.get("id")))
        elif op == "new_conversation":
            self.require_idle()
            self.conv.reset()
            self.bus.clear_transcript()
            self.bus.request("history", 0)
            self.publish()
        elif op in {
            "conversation_rename",
            "conversation_pin",
            "conversation_delete",
            "conversation_restore",
        }:
            self.conversation_change(value)
        elif op in {"item_add", "item_update", "item_delete"}:
            self.item_change(value)
        elif op in {"memory_update", "memory_delete"}:
            self.require_idle()
            identifier = self.identifier(value.get("id"))
            row = next((r for r in self.store.memories() if r["id"] == identifier), None)
            if row is None or value.get("revision") != row["revision"]:
                raise ValueError("Воспоминание изменилось. Обнови список и повтори просьбу.")
            command = (
                f"забудь это факт {identifier}"
                if op == "memory_delete"
                else f"исправь воспоминание {identifier}: {self.text(value.get('content'), 600)}"
            )
            self.chat({"text": command})
        elif op == "confirm":
            self.require_idle()
            pending = self.conv.tools.confirmations.pending.get(
                (self.conv.device.device_id, self.conv.device.session_id)
            )
            if (
                not pending
                or not isinstance(value.get("fingerprint"), str)
                or not isinstance(value.get("code"), str)
                or not hmac.compare_digest(pending.fingerprint, value["fingerprint"])
                or not hmac.compare_digest(pending.code, value["code"])
            ):
                raise ValueError("Подтверждение устарело. Проверь текущее действие.")
            self.chat({"text": "Подтверждаю " + value["code"]})
        else:
            raise ValueError("Неизвестная команда дашборда.")

    def require_idle(self):
        if self.loop is None or self.loop.busy:
            raise ValueError("Дождись текущего ответа перед изменением данных.")

    def conversation_change(self, value):
        self.store._require_memory()
        identifier = self.identifier(value.get("id"))
        op = value["op"]
        if not self.store.db.execute(
            "SELECT 1 FROM conversations WHERE id=?", (identifier,)
        ).fetchone():
            raise ValueError("Разговор не найден.")
        if op == "conversation_delete" and self.conv.id == identifier:
            self.require_idle()
            self.conv.reset()
            self.bus.clear_transcript()
        with self.store.memory_transaction():
            if op == "conversation_rename":
                self.store.db.execute(
                    "UPDATE conversations SET title=? WHERE id=?",
                    (self.text(value.get("title"), 80), identifier),
                )
            elif op == "conversation_pin":
                self.store.db.execute(
                    "UPDATE conversations SET pinned=1-pinned WHERE id=?", (identifier,)
                )
            else:
                self.store.db.execute(
                    "UPDATE conversations SET deleted_at=? WHERE id=?",
                    (_now() if op == "conversation_delete" else None, identifier),
                )
        self.bus.request("history", 0)
        self.publish()

    def item_change(self, value):
        self.store._require_memory()
        with self.store.memory_transaction():
            if value["op"] == "item_add":
                kind = value.get("kind")
                if kind not in {"note", "goal", "task"}:
                    raise ValueError("Неизвестный вид записи.")
                self.store.db.execute(
                    "INSERT INTO dashboard_items(kind,content,created_at,updated_at) "
                    "VALUES (?,?,?,?)",
                    (kind, self.text(value.get("content"), 3000), _now(), _now()),
                )
            else:
                identifier = self.identifier(value.get("id"))
                row = self.store.db.execute(
                    "SELECT * FROM dashboard_items WHERE id=? AND deleted_at IS NULL", (identifier,)
                ).fetchone()
                if not row:
                    raise ValueError("Запись не найдена.")
                if value["op"] == "item_delete":
                    self.store.db.execute(
                        "UPDATE dashboard_items SET deleted_at=?,updated_at=? WHERE id=?",
                        (_now(), _now(), identifier),
                    )
                else:
                    completed = value.get("completed", bool(row["completed"]))
                    if type(completed) is not bool:
                        raise ValueError("Неверное состояние задачи.")
                    content = self.text(value.get("content", row["content"]), 3000)
                    self.store.db.execute(
                        "UPDATE dashboard_items SET content=?,completed=?,updated_at=? WHERE id=?",
                        (content, int(completed), _now(), identifier),
                    )
        self.publish()

    async def manual_city(self, city):
        self.location = {**self.location, "status": "loading", "message": "Ищу город…"}
        self.publish()
        try:
            result = await asyncio.wait_for(geocode(city, language="ru"), 15)
            self.location = {
                "city": result["name"].split(",")[0],
                "latitude": round(result["latitude"], 1),
                "longitude": round(result["longitude"], 1),
                "status": "ready",
                "source": "manual",
                "updated_at": _now(),
            }
            self.store.set_preference("dashboard", "location_enabled", False)
            automations = getattr(self.conv.tools, "automations", None)
            if automations:
                automations.locations.select(
                    "manual", CITY_PLACES.get(normalize_phrase(self.location["city"]), "general")
                )
            self.store.set_preference("dashboard", "location", self.location)
            self.publish()
            await self.read_weather()
        except (OSError, ToolError, ValueError, httpx.HTTPError, GeocodeError, TimeoutError):
            self.location = {
                **self.location,
                "status": "error",
                "message": "Город не найден или сеть недоступна. Последний город сохранён.",
            }
            self.publish()

    async def read_location(self, request_access=False, *, refresh_weather=True):
        self.location = {**self.location, "status": "loading", "message": "Определяю город на Mac…"}
        self.publish()
        try:
            result = (
                await self.personal.read(
                    request_access=request_access, parts=("location",), city_only=True
                )
            ).get("location", {})
        except (ToolError, OSError, TimeoutError):
            result = {}
        self.native_timezone(result)
        observed = native_observation(result)
        if observed["status"] != "ready":
            self.location = {
                **self.location,
                "status": "error",
                "error_code": observed["error_code"],
                "message": "Город не определён. Разреши геопозицию для «Чушпан — утро» "
                "в настройках macOS или выбери город вручную.",
            }
        else:
            self.location = observed
            self.store.set_preference("dashboard", "location_enabled", True)
            self.store.set_preference("dashboard", "location", self.location)
        self.publish()
        if refresh_weather and self.location.get("status") == "ready":
            await self.read_weather()

    async def read_weather(self):
        if self.weather is None or not self.location.get("city"):
            self.weather_state = {
                "status": "unknown",
                "message": "Выбери город, чтобы узнать погоду.",
            }
            self.publish()
            return
        self.weather_state = {
            **self.weather_state,
            "status": "loading",
            "message": "Обновляю погоду…",
        }
        self.publish()
        try:
            coordinates = {k: round(float(self.location[k]), 1) for k in ("latitude", "longitude")}
            result = await asyncio.wait_for(
                self.weather.forecast(days=3, coordinates=coordinates), 15
            )
            result["place"] = self.location["city"]
            self.weather_state = {"status": "ready", "data": result, "updated_at": _now()}
            self.store.set_preference("dashboard", "weather", self.weather_state)
        except Exception:
            self.weather_state = {
                **self.weather_state,
                "status": "error",
                "message": "Погода не обновилась. Проверь интернет; "
                "последнее измерение может быть устаревшим.",
            }
        self.publish()

    async def read_calendar(self, day, request_access=False):
        if (
            not request_access
            and self.store.preference("dashboard", "calendar_enabled") is not True
        ):
            self.calendar[day] = {
                "status": "unknown",
                "date": day,
                "events": [],
                "message": "Подключи Apple Calendar, чтобы увидеть события.",
            }
        else:
            self.calendar[day] = {
                "status": "loading",
                "date": day,
                "events": [],
                "message": "Читаю календарь Mac…",
            }
            self.bus.publish({"type": "dashboard_calendar", **self.calendar[day]})
            self.publish()
            try:
                result = (
                    await self.personal.read(
                        request_access=request_access, parts=("calendar",), day=day
                    )
                ).get("calendar", {})
            except (ToolError, OSError, TimeoutError):
                result = {}
            self.native_timezone(result)
            if result.get("error") or not isinstance(result.get("events"), list):
                self.calendar[day] = {
                    "status": "error",
                    "date": day,
                    "events": [],
                    "message": "Нет доступа к календарю. Разреши полный доступ для "
                    "«Чушпан — утро» в настройках macOS → Конфиденциальность → Календари.",
                }
            else:
                self.store.set_preference("dashboard", "calendar_enabled", True)
                self.calendar[day] = {
                    "status": "ready",
                    "date": day,
                    "events": result.get("events", []),
                    "timezone": result.get("timezone", self.s.location.tz_name),
                    "updated_at": _now(),
                }
        while len(self.calendar) > 32:
            del self.calendar[next(iter(self.calendar))]
        self.bus.publish({"type": "dashboard_calendar", **self.calendar[day]})
        self.publish()

    async def open_calendar(self, identifier=None):
        from backend.media.safari import process

        if identifier is None:
            await process("open", "-a", "Calendar", timeout=5)
            return
        event = next(
            (
                e
                for d in self.calendar.values()
                for e in d.get("events", [])
                if e.get("id") == identifier
            ),
            None,
        )
        if not event or not event.get("external_id"):
            raise ValueError("Событие устарело. Обнови календарь.")
        script = """on run argv
try
set targetDate to current date
set day of targetDate to 1
set year of targetDate to (item 4 of argv as integer)
set month of targetDate to (item 5 of argv as integer)
set day of targetDate to (item 6 of argv as integer)
set time of targetDate to (item 7 of argv as integer)
tell application "Calendar"
activate
repeat with c in calendars
if name of c is (item 2 of argv) then
set foundEvents to (events of c whose uid is (item 1 of argv))
if (count of foundEvents) is 0 then
set foundEvents to (events of c whose summary is (item 3 of argv) and start date is targetDate)
end if
if (count of foundEvents) is 1 then
set selectedEvent to item 1 of foundEvents
view calendar at targetDate
show selectedEvent
return "opened"
else if (count of foundEvents) > 1 then
return "ambiguous"
end if
end if
end repeat
end tell
return "not_found"
on error errorMessage number errorNumber
return "error:" & errorNumber
end try
end run"""
        start = datetime.fromisoformat(event["start"]).astimezone(ZoneInfo(self.timezone))
        try:
            result = await process(
                "osascript",
                "-e",
                script,
                "--",
                event["external_id"],
                event["calendar"],
                event["title"],
                str(start.year),
                str(start.month),
                str(start.day),
                str(start.hour * 3600 + start.minute * 60 + start.second),
                timeout=15,
            )
        except (ToolError, TimeoutError):
            result = "unavailable"
        result = result.strip()
        if result == "error:-1743":
            self.notice(
                "Не удалось открыть событие. Проверь доступ Чушпана к Calendar "
                "в macOS → Автоматизация."
            )
        elif result in {"not_found", "ambiguous"}:
            self.notice("Событие не найдено однозначно в Calendar. Обнови выбранный день.")
        elif result != "opened":
            self.notice(
                "Calendar не подтвердил открытие события. Можно открыть приложение кнопкой выше."
            )

    def statistics(self, days=1):
        if type(days) is not int or days not in {1, 7, 30}:
            raise ValueError("Выбери период: день, неделя или месяц.")
        now = datetime.now(ZoneInfo(self.timezone))
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
        cutoff = start.astimezone(UTC).isoformat()
        rows = self.store.db.execute(
            "SELECT data FROM request_traces WHERE created_at>=? AND kind!='activation' "
            "ORDER BY created_at",
            (cutoff,),
        ).fetchall()
        samples, stages, commands, failures, buckets = (
            [],
            {k: [] for k in ("stt", "model_total", "tts_generation")},
            0,
            0,
            {},
        )
        for row in rows:
            trace = json.loads(row["data"])
            date = datetime.fromisoformat(trace["created_at"]).astimezone(ZoneInfo(self.timezone))
            key = date.strftime("%H:00") if days == 1 else date.strftime("%d.%m")
            bucket = buckets.setdefault(key, {"label": key, "requests": 0, "samples": []})
            bucket["requests"] += 1
            if trace["status"] == "failed":
                failures += 1
            if trace["status"] == "completed":
                if (first := trace.get("first_content_s")) is not None:
                    samples.append(first)
                    bucket["samples"].append(first)
                for name in stages:
                    values = [
                        s["seconds"]
                        for s in trace["stages"]
                        if s["stage"] == name and s.get("seconds") is not None
                    ]
                    if values:
                        stages[name].append(sum(values))
            commands += sum(
                s["stage"] == "tool_execution" and s["outcome"] == "ok" for s in trace["stages"]
            )
        counts = self.store.db.execute(
            "SELECT COUNT(DISTINCT conversation_id),COUNT(*) FROM messages WHERE created_at>=? "
            "AND role IN ('user','assistant') AND tool_calls IS NULL AND content!=''",
            (cutoff,),
        ).fetchone()
        model = getattr(self.conv.llm, "last_model", None)
        if not model:
            model = next(
                (
                    json.loads(row["data"]).get("metadata", {}).get("model")
                    for row in reversed(rows)
                    if json.loads(row["data"]).get("metadata", {}).get("model")
                ),
                None,
            )
        self.bus.publish(
            {
                "type": "dashboard_stats",
                "days": days,
                "conversations": counts[0],
                "messages": counts[1],
                "response_s": mean(samples) if samples else None,
                "stages": {k: mean(v) if v else None for k, v in stages.items()},
                "commands": commands,
                "errors": failures,
                "model": model,
                "buckets": [
                    {
                        "label": b["label"],
                        "requests": b["requests"],
                        "response_s": mean(b["samples"]) if b["samples"] else None,
                    }
                    for b in buckets.values()
                ],
            }
        )

    def start(self, loop):
        self.loop = loop
        self.publish()

        async def refresh():
            previous = time.time()
            next_location = next_weather = next_calendar = 0.0
            while True:
                now = time.time()
                woke = now - previous > 90
                previous = now
                self.publish()
                self.statistics()
                if (woke or now >= next_location) and self.store.preference(
                    "dashboard", "location_enabled"
                ) is True:
                    next_location = now + 120
                    self.spawn("location", self.read_location())
                if (woke or now >= next_weather) and self.location.get("city"):
                    next_weather = now + 1200
                    self.spawn("weather", self.read_weather())
                if (woke or now >= next_calendar) and self.store.preference(
                    "dashboard", "calendar_enabled"
                ) is True:
                    next_calendar = now + 300
                    self.spawn("calendar", self.read_calendar(self.today()))
                await asyncio.sleep(30)

        return [asyncio.create_task(refresh())]

    async def close(self):
        for task in self.jobs.values():
            task.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)
