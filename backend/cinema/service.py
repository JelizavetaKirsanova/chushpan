"""One bounded player session; async HUD endpoint and guarded voice tools."""

import asyncio
import contextlib
import functools
import json
import math
import secrets
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from backend.cinema.adapters import (
    HTML5Adapter,
    LocalFileAdapter,
    ManualAdapter,
    SafariBridge,
    open_page,
)
from backend.cinema.catalogue import Catalogue
from backend.cinema.commands import command
from backend.cinema.filters import with_preferences
from backend.cinema.models import CatalogueQuery, origin
from backend.cinema.personal import Recommender, score, signal, taste
from backend.cinema.sources import SourceManager, capabilities
from backend.cinema.store import CinemaStore
from backend.cinema.tmdb import TMDB, TMDBCredentials, TMDBRateLimited
from backend.context import RequestContext, request_scope, voice_addressed
from backend.mac_agent import LocalMacAgent
from backend.security import ActionDenied, Risk, command_text
from backend.tools.registry import Tool, ToolError


class Cinema:
    def __init__(
        self, tools, settings, memory, *, adapter_factory=None, opener=open_page, tmdb=None
    ):
        self.tools, self.s, self.memory = tools, settings, memory
        path = memory.db.execute("PRAGMA database_list").fetchone()[2] or ":memory:"
        self.available = memory.memory_available
        try:
            self.store = CinemaStore(path)
        except sqlite3.Error:
            # A failed cinema migration must not stop speech or the rest of the HUD.
            self.store = CinemaStore(":memory:")
            self.available = False
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cinema-sqlite")
        self.adapter_factory = adapter_factory or (
            lambda name: (
                HTML5Adapter(SafariBridge(settings))
                if name == "html5"
                else LocalFileAdapter()
                if name == "local"
                else ManualAdapter(opener)
            )
        )
        self.opener = opener
        self.bus = self.conv = None
        self.session = None
        self.poller = None
        self.jobs = set()
        self.lock = asyncio.Lock()
        self.closed = False
        self.message = ""
        self.tmdb = tmdb or TMDB(TMDBCredentials(settings.data_path / "integrations"))
        self.catalogues = {"TMDB": self.tmdb}
        self.catalogue = Catalogue(self.tmdb)
        self.sources = SourceManager(self.db, opener=self.opener)
        self.personal = Recommender(self.tmdb, self.catalogue)
        self.catalogue_requests = set()
        self.matches = []
        self.focused = None
        self.focused_catalogue = None
        self.search_sequence = 0
        self.import_proposals = {}
        self.register()

    async def db(self, method, *args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(
            self.worker, functools.partial(getattr(self.store, method), *args, **kwargs)
        )

    def register(self):
        command_tool = Tool(
            "cinema_command",
            "Кино: продолжить последний сериал, пауза, следующая серия, "
            "перемотка, информация и библиотека. Только прямая команда пользовательницы.",
            self.execute,
            {"command": {"type": "string", "minLength": 1, "maxLength": 400}},
            ["command"],
            risk=Risk.CHANGE,
            intent=lambda args, text: (
                command_text(args["command"]) == command_text(text)
                and command(text, bool(self.session)) is not None
            ),
            reply=lambda result: result.get("message", "Действие плеера не подтверждено."),
            capability={
                "ru": "Кинотека, отдельный прогресс серий и управление доступным HTML5-плеером."
            },
        )
        open_tool = Tool(
            "cinema_open",
            "Открыть выбранный фильм или серию из персональной кинотеки.",
            self.open,
            {
                "title_id": {"type": "string", "maxLength": 64},
                "episode_id": {"type": "string", "maxLength": 64},
                "link_id": {"type": "string", "maxLength": 64},
                "device": {"type": "string", "enum": ["mac", "airplay"]},
            },
            ["title_id"],
            risk=Risk.CHANGE,
            intent=self.open_intent,
            reply=lambda result: result.get("message", "Воспроизведение не подтверждено."),
        )
        for tool in (command_tool, open_tool):
            tool.required_capability = "mac.media"
            tool.agent = self.tools.mac_agent
            tool.settings_store = self.memory
            if isinstance(tool.agent, LocalMacAgent):
                tool.agent.register(tool.name, tool._handler)
            self.tools.add(tool)

    def open_intent(self, args, text):
        # Synchronous lookup for the registry guard only; never read player state here.
        with contextlib.suppress(ValueError, sqlite3.Error):
            # The owning memory connection is only used on the event loop.
            row = self.memory.db.execute(
                "SELECT title FROM cinema_titles WHERE id=?", (args["title_id"],)
            ).fetchone()
            if row:
                value = command_text(text).rstrip(".!? ")
                return value in {
                    f"открой сериал {row[0].casefold()}",
                    f"открой фильм {row[0].casefold()}",
                }
        return False

    def route(self, text):
        return ("cinema_command", {"command": text}) if command(text, bool(self.session)) else None

    def bind(self, bus, conv):
        self.bus, self.conv = bus, conv
        bus.cinema = self
        bus.handle("cinema", self.request)
        self.schedule(self.publish())

    def schedule(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.jobs.add(task)
        task.add_done_callback(self.jobs.discard)
        return task

    def request(self, value):
        if not self.closed:
            self.schedule(self.handle(value))

    def location(self):
        devices = getattr(self.tools, "devices", None)
        return devices.locations.current().get("place_id") or "general" if devices else "general"

    def device_options(self):
        devices = getattr(self.tools, "devices", None)
        records = devices.store.list() if devices else []
        return [
            {"id": "mac", "name": "Этот MacBook", "available": True, "reason": ""},
            {
                "id": "airplay",
                "name": "Телевизор · AirPlay",
                "available": False,
                "reason": "Менеджер устройств пока не имеет адаптера передачи видео AirPlay. "
                "Можно выбрать телевизор вручную в плеере Safari, если он это поддерживает.",
            },
            *[
                {
                    "id": d.id,
                    "name": d.name,
                    "available": False,
                    "reason": "BetterDisplay управляет яркостью дисплея и не передаёт видео.",
                }
                for d in records
                if d.kind == "external_display"
            ],
        ]

    async def publish(self, **extra):
        if not self.bus or self.closed:
            return
        data = await self.db("snapshot")
        for source in data["sources"]:
            source["capabilities"] = capabilities(source)
        self.bus.publish(
            {
                "type": "cinema",
                **data,
                "location": self.location(),
                "devices": self.device_options(),
                "durable": self.available,
                "session": {
                    "title_id": self.session["target"]["item"]["id"],
                    "episode_id": self.session["target"]["episode"]["id"],
                    "mode": self.session["mode"],
                }
                if self.session
                else None,
                "message": self.message,
                "matches": self.matches,
                "tmdb": {"configured": self.tmdb.credentials.configured},
                **extra,
            }
        )

    def require_local(self):
        if (
            not self.conv
            or not self.conv.device.authenticated
            or self.conv.device.target_agent_id != "local-mac"
        ):
            raise ActionDenied("Кинотека доступна только авторизованному локальному приложению.")
        if not self.available:
            raise ToolError("SQLite недоступна. Сохранение кинотеки не подтверждено.")

    def require_playback(self):
        self.require_local()
        if "mac.media" not in self.conv.device.capabilities:
            raise ActionDenied("У этой сессии нет разрешения на управление медиаплеером.")
        if not self.tools.enabled("cinema_open"):
            raise ToolError("Открытие кино отключено в настройках доступа.")

    async def handle(self, value):
        try:
            self.require_local()
            if not isinstance(value, dict):
                raise ValueError("Неверный запрос кинотеки.")
            op = value.get("op")
            result = None
            source_action = None
            if op == "refresh":
                pass
            elif op == "library_export":
                document = await self.db("export_library")
                await self.publish(library_export=document)
                return
            elif op == "library_import_preview":
                summary = await self.db("import_preview", value["document"])
                token = secrets.token_urlsafe(24)
                self.import_proposals = {token: (time.monotonic() + 600, value["document"])}
                await self.publish(library_import_preview={"token": token, **summary})
                return
            elif op == "library_import":
                proposal = self.import_proposals.pop(value["token"], None)
                if not proposal or proposal[0] < time.monotonic():
                    raise ToolError("Предпросмотр импорта устарел. Выбери файл заново.")
                async with self.lock:
                    result = await self.db("import_library", proposal[1])
                self.sources.invalidate()
                self.message = (
                    f"Импортировано названий: {result['added']}. Существующие данные сохранены."
                )
                await self.publish(library_imported=result)
                return
            elif op == "clear_cinema_cache":
                if value.get("scope") not in {"tmdb", "sources"}:
                    raise ValueError("Неизвестный кеш.")
                if value["scope"] == "tmdb":
                    self.tmdb.clear_cache()
                    self.catalogue.page_counts.clear()
                else:
                    self.sources.invalidate()
                self.message = "Кеш очищен. Личная медиатека сохранена."
            elif op == "source":
                async with self.lock:
                    result = await self.db("save_source", value["source"])
                    if self.session and self.session["target"]["source"]["id"] == result["id"]:
                        if (
                            not result["enabled"]
                            or result["adapter"] != self.session["target"]["source"]["adapter"]
                        ):
                            await self.stop_tracking()
                self.sources.invalidate()
            elif op in {"source_delete", "source_order", "source_check"}:
                if op == "source_delete":
                    async with self.lock:
                        if self.session and self.session["target"]["source"]["id"] == value["id"]:
                            await self.stop_tracking()
                        await self.db("delete_source", value["id"])
                elif op == "source_order":
                    await self.db("reorder_sources", value["ids"])
                else:
                    await self.sources.check(value["id"])
                self.sources.invalidate()
            elif op in {"profile", "recommendations", "personal_enrich"}:
                await self.personal_request(value)
                return
            elif op == "recommendation_dismiss":
                await self.db("feedback", value["key"])
                await self.publish(recommendation_dismissed=value["key"])
                return
            elif op == "recommendation_reset":
                await self.db("reset_recommendations")
                self.message = "Рекомендательный профиль сброшен. Оценки и история сохранены."
                await self.publish(recommendations_reset=True)
                return
            elif op == "find_sources":
                choices = await self.sources.search(
                    value["title_id"],
                    value.get("episode_id"),
                    refresh=value.get("refresh") is True,
                    alternative=value.get("alternative") is True,
                )
                await self.publish(
                    source_results={**choices, "request_id": str(value.get("request_id", ""))[:100]}
                )
                return
            elif op in {"source_result_open", "source_result_remember"}:
                result = await self.source_result(
                    value["result_id"],
                    open_result=op == "source_result_open",
                    pin=value.get("pin") is True,
                )
                source_action = {
                    "result_id": value["result_id"],
                    "message": result.get("message", ""),
                }
            elif op == "source_unpin":
                await self.db("unpin_source", value["title_id"])
                self.sources.invalidate()
            elif op == "embedded_event":
                await self.embedded_event(value)
                return
            elif op == "add":
                result = await self.db("add", value["entry"])
                self.sources.invalidate()
            elif op == "update":
                async with self.lock:
                    if (
                        self.session
                        and self.session["target"]["item"]["id"] == value["id"]
                        and "status" in value["values"]
                    ):
                        await self.stop_tracking()
                    result = await self.db("update_title", value["id"], value["values"])
            elif op in {"delete", "replay"}:
                async with self.lock:
                    if self.session and self.session["target"]["item"]["id"] == value["id"]:
                        await self.stop_tracking()
                    result = await self.db(op, value["id"])
                    if op == "delete" and self.focused == value["id"]:
                        self.focused = None
                if op == "delete":
                    self.message = "Удалено из медиатеки."
            elif op == "focus":
                await self.db("title", value["id"])
                self.focused = value["id"]
                self.focused_catalogue = None
                return
            elif op == "catalogue_focus":
                TMDB.identity(value["key"])
                self.focused_catalogue = value["key"]
                self.focused = None
                return
            elif op == "clear_focus":
                self.focused = self.focused_catalogue = None
                return
            elif op == "tmdb":
                await asyncio.to_thread(self.tmdb.credentials.save, value["credential"])
                self.tmdb.clear_cache()
                self.catalogue = Catalogue(self.tmdb)
                self.personal = Recommender(self.tmdb, self.catalogue)
                self.matches = []
                self.message = "Настройки TMDB сохранены."
                await self.publish(saved="tmdb")
                return
            elif op == "catalogue_detail":
                detail = await self.tmdb.detail(value["key"])
                await self.publish(catalogue_detail=detail)
                return
            elif op == "title_details":
                item = await self.db("title", value["id"])
                detail = await self.tmdb.detail(item["catalogue_key"])
                await self.db("add", {**detail, "title_id": item["id"]})
                await self.publish(detail_updated=item["id"])
                return
            elif op == "similar":
                prefs = await self.db("preferences")
                context = await self.db("recommendation_context")
                result = await self.catalogue.page(
                    with_preferences(CatalogueQuery(feed="recommended"), prefs),
                    seeds=[value["key"]],
                    excluded={value["key"], *context["excluded"]},
                    excluded_titles=context["excluded_titles"],
                )
                await self.publish(
                    similar={
                        "request_id": str(value.get("request_id", ""))[:100],
                        "items": result["items"],
                    }
                )
                return
            elif op == "catalogue_add":
                detail = await self.tmdb.detail(value["key"])
                result = await self.db("add", detail)
                values = value.get("values")
                if values:
                    result = await self.db("update_title", result["id"], values)
            elif op == "catalogue_config":
                try:
                    config = await self.catalogue.configuration()
                    await self.publish(catalogue_config=config)
                except (ToolError, ValueError, KeyError, TypeError) as exc:
                    await self.publish(
                        catalogue_config_error=str(exc)
                        if isinstance(exc, ToolError)
                        else "Не удалось загрузить справочники TMDB. Повтори позже."
                    )
                return
            elif op == "catalogue_page":
                await self.catalogue_page(value)
                return
            elif op == "season":
                item = await self.db("title", value["id"])
                episodes = await self.tmdb.episodes(item["catalogue_key"], value["season"])
                result = await self.db("season", value["id"], episodes)
                await self.publish(season_loaded=value["id"], loaded_season=value["season"])
                return
            elif op == "progress":
                async with self.lock:
                    # Do not let a poll overwrite an explicit manual correction.
                    if (
                        self.session
                        and self.session["target"]["episode"]["id"] == value["episode_id"]
                    ):
                        await self.stop_tracking(flush=False)
                    result = await self.db(
                        "progress",
                        value["episode_id"],
                        value["position"],
                        value["duration"],
                        "manual",
                        value.get("link_id"),
                        value.get("watched"),
                    )
            elif op == "preferences":
                prefs = value["values"]
                if not isinstance(prefs, dict):
                    raise ValueError("Неверные настройки кинотеки.")
                if "devices" in prefs:
                    devices = self.tools.devices
                    places = (
                        {"general", *[p.id for p in devices.locations.list()]}
                        if devices
                        else {"general"}
                    )
                    if set(prefs["devices"]) - places:
                        raise ValueError("Неизвестное местоположение.")
                await self.db("configure", prefs)
                self.sources.invalidate()
            elif op == "open":
                item = await self.db("title", value["title_id"])
                args = {
                    k: value[k]
                    for k in ("title_id", "episode_id", "link_id", "device")
                    if k in value
                }
                result = await self.call("cinema_open", args, f"Открой сериал {item['title']}")
            elif op == "control":
                result = await self.call(
                    "cinema_command", {"command": value["command"]}, value["command"]
                )
            elif op == "search":
                self.search_sequence += 1
                sequence = self.search_sequence
                self.matches = []
                prefs = await self.db("preferences")
                page = await self.catalogue.page(
                    with_preferences(
                        CatalogueQuery(
                            query=str(value.get("query", "")).strip()[:200],
                            kind=value.get("kind", "all"),
                            genre=value.get("genre", ""),
                            year=value.get("year", 0),
                            rating=value.get("rating", 0),
                            relax_restrictions=value.get("relax_restrictions") is True,
                        ),
                        prefs,
                    )
                )
                matches = page["items"]
                if sequence != self.search_sequence:
                    return
                self.matches = matches
                await self.publish()
                return
            elif op == "options":
                choices = await self.source_options(value["title_id"], value.get("episode_id"))
                await self.publish(source_options=choices)
                return
            elif op == "provider_page":
                self.require_playback()
                item = await self.db("title", value["title_id"])
                prefs = await self.db("preferences")
                if not prefs.allow_browser:
                    raise ToolError("Открытие браузера отключено в настройках медиатеки.")
                providers = await self.tmdb.providers(item["catalogue_key"], prefs.watch_region)
                url = providers.get("url", "")
                if not url.startswith("https://www.themoviedb.org/"):
                    raise ToolError("Страница официальных предложений недоступна.")
                await self.opener(url)
                self.message = "Открыты предложения сервисов. Запуск видео не подтверждён."
                await self.publish()
                return
            elif op == "source_search":
                self.require_playback()
                source = await self.db("source", value["source_id"])
                if not source.enabled or not source.search_url:
                    raise ValueError("Поиск на этом источнике не настроен.")
                if not (await self.db("preferences")).allow_browser:
                    raise ToolError("Открытие браузера отключено в настройках медиатеки.")
                await self.opener(
                    source.search_url.replace("{query}", quote(str(value["query"])[:200], safe=""))
                )
                self.message = "Открыт поиск на сайте источника. Результаты выбираются на сайте."
            else:
                raise ValueError("Неизвестная операция кинотеки.")
            if isinstance(result, dict):
                if result.get("error"):
                    await self.publish(error=result["error"])
                    return
                if result.get("message"):
                    self.message = result["message"]
            await self.publish(
                saved=result.get("id") if isinstance(result, dict) else None,
                source_action=source_action,
            )
        except (
            ValueError,
            KeyError,
            TypeError,
            ToolError,
            ActionDenied,
            sqlite3.Error,
            TimeoutError,
            OSError,
        ) as exc:
            # Never include a browser script, page content or private URL in errors.
            message = (
                str(exc)
                if isinstance(exc, (ToolError, ActionDenied))
                else "Не удалось сохранить или выполнить запрос. Проверь поля и доступность SQLite."
            )
            await self.publish(error=message)

    async def personal_request(self, value):
        request_id = str(value.get("request_id", ""))[:100]
        try:
            if value["op"] == "personal_enrich" and self.tmdb.credentials.configured:
                items = await self.db("library")
                eligible = [
                    i
                    for i in items
                    if i["catalogue_key"].startswith("tmdb:")
                    and not i["metadata"].get("enriched")
                    and (i["user_rating"] is not None or i["favorite"] or i["status"] == "watched")
                ]
                for item in eligible[:12]:
                    try:
                        detail = await self.tmdb.detail(item["catalogue_key"])
                        await self.db("add", {**detail, "title_id": item["id"]})
                    except ToolError:
                        continue
            if value["op"] == "recommendations":
                result = await self.personal.recommendations(
                    await self.db("library"),
                    await self.db("recommendation_data"),
                    await self.db("preferences"),
                    mood=value.get("mood", "any"),
                    ready=set(await self.db("ready_keys")),
                )
                await self.publish(
                    personal_result={"request_id": request_id, "recommendations": result}
                )
            else:
                await self.publish(
                    personal_result={
                        "request_id": request_id,
                        "profile": await self.db("analytics"),
                    }
                )
        except (ToolError, ValueError, TypeError, KeyError, sqlite3.Error):
            await self.publish(
                personal_result={
                    "request_id": request_id,
                    "error": "Не удалось обновить профиль. Проверь TMDB и повтори.",
                }
            )

    async def source_result(self, identifier, *, open_result=False, pin=False):
        if open_result:
            self.require_playback()
        row = self.sources.result(identifier)
        source = await self.db("source", row["source_id"])
        prefs = await self.db("preferences")
        if not source.enabled:
            raise ToolError("Источник отключён.")
        if open_result and (
            not row["allowed"]
            or (prefs.free_only and row["access_type"] != "free" and source.adapter != "local")
        ):
            raise ToolError(
                "Включены только бесплатные источники; тип доступа этого варианта не подходит."
            )
        if row["kind"] in {"home", "search"} or row["match"] < 0.95:
            if pin or not open_result:
                await self.db(
                    "remember_source", row["title_id"], source.id, row["episode_id"], pin=pin
                )
                self.sources.cache.clear()
            if not open_result:
                return {"message": "Источник запомнен. Точную страницу выбери и привяжи вручную."}
            if not prefs.allow_browser:
                raise ToolError("Открытие браузера отключено в настройках медиатеки.")
            if not self.tools.enabled("cinema_open"):
                raise ToolError("Открытие кино отключено в настройках доступа.")
            return await self.sources.adapter(source.model_dump()).open(row)
        item = await self.db("detail", row["title_id"])
        ep = next(e for e in item["episodes"] if e["id"] == row["episode_id"])
        if not row.get("link_id"):
            item = await self.db(
                "add",
                {
                    "title_id": item["id"],
                    "title": item["title"],
                    "kind": item["kind"],
                    "year": item["year"],
                    "source_id": source.id,
                    "url": row["url"],
                    "season": ep["season"],
                    "episode": ep["episode"],
                },
            )
            ep = next(e for e in item["episodes"] if e["id"] == row["episode_id"])
        link = next(
            link
            for link in ep["links"]
            if link["source_id"] == source.id and link["url"] == row["url"]
        )
        await self.db("remember_source", item["id"], source.id, ep["id"], link["id"], pin=pin)
        self.sources.cache.clear()
        if row["availability"] == "available":
            await self.db(
                "source_health", source.id, "available", "Проверена страница выбранного контента."
            )
        if open_result:
            return await self.call(
                "cinema_open",
                {"title_id": item["id"], "episode_id": ep["id"], "link_id": link["id"]},
                f"Открой сериал {item['title']}",
            )
        return {"message": "Источник и точная страница сохранены."}

    async def embedded_event(self, value):
        self.require_playback()
        async with self.lock:
            session = self.session
            if (
                not session
                or session["mode"] != "embedded"
                or not secrets.compare_digest(str(value.get("token", "")), session["token"])
                or not self.tools.enabled("cinema_open")
                or not self.tools.enabled("cinema_command")
            ):
                return
            if type(value.get("sequence")) is not int or value["sequence"] <= session["sequence"]:
                return
            if (
                any(
                    type(value.get(k)) not in {int, float}
                    or not math.isfinite(value[k])
                    or value[k] < 0
                    for k in ("position", "duration")
                )
                or value["position"] > value["duration"] + 2
            ):
                raise ValueError("Неверное состояние встроенного плеера.")
            event = value.get("event")
            if event not in {"periodic", "playing", "pause", "seek", "ended", "close", "error"}:
                raise ValueError("Неверное событие плеера.")
            if event == "playing" and (
                value.get("paused") is not False
                or value["position"] <= session.get("last_position", 0) + 0.05
            ):
                return
            if event == "pause" and value.get("paused") is not True:
                return
            session["sequence"] = value["sequence"]
            if event == "error":
                self.message = (
                    "Встроенное видео недоступно. Выбери другой источник или открой сайт."
                )
            else:
                if event == "playing" or session.get("counted"):
                    await self.save(value, event)
                if event == "playing":
                    self.message = "Воспроизведение во встроенном плеере подтверждено."
                    if not session.get("counted"):
                        await self.db(
                            "source_health",
                            session["target"]["source"]["id"],
                            "available",
                            playback=True,
                        )
                        session["counted"] = True
                elif event == "pause":
                    self.message = "Фильм на паузе."
            target = session["target"]
            session["last_position"] = value["position"]
            session["last_duration"] = value["duration"]
            control = session.get("control")
            if control and value.get("command_id") == control["id"]:
                expected = {"play": "playing", "pause": "pause", "seek": "seek"}[control["action"]]
                confirmed = event == expected and (event != "pause" or value.get("paused") is True)
                if confirmed and event == "seek":
                    confirmed = abs(value["position"] - control["expected"]) < 2
                if not control["future"].done() and (confirmed or event == "error"):
                    control["future"].set_result(
                        {
                            "playback_confirmed": event == "playing",
                            "result_status": "success" if confirmed else "partial_success",
                            "message": "Воспроизведение подтверждено."
                            if event == "playing"
                            else "Фильм на паузе."
                            if event == "pause"
                            else "Позиция плеера изменена."
                            if event == "seek"
                            else "Плеер сообщил об ошибке.",
                        }
                    )
            if event == "close":
                await self.stop_tracking(flush=False)
            await self.publish()
        if event == "ended":
            await self.finished(target, (await self.db("preferences")).auto_next)

    async def catalogue_page(self, value):
        request_id = str(value.get("request_id", ""))[:100]
        if request_id in self.catalogue_requests:
            return
        self.catalogue_requests.add(request_id)
        try:
            query = CatalogueQuery.model_validate(value.get("filters", {}))
            context = (
                await self.db("recommendation_context", query.kind)
                if query.feed == "recommended"
                else {}
            )
            prefs = await self.db("preferences")
            state = await self.db("recommendation_data") if query.feed == "recommended" else {}
            query = with_preferences(query, prefs)
            library = await self.db("library") if query.feed == "recommended" else []
            if query.feed == "recommended":
                context["seeds"] = [
                    item["catalogue_key"]
                    for item in sorted(
                        library,
                        key=lambda item: signal(item, state["reset_at"], prefs),
                        reverse=True,
                    )
                    if prefs.personalize
                    and signal(item, state["reset_at"], prefs) >= 1
                    and item["catalogue_key"].startswith("tmdb:")
                ][:2]
            result = await self.catalogue.page(
                query,
                seeds=context.get("seeds", []),
                excluded=set(context.get("excluded", [])),
                excluded_titles=context.get("excluded_titles", []),
            )
            if query.feed == "recommended":
                weights, samples = (
                    taste(library, state["reset_at"], prefs) if prefs.personalize else ({}, 0)
                )
                result["items"] = [
                    {**item, "reason": score(item, weights)[1]}
                    for item in sorted(
                        result["items"], key=lambda item: score(item, weights)[0], reverse=True
                    )
                    if item["catalogue_key"] not in state["dismissed"]
                ]
                result["message"] = (
                    "Локальный подбор по оценкам и метаданным; "
                    "мало истории, выводы предварительные."
                    if samples < 10
                    else "Подбор учитывает положительные и отрицательные оценки."
                )
            await self.publish(catalogue_page={"request_id": request_id, **result})
        except (ToolError, ValueError, TypeError, KeyError) as exc:
            await self.publish(
                catalogue_page={
                    "request_id": request_id,
                    "error": str(exc)
                    if isinstance(exc, ToolError)
                    else "Проверь фильтры каталога.",
                    "retry_after": exc.retry_after if isinstance(exc, TMDBRateLimited) else 0,
                }
            )
        finally:
            self.catalogue_requests.discard(request_id)

    async def call(self, name, args, text):
        token = voice_addressed.set(True)
        try:
            with request_scope(RequestContext(self.conv.device, text)):
                return json.loads(await self.tools.call(name, args))
        finally:
            voice_addressed.reset(token)

    async def execute(self, command):
        from backend.cinema.commands import command as parse

        if not self.available:
            raise ToolError("Долговременная SQLite недоступна.")
        parsed = parse(command, bool(self.session))
        if parsed is None:
            raise ToolError("Уточни команду кинотеки.")
        action = parsed["action"]
        if action in {"favorite", "rate"}:
            identifier = self.focused
            if not identifier and self.focused_catalogue:
                item = await self.db("add", await self.tmdb.detail(self.focused_catalogue))
                identifier = item["id"]
                self.focused, self.focused_catalogue = identifier, None
            identifier = identifier or (
                self.session["target"]["item"]["id"] if self.session else None
            )
            if not identifier:
                raise ToolError("Сначала открой карточку нужного фильма или сериала.")
            values = (
                {"favorite": True} if action == "favorite" else {"user_rating": parsed["rating"]}
            )
            item = await self.db("update_title", identifier, values)
            await self.publish()
            return {
                "message": f"«{item['title']}» добавлен в избранное."
                if action == "favorite"
                else f"Твоя оценка «{item['title']}»: {parsed['rating']:g} из 10.",
                "result_status": "success",
            }
        if action == "profile":
            profile = await self.db("analytics")
            await self.publish(navigate=True, active_tab="profile")
            return {
                "message": f"Открыла твой кинопрофиль: {profile['watched_movies']} фильмов, "
                f"{profile['watched_series']} сериалов и {profile['rated_count']} оценок. "
                + profile["insights"][0],
                "result_status": "success",
            }
        if action == "recommend":
            result = await self.personal.recommendations(
                await self.db("library"),
                await self.db("recommendation_data"),
                await self.db("preferences"),
                mood=parsed["mood"],
                genre=parsed.get("genre", ""),
                ready=set(await self.db("ready_keys")),
            )
            titles = [i["title"] for i in result["shelves"][0]["items"][:3]]
            await self.publish(
                navigate=True, active_tab="personal", personal_recommendations=result
            )
            return {
                "message": ("Предлагаю: " + ", ".join(titles) + ". " + result["message"])
                if titles
                else "Пока не нашла подходящие рекомендации. "
                "Проверь подключение TMDB и добавь оценки.",
                "result_status": "success" if titles else "partial_success",
            }
        if action in {"find_sources", "alternative", "episode"}:
            item = None
            if action == "find_sources":
                from backend.cinema.personal import normalized

                exact = [
                    i
                    for i in await self.db("library")
                    if normalized(i["title"]) == normalized(parsed["title"])
                    or normalized(i["metadata"].get("original_title", ""))
                    == normalized(parsed["title"])
                ]
                if not exact and self.tmdb.credentials.configured:
                    matches = await self.tmdb.search(parsed["title"])
                    exact_tmdb = [
                        i
                        for i in matches
                        if normalized(i["title"]) == normalized(parsed["title"])
                        or normalized(i["metadata"].get("original_title", ""))
                        == normalized(parsed["title"])
                    ]
                    if len(exact_tmdb) == 1:
                        item = await self.db(
                            "add", await self.tmdb.detail(exact_tmdb[0]["catalogue_key"])
                        )
                if len(exact) == 1:
                    item = exact[0]
                if item is None:
                    await self.publish(
                        navigate=True,
                        active_tab="catalogue",
                        catalogue_intent={"query": parsed["title"]},
                    )
                    return {
                        "message": "Выбери точный фильм или сериал в каталоге: "
                        "название не найдено или неоднозначно.",
                        "playback_confirmed": False,
                        "result_status": "partial_success",
                    }
            else:
                identifier = self.session["target"]["item"]["id"] if self.session else self.focused
                item = (
                    await self.db("detail", identifier)
                    if identifier
                    else await self.db("latest", "series")
                )
            episode = item["current"]
            if action == "episode":
                if item["kind"] != "series" or not parsed["season"] or not parsed["episode"]:
                    raise ToolError("Выбери сериал и номер сезона и серии от 1.")
                episode = next(
                    (
                        e
                        for e in item["episodes"]
                        if e["season"] == parsed["season"] and e["episode"] == parsed["episode"]
                    ),
                    None,
                )
                if episode is None and item["catalogue_key"].startswith("tmdb:"):
                    episodes = await self.tmdb.episodes(item["catalogue_key"], parsed["season"])
                    item = await self.db("season", item["id"], episodes)
                    episode = next(
                        (
                            e
                            for e in item["episodes"]
                            if e["season"] == parsed["season"] and e["episode"] == parsed["episode"]
                        ),
                        None,
                    )
                if episode is None:
                    raise ToolError(
                        "Такой серии пока нет в медиатеке. Добавь её страницу в карточке сериала."
                    )
                return await self.resume(item["id"], episode["id"])
            choices = await self.sources.search(
                item["id"], episode["id"], alternative=action == "alternative"
            )
            await self.publish(navigate=True, source_results=choices)
            return {
                "message": choices["message"],
                "playback_confirmed": False,
                "result_status": "success" if choices["selected_id"] else "partial_success",
            }
        if action == "planned":
            items = [item for item in await self.db("library") if item["status"] == "planned"]
            await self.publish(navigate=True, active_tab="planned")
            return {
                "message": "Хочешь посмотреть: "
                + ", ".join(item["title"] for item in items[:20])
                + "."
                if items
                else "Список «Хочу посмотреть» пока пуст.",
                "result_status": "success",
            }
        if action == "discover":
            page = await self.catalogue.page(
                with_preferences(
                    CatalogueQuery(genre=parsed["genre"], kind="movie"),
                    await self.db("preferences"),
                )
            )
            self.matches = page["items"]
            await self.publish(
                navigate=True, active_tab="catalogue", catalogue_intent={"genre": parsed["genre"]}
            )
            return {
                "message": "Нашла в TMDB: "
                + ", ".join(item["title"] for item in self.matches[:3])
                + ". Источник просмотра выбирается отдельно."
                if self.matches
                else "В TMDB ничего не найдено.",
                "result_status": "success",
            }
        if action == "plan":
            existing = [
                item
                for item in await self.db("library")
                if item["title"].casefold() == parsed["title"].casefold()
            ]
            if len(existing) == 1:
                item = existing[0]
                if item["status"] == "planned":
                    return {
                        "message": f"«{item['title']}» уже в списке «Хочу посмотреть».",
                        "result_status": "success",
                    }
                return {
                    "message": f"«{item['title']}» уже в медиатеке. "
                    "Переместить его можно в карточке.",
                    "result_status": "success",
                }
            entry = {"title": parsed["title"], "kind": "movie"}
            if self.tmdb.credentials.configured:
                matches = await self.tmdb.search(parsed["title"])
                exact = [
                    item
                    for item in matches
                    if item["title"].casefold() == parsed["title"].casefold()
                ]
                if len(exact) != 1:
                    self.matches = matches
                    await self.publish(
                        navigate=True,
                        active_tab="catalogue",
                        catalogue_intent={"query": parsed["title"]},
                    )
                    return {
                        "message": "Выбери нужный фильм в каталоге: название неоднозначно "
                        "или не найдено. Добавление пока не выполнено.",
                        "result_status": "partial_success",
                    }
                entry = await self.tmdb.detail(exact[0]["catalogue_key"])
            item = await self.db("add", entry)
            await self.publish(navigate=True, active_tab="planned")
            return {
                "message": f"Добавила «{item['title']}» в «Хочу посмотреть».",
                "result_status": "success",
            }
        if action == "title_watched":
            identifier = self.session["target"]["item"]["id"] if self.session else self.focused
            if not identifier:
                raise ToolError("Сначала открой карточку фильма или начни просмотр.")
            async with self.lock:
                await self.stop_tracking()
                item = await self.db("update_title", identifier, {"status": "watched"})
            await self.publish()
            return {
                "message": f"«{item['title']}» отмечен просмотренным.",
                "result_status": "success",
            }
        if action == "library":
            await self.publish(navigate=True)
            return {"message": "Открыла твою библиотеку.", "result_status": "success"}
        if action == "continue":
            kind = (
                "series"
                if "сериал" in command.casefold() or "последнюю серию" in command.casefold()
                else None
            )
            latest = await self.db("latest", kind, "последнюю серию" not in command.casefold())
            episode = latest["current"]
            if (
                episode["watched"]
                and latest["kind"] == "series"
                and "последнюю серию" not in command.casefold()
            ):
                index = latest["episodes"].index(episode)
                if index + 1 >= len(latest["episodes"]):
                    raise ToolError("Следующая серия ещё не добавлена. Открой сезон в карточке.")
                episode = latest["episodes"][index + 1]
            return await self.resume(latest["id"], episode["id"], device=parsed.get("device"))
        if not self.session:
            if action in {"info", "next", "watched"}:
                target = await self.db("target")
            else:
                raise ToolError("Нет активного плеера кинотеки. Скажи «Продолжи мой сериал».")
        else:
            target = self.session["target"]
        if action == "next":
            following = await self.db("next_target", target["item"]["id"], target["episode"]["id"])
            return await self.open(
                following["item"]["id"], following["episode"]["id"], device=parsed.get("device")
            )
        if (
            self.session
            and self.session["mode"] == "embedded"
            and action in {"play", "pause", "seek"}
        ):
            session = self.session
            if session.get("control"):
                raise ToolError("Дождись ответа плеера на предыдущую команду.")
            control = {
                "id": secrets.token_hex(8),
                "action": action,
                "future": asyncio.get_running_loop().create_future(),
            }
            if action == "seek":
                duration = session.get("last_duration") or target["episode"]["duration"]
                control["expected"] = max(
                    0,
                    min(
                        max(0, duration - 0.1), session.get("last_position", 0) + parsed["seconds"]
                    ),
                )
            session["control"] = control
            try:
                await self.publish(
                    embedded_control={
                        "token": session["token"],
                        "action": action,
                        "seconds": parsed.get("seconds", 0),
                        "command_id": control["id"],
                    }
                )
                return await asyncio.wait_for(control["future"], 5)
            except TimeoutError:
                raise ToolError(
                    "Встроенный плеер не подтвердил действие. Проверь его в кинотеке."
                ) from None
            finally:
                session.pop("control", None)
        async with self.lock:
            if action == "info":
                live = None
                if self.session and self.session["mode"] == "html5":
                    live = await self.sample("periodic")
                elif self.session and self.session["mode"] == "embedded":
                    live = {
                        "position": self.session.get("last_position", 0),
                        "duration": self.session.get("last_duration", 0),
                    }
                refreshed = await self.db("target", target["item"]["id"], target["episode"]["id"])
                ep = refreshed["episode"]
                if live:
                    ep = {**ep, "position": live["position"], "duration": live["duration"]}
                remaining = max(0, ep["duration"] - ep["position"])
                message = f"{target['item']['title']}. "
                if target["item"]["kind"] == "series":
                    message += f"Сезон {ep['season']}, серия {ep['episode']}. "
                if not live:
                    message += "По последней сохранённой позиции. "
                message += (
                    f"Осталось {int(remaining // 60)} мин {int(remaining % 60)} с."
                    if ep["duration"]
                    else "Длительность пока неизвестна."
                )
                return {"message": message, "result_status": "success"}
            if action == "watched":
                if self.session:
                    await self.stop_tracking()
                ep = target["episode"]
                await self.db(
                    "progress",
                    ep["id"],
                    ep["position"],
                    ep["duration"],
                    "manual",
                    target["link"]["id"],
                    True,
                )
                await self.publish()
                return {"message": "Серия отмечена просмотренной.", "result_status": "success"}
            if action == "tv" or parsed.get("device") == "airplay":
                return self.airplay_result()
            if self.session["mode"] != "html5":
                raise ToolError("Источник в ручном режиме. Управляй плеером на сайте.")
            if action == "seek":
                state = await self.session["adapter"].sample()
                position = max(0, min(state["duration"], state["position"] + parsed["seconds"]))
                result = await self.session["adapter"].control("seek", position)
            else:
                result = await self.session["adapter"].control(action)
            await self.save(
                result, "pause" if action == "pause" else "seek" if action == "seek" else "playing"
            )
            self.message = result["message"]
            await self.publish()
            return result

    def airplay_result(self):
        return {
            "playback_confirmed": False,
            "result_status": "partial_success",
            "message": self.device_options()[1]["reason"],
        }

    async def resume(self, title_id, episode_id=None, device=None):
        from urllib.parse import urlsplit

        choices = await self.db("options", title_id, episode_id)
        # Existing local-file and loopback fixtures don't need an Internet probe.
        links = [o for o in choices["options"] if o["link"] and o["source"]["enabled"]]
        public = any(
            o["source"]["adapter"] != "local"
            and urlsplit(o["source"]["base_url"]).hostname not in {"localhost", "127.0.0.1", "::1"}
            for o in links
        )
        if public or not links:
            result = await self.sources.search(title_id, episode_id, refresh=True)
            selected = result["selected_id"]
            if selected and device != "airplay":
                return await self.source_result(selected, open_result=True)
            await self.publish(navigate=True, source_results=result)
            return {
                "playback_confirmed": False,
                "result_status": "partial_success",
                "message": "Проверила источники. Выбери вариант просмотра в кинотеке.",
            }
        return await self.open(title_id, episode_id, device=device)

    async def open(self, title_id, episode_id=None, link_id=None, device=None):
        if not self.available:
            raise ToolError("Долговременная SQLite недоступна.")
        async with self.lock:
            prefs = await self.db("preferences")
            device = device or prefs.devices.get(self.location(), "mac")
            if device == "airplay":
                return self.airplay_result()
            choices = await self.db("options", title_id, episode_id)
            available = [choice for choice in choices["options"] if choice["available"]]
            if not link_id:
                item = await self.db("detail", title_id)
                ep = next(ep for ep in item["episodes"] if ep["id"] == choices["episode_id"])
                memory = item.get("source_memory", {})
                preferred = sorted(
                    available,
                    key=lambda choice: (
                        choice["source"]["id"] == memory.get("pinned_source_id"),
                        prefs.use_last_source and choice["link"]["id"] == ep["last_link_id"],
                        prefs.use_last_source
                        and choice["source"]["id"] == memory.get("last_source_id"),
                        choice["source"]["preferred"],
                        -choice["source"]["priority"],
                    ),
                    reverse=True,
                )
                link_id = (
                    preferred[0]["link"]["id"]
                    if preferred and prefs.auto_select_source
                    else next(
                        (
                            choice["link"]["id"]
                            for choice in available
                            if prefs.auto_select_source
                            and prefs.use_last_source
                            and choice["link"]["id"] == ep["last_link_id"]
                        ),
                        None,
                    )
                )
            if not link_id and (len(available) != 1 or not prefs.auto_select_source):
                await self.publish(source_options=await self.source_options(title_id, episode_id))
                return {
                    "playback_confirmed": False,
                    "result_status": "partial_success",
                    "message": "Выбери источник просмотра."
                    if available
                    else "Доступный источник не привязан. "
                    "Добавь страницу или локальный файл в карточке.",
                }
            if not link_id:
                link_id = available[0]["link"]["id"]
            target = await self.db("target", title_id, episode_id, link_id)
            source = target["source"]
            if prefs.free_only and source["adapter"] != "local" and source["access_type"] != "free":
                raise ToolError("Этот источник не отмечен бесплатным. Проверь настройки медиатеки.")
            embed = None
            if source["adapter"] == "direct" and prefs.embedded_playback:
                resolved = await self.sources.adapter(source).resolve(
                    {"url": target["link"]["url"]}
                )
                if resolved["embedded"]:
                    embed = target["link"]["url"]
            if not embed and source["adapter"] != "local" and not prefs.allow_browser:
                raise ToolError(
                    "Открытие браузера отключено. Для этого источника встроенное видео недоступно."
                )
            await self.stop_tracking()
            # The final sample may advance the position saved by a previous poll.
            target = await self.db("target", title_id, episode_id, link_id)
            adapter = self.adapter_factory(target["source"]["adapter"])
            token = secrets.token_urlsafe(32)
            base = getattr(self.bus, "cinema_url", "") if self.bus else ""
            target["tracking"] = {
                "session": token,
                "poll": prefs.poll_seconds,
                "callback": f"{base}/cinema/progress/{token}" if base else "",
            }
            starting_position = target["episode"]["position"] if prefs.resume_position else 0
            self.session = {
                "token": token,
                "target": target,
                "adapter": adapter,
                "sequence": 0,
                "observed_at": 0,
                "mode": target["source"]["adapter"],
                "errors": 0,
            }
            if embed:
                self.session["mode"] = "embedded"
                self.session["last_position"] = starting_position
                await self.db("select", target["episode"]["id"], target["link"]["id"])
                self.message = (
                    "Видео подготовлено во встроенном плеере. Ожидаю подтверждения запуска."
                )
                await self.publish(
                    embedded_player={
                        "token": token,
                        "url": embed,
                        "title": target["item"]["title"],
                        "position": starting_position,
                        "title_id": title_id,
                        "episode_id": target["episode"]["id"],
                    }
                )
                return {
                    "playback_confirmed": False,
                    "message": self.message,
                    "result_status": "partial_success",
                }
            try:
                opening_target = {
                    **target,
                    "episode": {**target["episode"], "position": starting_position},
                    "restart": not prefs.resume_position,
                }
                result = await adapter.open(opening_target)
            except (ToolError, OSError, TimeoutError) as exc:
                self.session["mode"] = "manual"
                result = {"playback_confirmed": False, "mode": "manual", "message": str(exc)}
            self.session["mode"] = result.get("mode", self.session["mode"])
            if (
                result.get("page_opened")
                or result.get("file_opened")
                or result.get("playback_confirmed")
            ):
                await self.db("select", target["episode"]["id"], target["link"]["id"])
            self.message = result.get("message", "Воспроизведение не подтверждено.")
            # Opening a page is not a viewing event. Preserve the explicitly chosen link.
            if self.session["mode"] == "html5":
                if "position" in result:
                    await self.save(result, "playing")
                self.poller = asyncio.create_task(self.poll())
            await self.publish()
            return result

    async def save(self, state, event):
        if not self.session:
            return
        if event == "playing" and not self.session.get("counted"):
            await self.db(
                "source_health", self.session["target"]["source"]["id"], "available", playback=True
            )
            self.session["counted"] = True
            await self.db("link_check", self.session["target"]["link"]["id"], "available", True)
        if not (await self.db("preferences")).save_progress:
            return
        observed = state.get("observed_at", 0)
        if type(observed) not in (int, float) or not math.isfinite(observed):
            raise ValueError("Неверная отметка времени плеера.")
        if observed and observed < self.session["observed_at"]:
            return
        self.session["observed_at"] = observed
        target = self.session["target"]
        await self.db(
            "progress",
            target["episode"]["id"],
            state["position"],
            state["duration"],
            event,
            target["link"]["id"],
        )
        target["episode"].update(position=state["position"], duration=state["duration"])

    async def sample(self, event):
        state = await self.session["adapter"].sample()
        await self.save(state, "ended" if state.get("ended") else event)
        return state

    async def poll(self):
        while self.session and not self.closed:
            prefs = await self.db("preferences")
            await asyncio.sleep(prefs.poll_seconds)
            try:
                async with self.lock:
                    if not self.session:
                        return
                    if not self.tools.enabled("cinema_open") or not self.tools.enabled(
                        "cinema_command"
                    ):
                        await self.stop_tracking()
                        return
                    state = await self.sample("periodic")
                    self.session["errors"] = 0
                    ended_target = self.session["target"] if state.get("ended") else None
                    await self.publish()
                if ended_target:
                    await self.finished(ended_target, prefs.auto_next)
                    return
            except (ToolError, OSError, TimeoutError, sqlite3.Error, ValueError):
                async with self.lock:
                    if not self.session:
                        return
                    self.session["errors"] += 1
                    if self.session["errors"] >= 3:
                        self.message = (
                            "Плеер недоступен. Сохранена последняя подтверждённая позиция; "
                            "проверь прогресс вручную."
                        )
                        await self.stop_tracking(flush=False)
                        await self.publish()
                        return

    async def finished(self, target, auto_next):
        async with self.lock:
            if not self.session or self.session["target"] is not target:
                return
            await self.stop_tracking(flush=False)
        if auto_next and self.conv:
            try:
                following = await self.db(
                    "next_target", target["item"]["id"], target["episode"]["id"]
                )
                result = await self.call(
                    "cinema_open",
                    {"title_id": following["item"]["id"], "episode_id": following["episode"]["id"]},
                    f"Открой сериал {following['item']['title']}",
                )
                if result.get("error"):
                    self.message = result["error"]
            except (ValueError, ToolError):
                self.message = "Просмотр завершён. Следующая серия недоступна; добавь её страницу."
        await self.publish()

    async def telemetry(self, token, request_origin, value):
        # Only the active episode's random capability is accepted; no arbitrary IDs/URLs.
        async with self.lock:
            session = self.session
            if (
                not session
                or not secrets.compare_digest(token, session["token"])
                or session["mode"] != "html5"
                or request_origin != origin(session["target"]["link"]["url"])
            ):
                return False
            if (
                not self.tools.enabled("cinema_open")
                or not self.tools.enabled("cinema_command")
                or session["mode"] != "html5"
            ):
                return False
            if (
                not isinstance(value, dict)
                or type(value.get("sequence")) is not int
                or value["sequence"] <= session["sequence"]
            ):
                return False
            if any(
                type(value.get(k)) not in (int, float)
                or not math.isfinite(value[k])
                or value[k] < 0
                for k in ("position", "duration")
            ):
                return False
            event = value.get("event")
            if event not in {"periodic", "pause", "ended", "seek", "close", "playing"}:
                return False
            session["sequence"] = value["sequence"]
            await self.save(value, event)
            target = session["target"]
            prefs = await self.db("preferences")
            if event == "close":
                await self.stop_tracking(flush=False)
            await self.publish()
        if event == "ended":
            await self.finished(target, prefs.auto_next)
        return True

    async def source_options(self, title_id, episode_id=None):
        choices = await self.db("options", title_id, episode_id)
        item = await self.db("title", title_id)
        if item["catalogue_key"].startswith("tmdb:") and self.tmdb.credentials.configured:
            try:
                prefs = await self.db("preferences")
                choices["official"] = await self.tmdb.providers(
                    item["catalogue_key"], prefs.watch_region
                )
            except ToolError as exc:
                choices["provider_error"] = str(exc)
        return choices

    async def stop_tracking(self, flush=True):
        if self.poller and self.poller is not asyncio.current_task():
            self.poller.cancel()
        self.poller = None
        if self.session:
            if flush and self.session["mode"] == "html5":
                with contextlib.suppress(
                    ToolError, OSError, TimeoutError, ValueError, sqlite3.Error
                ):
                    await self.sample("close")
            await self.session["adapter"].detach()
        self.session = None
        self.message = ""

    async def close(self):
        for task in list(self.jobs):
            task.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        async with self.lock:
            await self.stop_tracking()
            self.closed = True
        await self.db("close")
        if hasattr(self.tmdb, "close"):
            await self.tmdb.close()
        self.worker.shutdown(wait=False)
