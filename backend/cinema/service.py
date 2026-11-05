"""One bounded player session; async HUD endpoint and guarded voice tools."""

import asyncio
import contextlib
import functools
import json
import math
import re
import secrets
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import httpx

from backend.cinema.adapters import (
    HTML5Adapter,
    LocalFileAdapter,
    ManualAdapter,
    SafariBridge,
    open_page,
)
from backend.cinema.catalogue import Catalogue
from backend.cinema.classification import FULL_TYPES, PROMO_TYPES
from backend.cinema.commands import command
from backend.cinema.filters import with_preferences
from backend.cinema.internet import InternetSearch, SearchCredentials, SearchProviders
from backend.cinema.keychain import TavilyCredentials
from backend.cinema.models import CatalogueQuery, Source, origin
from backend.cinema.native_player import AVPlayerAdapter
from backend.cinema.personal import Recommender, score, signal, taste
from backend.cinema.playback import (
    PlaybackManager,
    PlaybackState,
    SourceResolver,
    SourceValidator,
    playback_error,
)
from backend.cinema.sources import SourceManager, capabilities
from backend.cinema.store import CinemaStore
from backend.cinema.tavily import TavilySearch
from backend.cinema.tmdb import TMDB, TMDBCredentials, TMDBRateLimited
from backend.context import RequestContext, request_scope, voice_addressed
from backend.mac_agent import LocalMacAgent
from backend.paths import DESKTOP
from backend.security import ActionDenied, Risk, command_text
from backend.tools.registry import Tool, ToolError


class Cinema:
    def __init__(
        self,
        tools,
        settings,
        memory,
        *,
        adapter_factory=None,
        native_factory=None,
        opener=open_page,
        tmdb=None,
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
        self.safari = SafariBridge(settings)
        self.adapter_factory = adapter_factory or (
            lambda name: (
                HTML5Adapter(self.safari)
                if name == "html5"
                else LocalFileAdapter()
                if name == "local"
                else ManualAdapter(opener)
            )
        )
        self.native_factory = native_factory or (lambda: AVPlayerAdapter(self.s))
        self.opener = opener
        self.bus = self.conv = None
        self.playback = PlaybackManager()
        self.poller = None
        self.jobs = set()
        self.lock = asyncio.Lock()
        self.closed = False
        self.message = ""
        self.tmdb = tmdb or TMDB(TMDBCredentials(settings.data_path / "integrations"))
        self.catalogues = {"TMDB": self.tmdb}
        self.catalogue = Catalogue(self.tmdb)
        self.sources = SourceManager(self.db, opener=self.opener)
        self.resolver = SourceResolver(self.sources)
        self.validator = SourceValidator()
        self.search_credentials = SearchCredentials(settings.data_path / "integrations")
        self.tavily_credentials = TavilyCredentials()
        self.tavily = TavilySearch(self.tavily_credentials)
        self.sources.web = SearchProviders(
            self.sources.http,
            {
                "tavily": InternetSearch(
                    self.tavily_credentials, self.sources.http, provider=self.tavily, name="Tavily"
                ),
                "brave": InternetSearch(self.search_credentials, self.sources.http),
            },
        )
        self.watch_confirmations = {}
        self.open_approvals = {}
        self.watch_sequence = 0
        self.next_episode = None
        self.personal = Recommender(self.tmdb, self.catalogue)
        self.catalogue_requests = set()
        self.matches = []
        self.focused = None
        self.focused_catalogue = None
        self.search_sequence = 0
        self.import_proposals = {}
        self.register()

    @property
    def session(self):
        return self.playback.session

    @session.setter
    def session(self, value):
        self.playback.session = value

    @property
    def player(self):
        return self.session.controller

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
                "player": {"type": "string", "enum": ["native", "safari", "embedded"]},
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
        tavily_status = await asyncio.to_thread(self.tavily_credentials.status)
        provider = data["preferences"]["search_provider"]
        search_status = {
            "tavily": tavily_status,
            "brave": {"configured": self.search_credentials.configured, "storage": "private_file"},
        }
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
                "playback": self.playback.snapshot(),
                "message": self.message,
                "matches": self.matches,
                "tmdb": {"configured": self.tmdb.credentials.configured},
                "internet_search": {
                    "provider": provider,
                    "configured": search_status[provider]["configured"],
                    "providers": search_status,
                },
                "next_episode": self.next_episode,
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

    async def trace(self, stage, status, message, title_id="", episode_id="", session=""):
        await self.db("diagnostic", stage, status, message, title_id, episode_id, session)
        await self.publish(
            cinema_stage={
                "stage": stage,
                "status": status,
                "message": message,
                "title_id": title_id,
                "episode_id": episode_id,
            }
        )

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
            elif op == "search_credentials":
                await asyncio.to_thread(self.search_credentials.save, value.get("credential", ""))
                self.sources.invalidate()
                self.message = (
                    "Ключ Brave Search API сохранён локально."
                    if self.search_credentials.configured
                    else "Ключ Brave Search API удалён."
                )
            elif op == "tavily_credentials":
                await asyncio.to_thread(self.tavily_credentials.save, value.get("credential", ""))
                self.sources.invalidate()
                self.message = (
                    "Ключ Tavily сохранён в macOS Keychain."
                    if value.get("credential")
                    else "Ключ Tavily удалён из macOS Keychain."
                )
            elif op == "tavily_check":
                try:
                    connection = await self.tavily.check()
                except ToolError as exc:
                    connection = {
                        "provider": "tavily",
                        "ok": False,
                        "code": getattr(exc, "code", "keychain"),
                        "message": str(exc),
                    }
                await self.publish(search_connection=connection)
                return
            elif op == "cinema_diagnostics":
                await self.publish(
                    cinema_diagnostics=await self.db(
                        "diagnostics", str(value.get("title_id", ""))[:64]
                    )
                )
                return
            elif op == "safari_check":
                permission = await self.safari.executor.permission()
                result = {
                    "method": "Apple Events",
                    "automation": permission.get("automation", "unknown"),
                    "extension": "not_installed",
                }
                if result["automation"] == "granted":
                    try:
                        reply = await self.safari.executor._run(
                            'tell application "Safari"\nif (count of windows) is 0 then '
                            'return "no_tab"\nreturn do JavaScript "1+1" in current tab of '
                            "front window\nend tell"
                        )
                        result["javascript"] = "granted" if reply == "2" else "no_tab"
                    except ToolError:
                        result["javascript"] = "not_granted"
                await self.publish(safari_check=result)
                return
            elif op == "safari_access":
                self.require_playback()
                result = await self.safari.executor.request_access()
                await self.publish(
                    safari_check={
                        "method": "Apple Events",
                        "extension": "not_installed",
                        "automation": result.get("automation", "unknown"),
                    }
                )
                return
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
            elif op == "trailer":
                result = await self.trailer(value.get("key"), value.get("title_id"))
            elif op == "watch":
                result = await self.resume(
                    value["title_id"],
                    value.get("episode_id"),
                    value.get("device"),
                    request_id=str(value.get("request_id", ""))[:100],
                    player=value.get("player"),
                )
            elif op == "watch_cancel":
                self.watch_sequence += 1
                return
            elif op == "watch_confirm":
                self.require_playback()
                confirmation = self.watch_confirmations.pop(str(value.get("token", "")), None)
                if not confirmation or confirmation[0] < time.monotonic():
                    raise ToolError("Подтверждение устарело. Выбери источник ещё раз.")
                result = await self.source_result(
                    confirmation[1],
                    open_result=True,
                    manual_choice=confirmation[2],
                    approved=True,
                    player=confirmation[3] if len(confirmation) > 3 else None,
                )
            elif op == "find_sources":
                choices = await self.resolver.search(
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
                    manual_choice=op == "source_result_open",
                )
                source_action = {
                    "result_id": value["result_id"],
                    "message": result.get("message", ""),
                }
            elif op == "source_unpin":
                await self.db("unpin_source", value["title_id"])
                self.sources.invalidate()
            elif op in {"playback_control", "playback_switch", "playback_voice"}:
                self.require_playback()
                token = str(value.get("token", ""))
                if not self.session or not secrets.compare_digest(token, self.session["token"]):
                    raise ToolError("Просмотр изменился. Обнови экран плеера.")
                if op == "playback_switch":
                    target = self.session["target"]
                    result = await self.open(
                        target["item"]["id"],
                        target["episode"]["id"],
                        value.get("link_id") or target["link"]["id"],
                        player=value.get("player"),
                    )
                else:
                    result = await self.viewer_control(value)
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
                    for k in ("title_id", "episode_id", "link_id", "device", "player")
                    if k in value
                }
                if value.get("link_id") and value.get("device") != "airplay":
                    choices = await self.resolver.search(
                        item["id"], value.get("episode_id"), saved_only=True
                    )
                    row = next(
                        (r for r in choices["results"] if r.get("link_id") == value["link_id"]),
                        None,
                    )
                    if not row:
                        raise ToolError(
                            "Сохранённая ссылка недоступна. Повтори автоматический поиск."
                        )
                    result = await self.source_result(
                        row["id"], open_result=True, manual_choice=True, player=value.get("player")
                    )
                else:
                    kind = "сериал" if item["kind"] == "series" else "фильм"
                    result = await self.call(
                        "cinema_open",
                        args,
                        f"Открой {kind} {item['title']}",
                    )
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

    async def trace_source(self, row):
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        url = urlsplit(row.get("url", ""))
        query = [
            (k, "[redacted]" if re.search(r"token|key|auth|signature|credential", k, re.I) else v)
            for k, v in parse_qsl(url.query, keep_blank_values=True)
        ]
        address = urlunsplit((url.scheme, url.netloc, url.path, urlencode(query), ""))
        accepted = row.get("content_verified") is True and row.get("video_type") in FULL_TYPES
        await self.trace(
            "VERIFY",
            "success"
            if accepted
            else "rejected"
            if row.get("video_type") in PROMO_TYPES
            else "limited",
            json.dumps(
                {
                    "source": row.get("source_name", row.get("source_id", "")),
                    "url": address,
                    "video_type": row.get("video_type", "unknown"),
                    "accepted": accepted,
                    "reason": row.get("classification_reason", row.get("reason", "")),
                },
                ensure_ascii=False,
            ),
            row.get("title_id", ""),
            row.get("episode_id", ""),
        )

    async def trailer(self, key=None, title_id=None):
        self.require_playback()
        if title_id:
            key = (await self.db("detail", title_id)).get("catalogue_key")
        if not key:
            raise ToolError("Для официального трейлера открой карточку фильма или сериала из TMDB.")
        if not (await self.db("preferences")).allow_browser:
            raise ToolError("Открытие браузера отключено в настройках медиатеки.")
        video = await self.tmdb.official_trailer(key)
        await self.opener(video["url"])
        await self.trace(
            "OPEN_SAFARI",
            "success",
            "Открыт только официальный трейлер: " + video["url"],
            title_id or "",
        )
        return {
            "page_opened": True,
            "video_type": "trailer",
            "playback_confirmed": False,
            "message": "Открыт официальный трейлер. Полный фильм и прогресс просмотра не изменены.",
        }

    async def source_result(
        self,
        identifier,
        *,
        open_result=False,
        pin=False,
        manual_choice=False,
        approved=False,
        player=None,
    ):
        if open_result:
            self.require_playback()
        row = self.sources.result(identifier)
        await self.trace_source(row)
        if row.get("video_type") in PROMO_TYPES:
            raise ToolError("Источник для просмотра не найден: это трейлер, тизер или фрагмент.")
        try:
            source = await self.db("source", row["source_id"])
            new_source = False
        except ValueError:
            if not row.get("source_config"):
                raise ToolError(
                    "Этот результат содержит диагностику; открой настройки поиска."
                ) from None
            source = Source.model_validate(row["source_config"])
            new_source = True
        prefs = await self.db("preferences")
        if not source.enabled:
            raise ToolError("Источник отключён.")
        status = (
            row.get("access_status", "UNKNOWN")
            if source.access_status == "MIXED" or row.get("cost_verified")
            else source.access_status
        )
        if (
            open_result
            and prefs.free_only
            and source.adapter != "local"
            and status != "FREE"
            and not (manual_choice and status == "UNKNOWN")
        ):
            raise ToolError(
                "Включены только бесплатные источники; тип доступа этого варианта не подходит."
            )
        browser_verified = (
            row.get("previously_played")
            and row.get("scope") in {"episode", "movie"}
            and row["availability"] in {"unknown", "restricted"}
        )
        if (
            row["kind"] != "page"
            or row["match"] < 0.95
            or (row["availability"] != "available" and not browser_verified)
        ):
            if not open_result:
                await self.db(
                    "remember_source", row["title_id"], source.id, row["episode_id"], pin=pin
                )
                self.sources.cache.clear()
                return {
                    "message": (
                        "Источник выбран вручную. Совпадение не найдено; "
                        "для просмотра нужна проверенная конкретная страница."
                    )
                }
            raise ToolError(
                row.get("reason")
                or "Точная страница не найдена; открытие главной страницы отменено."
            )
        item = await self.db("detail", row["title_id"])
        memory = item.get("source_memory", {})
        voice_change = (
            prefs.remember_voice
            and memory.get("last_voice")
            and row.get("voice", "") != memory["last_voice"]
        )
        paid = prefs.ask_paid and (
            status in {"PAID", "MIXED"}
            or (status != "FREE" and source.access_status in {"PAID", "MIXED"})
        )
        if (
            open_result
            and not approved
            and (paid or voice_change or (new_source and not manual_choice))
        ):
            token = secrets.token_urlsafe(32)
            self.watch_confirmations = {
                k: v for k, v in self.watch_confirmations.items() if v[0] > time.monotonic()
            }
            if len(self.watch_confirmations) >= 16:
                self.watch_confirmations.pop(next(iter(self.watch_confirmations)))
            self.watch_confirmations[token] = (
                time.monotonic() + 180,
                identifier,
                manual_choice,
                player,
            )
            message = (
                "Найден новый сайт. Его безопасность и работа видео ещё не подтверждены. "
                "Открыть и запомнить этот источник?"
                if new_source
                else "Изменится источник или озвучка. Прежний прогресс сохранится. Продолжить?"
                if voice_change
                else (
                    "Сервис содержит платный контент. Открыть найденную "
                    "страницу? Оплата и вход остаются на сайте."
                )
            )
            await self.publish(
                watch_confirmation={
                    "token": token,
                    "source_name": source.name,
                    "url": row["url"],
                    "message": message,
                }
            )
            return {"message": message, "playback_confirmed": False}
        if new_source:
            await self.db("save_source", source.model_dump())
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
                    "voice": row.get("voice", ""),
                },
            )
            ep = next(e for e in item["episodes"] if e["id"] == row["episode_id"])
        link = next(
            link
            for link in ep["links"]
            if link["source_id"] == source.id and link["url"] == row["url"]
        )
        await self.db(
            "verify_link",
            link["id"],
            {
                "scope": row.get("scope", "episode"),
                "access_status": status,
                "title": item["title"],
                "year": item["year"],
                "content_kind": item["kind"],
                "cost_verified": row.get("cost_verified", False),
                "license": row.get("license", ""),
                "region_access": row.get("region_access", "unknown"),
                "region_checked": prefs.watch_region,
                "video_type": row.get("video_type", "unknown"),
                "content_verified": row.get("content_verified", False),
                "classification_reason": row.get("classification_reason", ""),
            },
        )
        if not open_result or manual_choice:
            await self.db("remember_source", item["id"], source.id, ep["id"], link["id"], pin=pin)
        self.sources.cache.clear()
        if row["availability"] == "available" and row.get("content_verified"):
            await self.db(
                "source_health", source.id, "available", "Проверена страница выбранного контента."
            )
        if open_result:
            self.open_approvals[link["id"]] = (time.monotonic() + 30, source.model_dump())
            return await self.call(
                "cinema_open",
                {
                    "title_id": item["id"],
                    "episode_id": ep["id"],
                    "link_id": link["id"],
                    **({"player": player} if player else {}),
                },
                f"Открой {'сериал' if item['kind'] == 'series' else 'фильм'} {item['title']}",
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
            if event not in {
                "periodic",
                "playing",
                "pause",
                "seek",
                "ended",
                "close",
                "error",
                "blocked",
            }:
                raise ValueError("Неверное событие плеера.")
            if event == "playing" and (
                value.get("paused") is not False
                or value["position"] <= session.get("last_position", 0) + 0.05
            ):
                return
            if event == "pause" and value.get("paused") is not True:
                return
            if event == "ended" and (
                value.get("ended") is not True or value["position"] < value["duration"] - 2
            ):
                return
            session["sequence"] = value["sequence"]
            if event in {"error", "blocked"}:
                self.message = (
                    "Встроенное видео недоступно. Выбери другой источник или открой сайт."
                )
                if event == "blocked":
                    self.message = "Браузер запретил автозапуск. Нажми ▶ во встроенном плеере."
                self.playback.failure(
                    playback_error(
                        "gesture_required" if event == "blocked" else "embedded_unavailable",
                        self.message,
                    )
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
            if event not in {"error", "blocked"}:
                session["last_position"] = value["position"]
                session["last_duration"] = value["duration"]
            control = session.get("control")
            if control and value.get("command_id") == control["id"]:
                expected = {"play": "playing", "pause": "pause", "seek": "seek"}[control["action"]]
                confirmed = event == expected and (event != "pause" or value.get("paused") is True)
                if confirmed and event == "seek":
                    confirmed = abs(value["position"] - control["expected"]) < 2
                if not control["future"].done() and (confirmed or event in {"error", "blocked"}):
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
        if action == "trailer":
            if parsed.get("title"):
                matches = [
                    i
                    for i in await self.db("library")
                    if i["title"].casefold() == parsed["title"].casefold()
                ]
                if len(matches) != 1:
                    raise ToolError(
                        "Открой карточку нужного фильма или сериала и выбери «Трейлер»."
                    )
                return await self.trailer(title_id=matches[0]["id"])
            return await self.trailer(self.focused_catalogue, self.focused)
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
                if parsed.get("kind"):
                    exact = [i for i in exact if i["kind"] == parsed["kind"]]
                if not exact and self.tmdb.credentials.configured:
                    matches = await self.tmdb.search(parsed["title"])
                    exact_tmdb = [
                        i
                        for i in matches
                        if normalized(i["title"]) == normalized(parsed["title"])
                        or normalized(i["metadata"].get("original_title", ""))
                        == normalized(parsed["title"])
                    ]
                    if parsed.get("kind"):
                        exact_tmdb = [i for i in exact_tmdb if i["kind"] == parsed["kind"]]
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
            if parsed.get("watch"):
                return await self.resume(
                    item["id"], episode["id"], exact_only=item["kind"] == "series"
                )
            choices = await self.resolver.search(
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
            kind = parsed.get("kind") or (
                "series"
                if "сериал" in command.casefold() or "последнюю серию" in command.casefold()
                else None
            )
            if parsed.get("focused_watch") and self.focused:
                selected = await self.db("detail", self.focused)
                if selected["kind"] != kind:
                    raise ToolError(
                        "Выбран другой тип контента. Открой карточку нужного фильма или сериала."
                    )
                return await self.resume(selected["id"], selected["current"]["id"])
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
            player = "native" if self.session and self.session["mode"] == "native" else None
            following = await self.db("next_target", target["item"]["id"], target["episode"]["id"])
            return await self.open(
                following["item"]["id"],
                following["episode"]["id"],
                device=parsed.get("device"),
                player=player,
            )
        if (
            self.session
            and self.session["mode"] == "embedded"
            and action in {"play", "pause", "seek"}
        ):
            return await self.embedded_control(
                action, parsed.get("seconds", 0), self.session["token"]
            )
        async with self.lock:
            if action == "info":
                live = None
                if self.session and self.session["mode"] in {"html5", "native"}:
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
            if self.session["mode"] not in {"html5", "native"}:
                raise ToolError("Источник в ручном режиме. Управляй плеером на сайте.")
            if action == "seek":
                state = await self.player.sample()
                position = max(0, min(state["duration"], state["position"] + parsed["seconds"]))
                self.session["allow_zero"] = position == 0
                result = await self.player.control("seek", position)
            else:
                result = await self.player.control(action)
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

    async def resume(self, *args, **kwargs):
        self.playback.transition(PlaybackState.SEARCHING, "Ищем сохранённый источник просмотра.")
        try:
            result = await self._resume(*args, **kwargs)
            if self.playback.state == PlaybackState.SEARCHING:
                self.playback.transition(
                    PlaybackState.BLOCKED,
                    result.get("message", "Выбери источник."),
                    "source_selection",
                )
                await self.publish()
            return result
        except (ToolError, OSError, TimeoutError, ValueError) as exc:
            self.playback.failure(exc)
            await self.publish()
            raise

    async def _resume(
        self,
        title_id,
        episode_id=None,
        device=None,
        *,
        request_id="",
        exact_only=False,
        player=None,
    ):
        from urllib.parse import urlsplit

        self.require_playback()
        self.watch_sequence += 1
        sequence = self.watch_sequence
        prefs = await self.db("preferences")
        item = await self.db("detail", title_id)

        async def stage(message):
            await self.publish(
                watch_progress={"request_id": request_id, "title_id": title_id, "message": message}
            )

        await stage("Ищем сериал…" if item["kind"] == "series" else "Ищем фильм…")
        await self.trace(
            "SEARCH",
            "running",
            "Проверяем сохранённую серию, источник, адаптеры и поисковый API.",
            title_id,
            episode_id or "",
        )
        if (
            item.get("catalogue_key")
            and not item.get("metadata", {}).get("search_enriched")
            and self.tmdb.credentials.configured
        ):
            try:
                detail = await self.tmdb.detail(item["catalogue_key"])
                item = await self.db("add", {**detail, "title_id": title_id})
            except (ToolError, httpx.HTTPError, OSError, TimeoutError):
                await stage("TMDB временно недоступен. Используем сохранённые название и год.")
        # A verified series page can be reused for another episode, without inventing episode URLs.
        ep = next(
            (e for e in item["episodes"] if e["id"] == episode_id),
            item["current"] if episode_id is None else None,
        )
        if not ep:
            raise ToolError("Серия не найдена.")
        if not ep["links"] and item["kind"] == "series" and prefs.use_last_source:
            memory = item.get("source_memory", {})
            for previous in item["episodes"]:
                link = next(
                    (
                        candidate
                        for candidate in previous["links"]
                        if candidate["source_id"] == memory.get("last_source_id")
                        and candidate["url"] == memory.get("page_url")
                    ),
                    None,
                )
                verification = await self.db("verify_link", link["id"]) if link else {}
                if verification.get("scope") == "series":
                    item = await self.db(
                        "add",
                        {
                            "title_id": title_id,
                            "title": item["title"],
                            "year": item["year"],
                            "kind": item["kind"],
                            "season": ep["season"],
                            "episode": ep["episode"],
                            "source_id": link["source_id"],
                            "url": link["url"],
                        },
                    )
                    new_link = next(e for e in item["episodes"] if e["id"] == ep["id"])["links"][0]
                    await self.db(
                        "verify_link",
                        new_link["id"],
                        {k: v for k, v in verification.items() if k != "checked_at"},
                    )
                    break
        choices = await self.db("options", title_id, episode_id)
        await self.trace(
            "VERIFY",
            "success",
            "Сохранённая привязка серии проверена; готовность видео проверяется в Safari.",
            title_id,
            choices["episode_id"],
        )
        # Existing local-file and loopback fixtures don't need an Internet probe.
        links = [o for o in choices["options"] if o["link"] and o["source"]["enabled"]]
        public = any(
            o["source"]["adapter"] != "local"
            and urlsplit(o["source"]["base_url"]).hostname not in {"localhost", "127.0.0.1", "::1"}
            for o in links
        )
        if public or not links:
            await stage("Проверяем источники…")
            await self.trace(
                "VERIFY",
                "running",
                "Проверяем точную страницу, название, год, тип и номера серии.",
                title_id,
                ep["id"],
            )
            result = (
                await self.resolver.search(title_id, episode_id, saved_only=True)
                if prefs.use_last_source
                else None
            )
            saved_exact = bool(
                result
                and result["selected_id"]
                and (
                    item["kind"] == "movie"
                    or self.sources.result(result["selected_id"]).get("scope") == "episode"
                )
            )
            if not saved_exact and prefs.auto_search:
                # Official alternative with public metadata. Never classified as free.
                configured = await self.db("sources")
                if not any(s["base_url"].rstrip("/") == "https://kion.ru" for s in configured):
                    await self.db(
                        "save_source",
                        {
                            "name": "KION · официальный",
                            "base_url": "https://kion.ru",
                            "search_mode": "sitemap",
                            "adapter": "html5",
                            "category": "streaming",
                            "access_status": "MIXED",
                            "language": "ru",
                            "priority": 100,
                        },
                    )
                result = await self.resolver.search(title_id, episode_id)
            if not result:
                result = await self.resolver.search(title_id, episode_id, saved_only=True)
            if sequence != self.watch_sequence:
                return {"playback_confirmed": False, "message": "Поиск заменён новым запросом."}
            selected = result["selected_id"]
            for candidate in result["results"]:
                await self.trace_source(candidate)
            if (
                exact_only
                and item["kind"] == "series"
                and selected
                and self.sources.result(selected).get("scope") != "episode"
            ):
                selected = None
            await self.trace(
                "VERIFY",
                "success" if selected else "limited",
                "Страница найдена; видео ещё не подтверждено." if selected else result["message"],
                title_id,
                ep["id"],
            )
            if selected and prefs.auto_open and device != "airplay":
                await stage("Найдено совпадение… Открываем воспроизведение…")
                result = await self.source_result(selected, open_result=True, player=player)
                await stage(result.get("message", "Открытие завершено; запуск ещё не подтверждён."))
                return result
            await self.publish(navigate=True, source_results={**result, "request_id": request_id})
            return {
                "playback_confirmed": False,
                "result_status": "partial_success",
                "message": result["message"]
                if not selected
                else "Выбери вариант просмотра в кинотеке.",
            }
        if not prefs.auto_open:
            result = await self.resolver.search(title_id, episode_id, saved_only=True)
            await self.publish(source_results={**result, "request_id": request_id})
            return {"playback_confirmed": False, "message": "Выбери сохранённый вариант просмотра."}
        return await self.open(title_id, episode_id, device=device, player=player)

    async def open(self, title_id, episode_id=None, link_id=None, device=None, *, player=None):
        if player not in {None, "safari", "native", "embedded"}:
            raise ValueError("Неизвестный плеер.")
        self.playback.transition(PlaybackState.VALIDATING_SOURCE, "Проверяем выбранный источник.")
        try:
            result = await self._open(title_id, episode_id, link_id, device, player=player)
            if self.session and self.session["mode"] == "embedded":
                pass
            elif self.session and not result.get("error_code"):
                self.playback.outcome(result)
            elif result.get("error_code"):
                pass
            else:
                self.playback.transition(
                    PlaybackState.BLOCKED,
                    result.get("message", "Выбери источник."),
                    "source_selection",
                )
            await self.publish()
            return {**result, "playback_state": self.playback.state.value}
        except (ToolError, OSError, TimeoutError, ValueError) as exc:
            self.playback.failure(exc)
            await self.publish()
            raise

    async def _open(self, title_id, episode_id=None, link_id=None, device=None, *, player=None):
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
                preferred = self.resolver.preferred(available, item, ep, prefs)
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
            link_verification = await self.db("verify_link", target["link"]["id"])
            playback_source = self.validator.validate(target, link_verification)
            approval = self.open_approvals.pop(target["link"]["id"], None)
            approved = bool(approval and approval[0] > time.monotonic() and approval[1] == source)
            status = source.get("access_status", "UNKNOWN")
            if (
                prefs.free_only
                and source["adapter"] != "local"
                and status != "FREE"
                and not approved
            ):
                raise ToolError("Этот источник не отмечен бесплатным. Проверь настройки медиатеки.")
            if prefs.ask_paid and status in {"PAID", "MIXED"} and not approved:
                raise ToolError(
                    "Выбери источник через «Найти и смотреть», чтобы подтвердить "
                    "переход на платный сервис."
                )
            embed = None
            if (
                source["adapter"] == "direct"
                and (prefs.embedded_playback or player in {"native", "embedded"})
                and (not prefs.open_in_safari or player in {"native", "embedded"})
                and player != "safari"
            ):
                resolved = await self.sources.adapter(source).resolve(
                    {"url": target["link"]["url"]}
                )
                if (
                    resolved["embedded"]
                    and link_verification.get("content_verified") is True
                    and link_verification.get("video_type")
                    == ("episode" if target["item"]["kind"] == "series" else "full_movie")
                ):
                    embed = target["link"]["url"]
            from pathlib import Path

            native_format = Path(target["link"]["url"].split("?", 1)[0]).suffix.lower() in {
                ".mp4",
                ".m4v",
                ".mov",
                ".m3u8",
            }
            native = (
                native_format
                and (
                    player == "native"
                    or (
                        DESKTOP
                        and player is None
                        and prefs.embedded_playback
                        and not prefs.open_in_safari
                    )
                )
                and (bool(embed) or source["adapter"] == "local")
            )
            if player == "native" and not native:
                raise playback_error(
                    "unsupported_source",
                    "Нативный просмотр доступен для совместимого прямого видео "
                    "или локального файла. "
                    "Соответствие сетевого видео должно быть подтверждено.",
                )
            if (
                not embed
                and not native
                and source["adapter"] != "local"
                and not prefs.allow_browser
            ):
                raise ToolError(
                    "Открытие браузера отключено. Для этого источника встроенное видео недоступно."
                )
            await self.stop_tracking()
            # The final sample may advance the position saved by a previous poll.
            target = await self.db("target", title_id, episode_id, link_id)
            verification = await self.db("verify_link", target["link"]["id"])
            if not prefs.remember_voice:
                target["link"] = {**target["link"], "voice": ""}
                playback_source.voice = ""
            series_page = (
                target["item"]["kind"] == "series" and verification.get("scope") == "series"
            )
            adapter = self.adapter_factory(
                "manual"
                if series_page
                else "html5"
                if source["adapter"] == "manual"
                or source["adapter"] == "direct"
                or player == "safari"
                else target["source"]["adapter"]
            )
            if native:
                adapter = self.native_factory()
                playback_source.adapter_id = "avkit"
            elif embed:
                playback_source.adapter_id = "webkit"
            else:
                playback_source.adapter_id = (
                    "safari" if isinstance(adapter, HTML5Adapter) else source["adapter"]
                )
            token = secrets.token_urlsafe(32)
            base = getattr(self.bus, "cinema_url", "") if self.bus else ""
            target["tracking"] = {
                "session": token,
                "poll": prefs.poll_seconds,
                "callback": f"{base}/cinema/progress/{token}" if base else "",
            }
            starting_position = (
                max(0, target["episode"]["position"] - prefs.resume_rewind_seconds)
                if prefs.resume_position
                else 0
            )
            self.next_episode = None
            self.playback.bind(
                playback_source,
                **{
                    "token": token,
                    "target": target,
                    "adapter": adapter,
                    "sequence": 0,
                    "observed_at": 0,
                    "mode": target["source"]["adapter"],
                    "errors": 0,
                    "last_position": starting_position,
                    "sample_position": starting_position,
                    "confirmed": False,
                    "allow_zero": not prefs.resume_position,
                },
            )
            if embed and not native:
                self.playback.transition(
                    PlaybackState.WAITING_FOR_PLAYER, "Ожидаем встроенный плеер."
                )
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
                        "autoplay": prefs.autoplay,
                    }
                )
                return {
                    "playback_confirmed": False,
                    "message": self.message,
                    "result_status": "partial_success",
                }
            try:

                async def diagnostic(stage, status, message):
                    self.playback.stage(stage, status, message)
                    await self.trace(stage, status, message, title_id, target["episode"]["id"])

                await diagnostic(
                    "OPEN_SAFARI",
                    "running",
                    "Открываем выбранную страницу; запуск ещё не подтверждён.",
                )
                opening_target = {
                    **target,
                    "episode": {**target["episode"], "position": starting_position},
                    "restart": not prefs.resume_position,
                    "autoplay": prefs.autoplay,
                    "diagnostic": diagnostic,
                    "direct_verification": link_verification
                    if source["adapter"] == "direct"
                    else None,
                }
                result = await self.player.open(opening_target)
            except (ToolError, OSError, TimeoutError) as exc:
                if native:
                    await adapter.detach()
                self.session["mode"] = "manual"
                self.playback.failure(exc)
                result = {
                    "playback_confirmed": False,
                    "mode": "manual",
                    "message": str(exc),
                    "error_code": getattr(exc, "code", "player_unavailable"),
                }
            self.session["mode"] = result.get("mode", self.session["mode"])
            if result.get("content_verified") is False:
                await self.db(
                    "verify_link",
                    target["link"]["id"],
                    {
                        **link_verification,
                        "content_verified": False,
                        "video_type": result.get("video_type", "unknown"),
                        "classification_reason": result.get("message", "Не подтверждён тип видео."),
                    },
                )
            if (
                result.get("mode") == "native"
                or result.get("page_opened")
                or result.get("file_opened")
                or result.get("playback_confirmed")
            ) and result.get("content_verified") is not False:
                await self.db("select", target["episode"]["id"], target["link"]["id"])
            self.message = result.get("message", "Воспроизведение не подтверждено.")
            if series_page and not result.get("playback_confirmed"):
                self.message += (
                    f" На сайте выбери сезон {target['episode']['season']}, "
                    f"серию {target['episode']['episode']}. "
                    "Прогресс можно сохранить вручную в карточке."
                )
                if target["episode"]["position"]:
                    self.message += f" Сохранённый таймкод: {target['episode']['position']:.1f} с."
                result["message"] = self.message
            # Opening a page is not a viewing event. Preserve the explicitly chosen link.
            if self.session["mode"] in {"html5", "native"}:
                if "position" in result and result.get("playback_confirmed"):
                    await self.save(result, "playing")
                self.poller = asyncio.create_task(self.poll())
            await self.publish()
            return result

    async def save(self, state, event):
        if not self.session:
            return False
        session = self.session
        adapter = session["adapter"]
        if isinstance(adapter, HTML5Adapter) and adapter.expected:
            if not adapter.content_verified or state.get("binding") != adapter.binding:
                self.playback.transition(
                    PlaybackState.BLOCKED,
                    "Видео изменилось: соответствие выбранному названию не подтверждено. "
                    "Прежний прогресс сохранён; выбери источник заново.",
                    "player_changed",
                )
                return False
        target = session["target"]
        position, duration = state.get("position"), state.get("duration")
        if (
            any(type(v) not in (int, float) or not math.isfinite(v) for v in (position, duration))
            or duration <= 0
            or position < 0
            or position > duration + 2
            or state.get("error")
            or state.get("seeking")
        ):
            return False
        if position == 0 and target["episode"]["position"] > 0 and not session.get("allow_zero"):
            await self.trace(
                "SAVE_PROGRESS",
                "ignored",
                "Нулевое показание отброшено; последняя корректная позиция сохранена.",
                target["item"]["id"],
                target["episode"]["id"],
            )
            return False
        if event == "ended" and (not state.get("ended") or position < duration - 2):
            return False
        observed = state.get("observed_at", 0)
        if type(observed) not in (int, float) or not math.isfinite(observed):
            raise ValueError("Неверная отметка времени плеера.")
        if observed and observed < session["observed_at"]:
            return False
        self.playback.observe(state, event)
        if (
            event == "periodic"
            and session.get("counted")
            and abs(position - session.get("sample_position", position)) <= 0.05
        ):
            # A stationary clock is not another successful viewing timestamp.
            return False
        if not session.get("counted"):
            advancing = (
                state.get("paused") is False and position > session.get("sample_position", 0) + 0.05
            )
            if (
                not (
                    event == "playing"
                    and (state.get("playback_confirmed") or session["mode"] == "embedded")
                )
                and not advancing
            ):
                session["sample_position"] = position
                return False
            event = "playing"
            await self.db("source_health", target["source"]["id"], "available", playback=True)
            await self.db("link_check", target["link"]["id"], "available", True)
            session["counted"] = session["confirmed"] = True
        session["observed_at"] = observed
        session["sample_position"] = position
        session["allow_zero"] = False
        if not (await self.db("preferences")).save_progress:
            return True
        await self.db(
            "progress", target["episode"]["id"], position, duration, event, target["link"]["id"]
        )
        await self.db(
            "playback_state",
            target["episode"]["id"],
            target["link"]["id"],
            position,
            duration,
            True,
            session.source.voice,
        )
        target["episode"].update(position=position, duration=duration)
        await self.trace(
            "SAVE_PROGRESS",
            "success",
            f"Сохранено {position:.1f} / {duration:.1f} с.",
            target["item"]["id"],
            target["episode"]["id"],
        )
        return True

    async def embedded_control(self, action, seconds, token):
        async with self.lock:
            session = self.session
            if (
                not session
                or session["mode"] != "embedded"
                or not secrets.compare_digest(token, session["token"])
            ):
                raise ToolError("Просмотр изменился. Повтори команду.")
            if session.get("control"):
                raise ToolError("Дождись ответа плеера на предыдущую команду.")
            control = {
                "id": secrets.token_hex(8),
                "action": action,
                "future": asyncio.get_running_loop().create_future(),
            }
            if action == "seek":
                duration = session.get("last_duration") or session["target"]["episode"]["duration"]
                control["expected"] = max(
                    0, min(max(0, duration - 0.1), session.get("last_position", 0) + seconds)
                )
            session["control"] = control
        try:
            await self.publish(
                embedded_control={
                    "token": token,
                    "action": action,
                    "seconds": seconds,
                    "command_id": control["id"],
                }
            )
            return await asyncio.wait_for(control["future"], 5)
        except TimeoutError:
            if self.session is session:
                self.playback.transition(
                    PlaybackState.BLOCKED,
                    "Встроенный плеер не подтвердил действие.",
                    "embedded_timeout",
                )
            raise ToolError(
                "Встроенный плеер не подтвердил действие. Проверь его в кинотеке."
            ) from None
        finally:
            session.pop("control", None)
            await self.publish()

    async def viewer_control(self, value):
        self.require_playback()
        if not self.tools.enabled("cinema_command"):
            raise ToolError("Управление кино отключено в настройках доступа.")
        if (
            self.session
            and self.session["mode"] == "embedded"
            and value.get("action") in {"play", "pause"}
        ):
            return await self.embedded_control(value["action"], 0, str(value.get("token", "")))
        async with self.lock:
            if not self.session or not secrets.compare_digest(
                str(value.get("token", "")), self.session["token"]
            ):
                raise ToolError("Просмотр изменился. Обнови экран плеера.")
            if value["op"] == "playback_voice":
                voice = str(value.get("voice", ""))[:100]
                tracks = self.session.source.capabilities.get("audio_tracks", [])
                if (
                    not voice
                    or voice not in tracks
                    or not hasattr(self.session["adapter"], "set_voice")
                ):
                    raise ToolError("Плеер не подтвердил наличие этой звуковой дорожки.")
                result = await self.session["adapter"].set_voice(voice)
                self.session.source.voice = voice
                await self.db(
                    "playback_voice",
                    self.session["target"]["episode"]["id"],
                    self.session["target"]["link"]["id"],
                    voice,
                )
                return {**result, "message": "Плеер подтвердил звуковую дорожку."}
            action = value.get("action")
            if action == "close":
                await self.stop_tracking()
                return {
                    "playback_confirmed": False,
                    "message": "Просмотр закрыт; прогресс сохранён.",
                }
            if action not in {"play", "pause", "seek", "fullscreen"}:
                raise ValueError("Неизвестное действие плеера.")
            if self.session["mode"] not in {"html5", "native"}:
                raise ToolError("Используй элементы управления встроенного видео или сайта.")
            position = value.get("position", 0)
            if type(position) not in {int, float} or not math.isfinite(position) or position < 0:
                raise ValueError("Неверная позиция видео.")
            if action == "seek":
                current = await self.player.sample()
                if not current.get("duration") or position >= current["duration"]:
                    raise ToolError("Позиция выходит за границы видео.")
                self.session["allow_zero"] = position == 0
            try:
                result = await self.player.control(action, position)
            except (ToolError, OSError, TimeoutError) as exc:
                if action != "fullscreen":
                    self.playback.failure(exc)
                raise
            if action != "fullscreen":
                await self.save(
                    result,
                    "seek" if action == "seek" else "pause" if action == "pause" else "playing",
                )
            self.session.source.capabilities.update(result.get("capabilities") or {})
            return result

    async def sample(self, event):
        state = await self.player.sample()
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
                    if state.get("closed"):
                        await self.stop_tracking(flush=False)
                        await self.publish()
                        return
                    ended_target = self.session["target"] if state.get("ended") else None
                    await self.publish()
                if ended_target:
                    await self.finished(ended_target, (await self.db("preferences")).auto_next)
                    return
            except (ToolError, OSError, TimeoutError, sqlite3.Error, ValueError) as exc:
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
                        self.playback.failure(exc)
                        await self.publish()
                        return

    async def finished(self, target, auto_next):
        async with self.lock:
            if not self.session or self.session["target"] is not target:
                return
            player = "native" if self.session["mode"] == "native" else None
            await self.stop_tracking(flush=False)
        item = await self.db("detail", target["item"]["id"])
        if item["kind"] != "series":
            self.message = "Просмотр фильма завершён."
            await self.publish()
            return
        current = target["episode"]

        def following():
            return next(
                (
                    e
                    for e in item["episodes"]
                    if (e["season"], e["episode"]) > (current["season"], current["episode"])
                ),
                None,
            )

        next_ep = following()
        if not next_ep and item.get("catalogue_key") and self.tmdb.credentials.configured:
            try:
                entries = await self.tmdb.episodes(item["catalogue_key"], current["season"])
                item = await self.db("season", item["id"], entries)
                next_ep = following()
                seasons = item.get("metadata", {}).get("seasons", [])
                next_season = next(
                    (v["season"] for v in seasons if v["season"] > current["season"]), None
                )
                if not next_ep and next_season:
                    item = await self.db(
                        "season",
                        item["id"],
                        await self.tmdb.episodes(item["catalogue_key"], next_season),
                    )
                    next_ep = following()
            except (ToolError, OSError, TimeoutError):
                pass
        prefs = await self.db("preferences")
        self.message = "Серия завершена."
        if next_ep and prefs.offer_next:
            self.next_episode = {
                "title_id": item["id"],
                "episode_id": next_ep["id"],
                "season": next_ep["season"],
                "episode": next_ep["episode"],
                "title": item["title"],
                "player": player,
            }
        if auto_next and self.conv and next_ep:
            try:
                result = await self.resume(
                    item["id"], next_ep["id"], exact_only=True, player=player
                )
                self.message = result.get("message", self.message)
            except (ValueError, ToolError):
                self.message = (
                    "Серия завершена. Автоматический переход не подтверждён; "
                    "следующую серию можно открыть вручную."
                )
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
            if not await self.save(value, event):
                return False
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
            if flush and self.session["mode"] in {"html5", "native"}:
                with contextlib.suppress(
                    ToolError, OSError, TimeoutError, ValueError, sqlite3.Error
                ):
                    await self.sample("close")
            await self.session["adapter"].detach()
        self.playback.clear()

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
