"""Trusted Russian command routing and a single registered sleep-video tool."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import sqlite3
import time

from backend.config import Settings
from backend.context import request_context
from backend.media.youtube_api import VideoLibrary, playlist_id, select_video
from backend.media.youtube_playback import SafariPlayback
from backend.memory.store import MemoryStore
from backend.security import command_text
from backend.tools.registry import Tool, ToolError


def route_video(text: str, *, active=False) -> dict | None:
    # command_text normalizes case, so extract the case-sensitive playlist URL separately.
    raw = text.strip()
    if len(raw.splitlines()) > 1:
        return None
    text = command_text(raw).rstrip(".!? ")
    if any(x in text for x in ('"', "«", "»", "`", "\n")) or re.search(
        r"музык|музон|песн|трек|мьюзик|music", text
    ):
        return None
    if re.match(r"(?:добавь|запомни) (?:этот |мой )?плейлист", text) and re.search(
        r"сна|засыпани", text
    ):
        urls = re.findall(r"https://[^\s<>]+", raw)
        if len(urls) > 1:
            return {"action": "clarify"}
        return {"action": "connect", **({"url": urls[0].rstrip(".,!?")} if len(urls) == 1 else {})}
    if re.fullmatch(r"https://\S+", raw):
        # A pasted playlist URL has a clear, non-executing meaning: remember its ID.
        try:
            playlist_id(raw)
        except ToolError:
            return None
        return {"action": "connect", "url": raw}
    if re.fullmatch(
        r"(?:какие видео есть в мо[её]м плейлисте|покажи (?:мой )?плейлист(?: для сна)?)", text
    ):
        return {"action": "list"}
    if re.fullmatch(r"обнови (?:мой )?плейлист(?: для сна)?", text):
        return {"action": "refresh"}
    if re.fullmatch(r"сколько (?:там )?(?:осталось )?непросмотренных видео", text):
        return {"action": "count"}
    if re.fullmatch(r"сбрось историю (?:моего )?плейлиста", text):
        return {"action": "reset"}
    if re.fullmatch(r"(?:я это уже смотрела|отметь (?:видео|его) просмотренным)", text):
        return {"action": "mark_watched"}
    if re.fullmatch(r"(?:это видео ещ[её] не смотрела|отметь (?:видео|его) непросмотренным)", text):
        return {"action": "mark_unwatched"}
    if re.fullmatch(r"(?:больше не включай это видео|пропусти (?:это )?видео)", text):
        return {"action": "skip"}
    if re.fullmatch(r"(?:поставь(?: видео)? на паузу|пауза)", text) and (active or "видео" in text):
        return {"action": "pause"}
    if re.fullmatch(r"продолжи видео", text):
        return {"action": "resume"}
    if (
        re.fullmatch(
            r"(?:что сейчас играет|сколько длится этот видос|сколько ещ[её] осталось)", text
        )
        and active
    ):
        return {"action": "current"}
    if re.fullmatch(r"следующее видео", text):
        return {"action": "next", "mode": "order"}
    if active and re.fullmatch(r"не включай то,? что было вчера", text):
        return {"action": "avoid_recent", "exclude_recent_hours": 48}
    if active and re.fullmatch(r"найди (?:видео )?подлиннее", text):
        return {"action": "play", "mode": "longest"}
    if re.match(r"(?:включи|поставь|найди|не включай)\b", text):
        video_intent = bool(
            re.search(
                (
                    "видео|ролик|видос|из моего плейлиста|то,? что я ещ[её] не "
                    "смотрела|случайное непросмотренное|что-нибудь на"
                ),
                text,
            )
        )
        if not video_intent:
            return None
        if text.startswith("не включай") and not (active and "вчера" in text):
            return None
        args = {"action": "next" if re.search(r"следующ", text) else "play"}
        if re.search(r"следующ|по порядку", text):
            args["mode"] = "order"
        if "случайн" in text:
            args["mode"] = "random"
        if re.search(r"самое длинное|подлиннее", text):
            args["mode"] = "longest"
        if "самое короткое" in text:
            args["mode"] = "shortest"
        if "повторно" in text:
            args["allow_repeat"] = True
        if "непросмотренн" in text or "не смотрела" in text:
            args["unwatched"] = True
        if "вчера" in text:
            args["exclude_recent_hours"] = 48  # Conservative rolling bound, explicit in reply.
        words = {"один": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4}
        number = r"(\d+|один|одну|два|две|три|четыре)"

        def seconds(m):
            return (int(m[1]) if m[1].isdigit() else words[m[1]]) * (
                3600 if m[2].startswith("час") else 60
            )

        lower = re.search(
            r"(?:не (?:короче|меньше)|от|минимум) " + number + r" (час\w*|минут\w*)", text
        )
        upper = re.search(
            r"(?:не (?:дольше|больше)|до|максимум) " + number + r" (час\w*|минут\w*)", text
        )
        around = re.search(r"на " + number + r" (час\w*|минут\w*)", text)
        if lower:
            args["min_seconds"] = seconds(lower)
        if upper:
            args["max_seconds"] = seconds(upper)
        if around and not lower and not upper:
            n = seconds(around)
            args.update(min_seconds=int(n * 0.9), max_seconds=int(n * 1.1))
        channel = re.search(r"(?:с канала|канала) (.+?)(?: для сна)?$", text)
        if channel:
            args["channel"] = channel[1]
        return args
    return None


def duration_text(seconds) -> str:
    if seconds is None:
        return "длительность неизвестна"
    n = max(0, int(seconds))
    return f"{n // 3600} ч {n % 3600 // 60} мин" if n >= 3600 else f"{n // 60} мин {n % 60} с"


class SleepVideo:
    def __init__(self, s: Settings, store: MemoryStore, *, api=None, playback=None):
        self.s, self.store = s, store
        self.library = VideoLibrary(store, s.youtube_video, api)
        self.playback = playback or SafariPlayback(s)
        self.lock = asyncio.Lock()
        self.media = None

    def current(self) -> dict | None:
        return self.store.preference("youtube_video", "current")

    def route(self, text: str) -> dict | None:
        # A successful music command takes precedence over our old current-video record.
        try:
            active = bool(self.current()) and (
                self.media is None or self.media._last_control_target != "music"
            )
        except sqlite3.Error:
            active = False
        return route_video(text, active=active)

    def pid(self) -> str:
        saved = self.store.preference("youtube_video", "playlist")
        value = saved["id"] if saved else self.s.youtube_video.playlist_url
        if not value:
            raise ToolError(
                "Добавь публичный плейлист: вставь ссылку "
                "https://www.youtube.com/playlist?list=ID в чат или скажи «запомни мой "
                "плейлист для сна» в Safari."
            )
        return playlist_id(value)

    def guard(self, args: dict) -> dict:
        context = request_context.get()
        if context is None or not context.device.authenticated or self.route(context.text) != args:
            raise ToolError(
                "Нужна соответствующая текущая просьба пользовательницы; параметры LLM не приняты."
            )
        return args

    def version(self, args: dict) -> str:
        if args.get("action") != "reset":
            return ""
        return hashlib.sha256(
            json.dumps([self.pid(), self.library.history(self.pid())], sort_keys=True).encode()
        ).hexdigest()

    async def invoke(self, action: str, **filters) -> dict:
        if action == "clarify":
            return {"ok": False, "message": "Пришли одну ссылку на плейлист для сна."}
        if action == "connect":
            pid = (
                playlist_id(filters["url"])
                if filters.get("url")
                else playlist_id(await self.playback.current_playlist())
            )
            self.store._require_memory()
            self.store.set_preference("youtube_video", "playlist", {"id": pid})
            return {
                "ok": True,
                "message": (
                    "Плейлист для сна сохранён отдельно от музыки. Скажи «какие видео есть "
                    "в моём плейлисте?» для проверки API."
                ),
            }
        pid = self.pid()
        if action == "avoid_recent":
            self.store._require_memory()
            self.store.set_preference("youtube_video", "selection", filters)
            return {
                "ok": True,
                "message": "Для следующих выборов исключаю запуски за последние 48 часов. "
                "Новое видео не запускал.",
            }
        if action == "reset":
            self.library.reset(pid)
            return {
                "ok": True,
                "message": (
                    "Сбросил только историю Чушпана для этого плейлиста. Историю YouTube не трогал."
                ),
            }
        if action in {"list", "refresh", "count"}:
            data = await self.library.load(pid, force=action == "refresh")
            history = self.library.history(pid)
            remaining = sum(
                v["available"]
                and history[v["id"]]["status"] == "unwatched"
                and not history[v["id"]]["excluded"]
                for v in data["videos"]
            )
            text = f"{data['title']}: {len(data['videos'])} видео, непросмотренных {remaining}."
            if action != "count":
                text += " " + "; ".join(
                    v["title"]
                    + " — "
                    + duration_text(v["duration"])
                    + (" (недоступно)" if not v["available"] else "")
                    for v in data["videos"][:10]
                )
                if len(data["videos"]) > 10:
                    text += " Показал первые 10; весь список сохранён локально."
            if data.get("cache") == "stale":
                text += " Последний сохранённый список: " + data["warning"]
            return {"ok": True, "message": text, "remaining": remaining, "cache": data["cache"]}
        if action in {"play", "next"}:
            return await self.play(action=action, **filters)
        current = self.current()
        if not current or current["playlist_id"] != pid:
            raise ToolError(
                "Нет выбранного Чушпаном видео этого плейлиста. Сначала включи видео для сна."
            )
        if action in {"pause", "resume", "current"}:
            state = await (
                self.playback.state(current["target"])
                if action == "current"
                else self.playback.control(current["target"], action)
            )
            self.progress(current, state)
            if action == "current":
                p, d = state.get("progress"), state.get("duration")
                remaining = (
                    duration_text(max(0, d - p))
                    if isinstance(p, (int, float)) and isinstance(d, (int, float))
                    else "неизвестно"
                )
                return {
                    "ok": True,
                    "message": (
                        f"{current['title']}, {duration_text(current['duration'])}; "
                        f"осталось {remaining}; "
                        + {
                            "playing": "играет",
                            "paused": "на паузе",
                            "ended": "окончено",
                            "loading": "загружается",
                        }.get(state.get("state"), "состояние неизвестно")
                        + "."
                    ),
                }
            if self.media:
                self.media._last_control_target = "video"
            return {
                "ok": True,
                "message": "Видео на паузе." if action == "pause" else "Видео продолжено.",
            }
        statuses = {"mark_watched": "watched", "mark_unwatched": "unwatched", "skip": "skipped"}
        if action in statuses:
            # 'This video' must still be the exact tab we launched, never a guessed old title.
            state = await self.playback.state(current["target"])
            if state.get("id") != current["video_id"]:
                raise ToolError("Вкладка сменилась. Статус не изменял.")
            self.library.mark(pid, current["video_id"], statuses[action])
            return {
                "ok": True,
                "message": {
                    "mark_watched": "Отметил просмотренным в истории Чушпана.",
                    "mark_unwatched": "Отметил непросмотренным.",
                    "skip": "Исключил это видео из дальнейшего выбора.",
                }[action],
            }
        raise ToolError("Неизвестная команда видео.")

    def progress(self, current: dict, state: dict):
        if state.get("id") != current["video_id"] or state.get("ad"):
            return
        row = self.library.history(current["playlist_id"]).get(current["video_id"])
        if not row or row["status"] in {"watched", "skipped"} or not row["starts"]:
            return
        p, d = state.get("progress"), state.get("duration")
        # Video duration must agree with API metadata; ads/live/mismatched streams aren't evidence.
        if (
            not isinstance(p, (int, float))
            or not isinstance(d, (int, float))
            or not current["duration"]
            or abs(d - current["duration"]) > 5
        ):
            return
        status = (
            "watched"
            if state.get("state") == "ended" and p >= d - 2
            else "partially_watched"
            if p > 0
            else "started"
        )
        self.library.mark(
            current["playlist_id"],
            current["video_id"],
            status,
            progress=p,
            source="safari_observation",
        )

    async def verify_for_sleep(self):
        current = self.current()
        if current is None:
            raise ToolError("Нет выбранного видео для Youtube sleep.")
        state = await self.playback.state(current["target"])
        if (
            state.get("id") != current["video_id"]
            or state.get("state") != "playing"
            or state.get("ad")
            or not state.get("active")
            or not state.get("frontmost")
            or not state.get("autoplay_off")
        ):
            raise ToolError("Сделай выбранное видео активной вкладкой Safari перед Youtube sleep.")

    async def play(self, *, action="play", source="voice_or_text", **filters) -> dict:
        async with self.lock:
            self.store._require_memory()
            token = secrets.token_hex(16)
            if not self.store.claim_operation("youtube_video", token, time.time()):
                raise ToolError("Уже выполняется запуск видео. Дождись результата.")
            try:
                return await self._play(action, source, filters)
            finally:
                self.store.release_operation("youtube_video", token)

    async def _play(self, action, source, filters):
        pid = self.pid()
        context = request_context.get()
        request_id = context.request_id if context else ""
        journal = self.store.preference("youtube_video", "launch") or {}
        if request_id and journal.get("request_id") == request_id:
            raise ToolError("Этот запуск уже обработан; повторно видео не открываю.")
        # A running watcher follows Safari's front tab. Never silently replace its video.
        night = self.store.preference("night", "last_run") or {}
        lease = self.store.preference("night", "operation_lease") or {}
        if source != "night" and lease.get("until", 0) > time.time():
            raise ToolError(
                "Ночной сценарий ещё выполняется. Дождись результата перед сменой видео."
            )
        if source != "night" and night.get("steps", {}).get("shortcut", {}).get("status") in {
            "done",
            "started",
            "unknown",
        }:
            raise ToolError(
                "Youtube sleep мог остаться запущен. Останови его в Командах и сбрось "
                "журнал через night reset --confirm-stopped перед сменой видео."
            )
        data = await self.library.load(pid)
        cfg = self.s.youtube_video
        options = {
            "mode": cfg.mode,
            "min_seconds": cfg.min_seconds,
            "max_seconds": cfg.max_seconds,
            "unwatched": True,
            "exclude_recent_hours": cfg.exclude_recent_hours,
            "allow_repeat": cfg.allow_repeat,
        }
        options.update(self.store.preference("youtube_video", "selection") or {})
        options.update(filters)
        if filters.get("unwatched"):
            options["allow_repeat"] = False
        if options["min_seconds"] > options["max_seconds"]:
            raise ToolError("Минимальная длительность больше максимальной; фильтры не менял.")
        previous = self.current()
        if (
            action == "next"
            and previous
            and previous["playlist_id"] == pid
            and options["mode"] == "order"
        ):
            options["after_position"] = previous["position"]
        selected = select_video(data, self.library.history(pid), **options)
        # Persist before the side effect; a repeated request cannot open another tab after a crash.
        self.store.set_preference(
            "youtube_video",
            "launch",
            {"request_id": request_id, "state": "pending", "video_id": selected["id"]},
        )
        result = await self.playback.launch(selected)
        current = {
            "playlist_id": pid,
            "video_id": selected["id"],
            "title": selected["title"],
            "duration": selected["duration"],
            "position": selected["position"],
            "target": result["target"],
        }
        try:
            with self.store.memory_transaction():
                self.store.set_preference("youtube_video", "current", current)
                if result["playing"]:
                    self.library.mark(pid, selected["id"], "started", source=source, launched=True)
                self.store.set_preference(
                    "youtube_video",
                    "launch",
                    {
                        "request_id": request_id,
                        "state": "playing" if result["playing"] else "unverified",
                        "video_id": selected["id"],
                    },
                )
        except sqlite3.Error:
            raise ToolError(
                "Видео могло открыться, но история не сохранена из-за SQLite. "
                "Проверь Safari; автоматически запуск не повторяю."
            ) from None
        if self.media:
            self.media._last_control_target = "video"
        message = (
            ("Играет: " if result["playing"] else "Открыл, но воспроизведение не подтверждено: ")
            + selected["title"]
            + ", "
            + duration_text(selected["duration"])
            + "."
        )
        if not result["playing"]:
            message += (
                " Проверь Safari: разреши JavaScript из Apple Events, выключи "
                "автопроигрывание и нажми Play, если браузер запрещает автозапуск. "
                "Историю запусков не увеличивал."
            )
        if options.get("exclude_recent_hours") == 48:
            message += " Исключены запуски за последние 48 часов."
        if data["cache"] == "stale":
            message += " Использован сохранённый список: " + data["warning"]
        message += " " + " ".join(result.get("warnings", []))
        return {
            "ok": result["playing"],
            "message": message.strip(),
            "playing": result["playing"],
            "video_id": selected["id"],
        }

    def tool(self) -> Tool:
        actions = [
            "clarify",
            "avoid_recent",
            "connect",
            "list",
            "refresh",
            "count",
            "reset",
            "play",
            "next",
            "pause",
            "resume",
            "current",
            "mark_watched",
            "mark_unwatched",
            "skip",
        ]
        tool = Tool(
            "youtube_sleep",
            (
                "Публичный YouTube-плейлист для сна, отдельный от YouTube Music. Только"
                " текущая явная команда, фильтры не выдумывать; массовый сброс по коду."
            ),
            self.invoke,
            {
                "action": {"type": "string", "enum": actions},
                "url": {"type": "string", "maxLength": 1000},
                "mode": {"type": "string", "enum": ["random", "order", "longest", "shortest"]},
                "min_seconds": {"type": "integer", "minimum": 0, "maximum": 604800},
                "max_seconds": {"type": "integer", "minimum": 1, "maximum": 604800},
                "channel": {"type": "string", "maxLength": 200},
                "unwatched": {"type": "boolean"},
                "allow_repeat": {"type": "boolean"},
                "exclude_recent_hours": {"type": "integer", "minimum": 0, "maximum": 8760},
            },
            ["action"],
            prepare=self.guard,
            intent=lambda a, t: self.route(t) == a,
            summary=lambda a: (
                "сбросить локальную историю Чушпана для " + self.pid()
                if a["action"] == "reset"
                else "YouTube для сна: " + a["action"]
            ),
            version=self.version,
            reply=lambda r: r["message"],
            slow=True,
            capability={
                "ru": (
                    "выбирать видео из отдельного YouTube-плейлиста для сна, помнить "
                    "просмотры и управлять видео в Safari"
                )
            },
        )
        tool.required_capability = "mac.media"
        return tool
