"""One bounded player session; async HUD endpoint and guarded voice tools."""

import asyncio
import contextlib
import functools
import json
import math
import secrets
import sqlite3
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
from backend.cinema.models import CatalogueQuery, origin
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
        self.catalogue_requests = set()
        self.matches = []
        self.focused = None
        self.search_sequence = 0
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

    async def handle(self, value):
        try:
            self.require_local()
            if not isinstance(value, dict):
                raise ValueError("Неверный запрос кинотеки.")
            op = value.get("op")
            result = None
            if op == "refresh":
                pass
            elif op == "source":
                async with self.lock:
                    result = await self.db("save_source", value["source"])
                    if self.session and self.session["target"]["source"]["id"] == result["id"]:
                        if not result["enabled"] or result["adapter"] != self.session["mode"]:
                            await self.stop_tracking()
            elif op == "add":
                result = await self.db("add", value["entry"])
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
                return
            elif op == "tmdb":
                await asyncio.to_thread(self.tmdb.credentials.save, value["credential"])
                self.tmdb.clear_cache()
                self.catalogue = Catalogue(self.tmdb)
                self.matches = []
                self.message = "Настройки TMDB сохранены."
                await self.publish(saved="tmdb")
                return
            elif op == "catalogue_detail":
                detail = await self.tmdb.detail(value["key"])
                await self.publish(catalogue_detail=detail)
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
                matches = await self.tmdb.search(
                    str(value.get("query", "")).strip()[:200],
                    kind=value.get("kind", "all"),
                    genre=value.get("genre", ""),
                    year=value.get("year", 0),
                    rating=value.get("rating", 0),
                )
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
                item = await self.db("title", value["title_id"])
                prefs = await self.db("preferences")
                providers = await self.tmdb.providers(item["catalogue_key"], prefs.watch_region)
                url = providers.get("url", "")
                if not url.startswith("https://www.themoviedb.org/"):
                    raise ToolError("Страница официальных предложений недоступна.")
                await self.opener(url)
                self.message = "Открыты предложения сервисов. Запуск видео не подтверждён."
                await self.publish()
                return
            elif op == "source_search":
                source = await self.db("source", value["source_id"])
                if not source.enabled or not source.search_url:
                    raise ValueError("Поиск на этом источнике не настроен.")
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
            await self.publish(saved=result.get("id") if isinstance(result, dict) else None)
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
            result = await self.catalogue.page(
                query,
                seeds=context.get("seeds", []),
                excluded=set(context.get("excluded", []))
                if not prefs.recommendations_show_watched
                else set(),
                excluded_titles=context.get("excluded_titles", [])
                if not prefs.recommendations_show_watched
                else [],
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
            self.matches = await self.tmdb.search(genre=parsed["genre"], kind="movie")
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
            return await self.open(latest["id"], episode["id"], device=parsed.get("device"))
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
        async with self.lock:
            if action == "info":
                if self.session and self.session["mode"] == "html5":
                    await self.sample("periodic")
                refreshed = await self.db("target", target["item"]["id"], target["episode"]["id"])
                ep = refreshed["episode"]
                remaining = max(0, ep["duration"] - ep["position"])
                message = f"{target['item']['title']}. "
                if target["item"]["kind"] == "series":
                    message += f"Сезон {ep['season']}, серия {ep['episode']}. "
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
                link_id = next(
                    (
                        choice["link"]["id"]
                        for choice in available
                        if choice["link"]["id"] == ep["last_link_id"]
                    ),
                    None,
                )
            if not link_id and len(available) != 1:
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
            self.session = {
                "token": token,
                "target": target,
                "adapter": adapter,
                "sequence": 0,
                "observed_at": 0,
                "mode": target["source"]["adapter"],
                "errors": 0,
            }
            try:
                result = await adapter.open(target)
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
