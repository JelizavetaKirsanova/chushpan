"""Control the installed Safari Web App through public macOS Accessibility APIs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import plistlib
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from backend.config import ROOT_DIR, Settings
from backend.permissions import instruction
from backend.tools.registry import ToolError

log = logging.getLogger(__name__)

ORIGIN = "https://music.youtube.com"
PAUSE_NAMES = ("пауза", "приостановить", "pause")
PLAY_NAMES = ("воспроизвести", "play")
PERMISSION = instruction("accessibility")


def validate_url(url: str) -> str:
    u = urlparse(url)
    if (
        u.scheme != "https"
        or u.netloc != "music.youtube.com"
        or u.username
        or u.password
        or u.fragment
        or u.path.split("/")[1]
        not in ("", "library", "playlist", "watch", "browse", "channel", "search")
    ):
        raise ToolError("Разрешены только музыкальные страницы music.youtube.com.")
    return url


def same_page(actual: str, expected: str) -> bool:
    a, e = urlparse(actual), urlparse(expected)
    if a.netloc != e.netloc or a.path.rstrip("/") != e.path.rstrip("/"):
        return False
    aq, eq = parse_qs(a.query), parse_qs(e.query)
    return all(aq.get(k) == v for k, v in eq.items())


def descendants(nodes: list[dict], parent: dict) -> list[dict]:
    path = parent["path"]
    return [n for n in nodes if n["path"][: len(path)] == path]


def label(node: dict) -> str:
    return node.get("title") or node.get("description") or node.get("value", "")


def player_nodes(nodes: list[dict]) -> list[dict]:
    for n in nodes:
        if n.get("role") == "AXToolbar" and label(n).casefold() in (
            "панель проигрывателя",
            "player bar",
            "player controls",
        ):
            return descendants(nodes, n)
    return []


def player_state(nodes: list[dict]) -> dict:
    items = player_nodes(nodes)
    names = {label(n).casefold() for n in items if n.get("role") == "AXButton"}
    title = next((label(n) for n in items if n.get("role") == "AXHeading"), "")
    state = "unknown"
    if names.intersection(PAUSE_NAMES):
        state = "playing"
    elif names.intersection(PLAY_NAMES):
        state = "paused"
    if not title:
        return {"state": "unknown", "provider": "youtube_music"}
    artists, album = [], ""
    for n in items:
        if n.get("role") != "AXLink":
            continue
        if "/channel/" in n.get("url", ""):
            artists.append(label(n))
        elif "/browse/" in n.get("url", ""):
            album = label(n)
    out = {
        "state": state,
        "track": title,
        "artist": ", ".join(artists),
        "album": album,
        "provider": "youtube_music",
    }
    for n in items:
        if n.get("role") == "AXSlider" and label(n).casefold() in ("громкость", "volume"):
            out["volume"] = round(float(n["value"]))
        if n.get("role") == "AXStaticText" and " / " in n.get("value", ""):
            out["position"] = n["value"]
    return out


async def process(*args: str, data: dict | None = None, timeout: float = 15) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        log.info("NATIVE_PROCESS pid=%d ppid=%d executable=%s", proc.pid, os.getpid(), args[0])
    except FileNotFoundError as exc:
        raise ToolError(
            "Для адаптера YouTube Music нужны macOS и Command Line Tools (xcrun)."
        ) from exc
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(json.dumps(data).encode() if data is not None else None), timeout
        )
    except (TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        # Never relay raw browser/library errors: they can contain request headers.
        raise ToolError(f"Не удалось выполнить локальную команду {Path(args[0]).name}.")
    return out.decode()


class AccessibilityApp:
    """Shared native AX bridge for the music web app and ordinary Safari windows."""

    def __init__(self, s: Settings, app_name: str, timeout_s: float = 12) -> None:
        self.s = s
        self.app_name, self.timeout_s = app_name, timeout_s
        self._binary: Path | None = None
        self._build_lock = asyncio.Lock()

    @property
    def bundle_id(self) -> str:
        raise NotImplementedError

    @property
    def source(self) -> Path:
        return ROOT_DIR / "backend/media/ytmusic_ax.swift"

    async def _helper(self) -> Path:
        from backend.paths import native_helper

        if bundled := native_helper(self.source.stem):
            return bundled
        async with self._build_lock:
            if self._binary is not None:
                return self._binary
            source = self.source
            from backend.native_identity import helper_info

            info = helper_info(source.stem)
            digest = hashlib.sha256(source.read_bytes() + info).hexdigest()[:16]
            cache = self.s.data_path / "native"
            cache.mkdir(exist_ok=True)
            target = cache / f"ytmusic-ax-{digest}"
            if not target.exists():
                plist = cache / (source.stem + ".plist")
                plist.write_bytes(info)
                temporary = target.with_suffix(".building")
                try:
                    await process(
                        "xcrun",
                        "swiftc",
                        "-module-cache-path",
                        str(cache / "swift-cache"),
                        str(source),
                        "-Xlinker",
                        "-sectcreate",
                        "-Xlinker",
                        "__TEXT",
                        "-Xlinker",
                        "__info_plist",
                        "-Xlinker",
                        str(plist),
                        "-o",
                        str(temporary),
                        timeout=90,
                    )
                    temporary.replace(target)
                    await process(
                        "codesign",
                        "--force",
                        "--sign",
                        "-",
                        "--identifier",
                        "local.chushpan." + source.stem.replace("_", "-"),
                        str(target),
                    )
                except TimeoutError as exc:
                    raise ToolError(
                        "Сборка адаптера YouTube Music заняла слишком много времени."
                    ) from exc
            self._binary = target
            return target

    async def request(self, op: str, **kwargs) -> dict:
        bundle = self.bundle_id
        binary = await self._helper()
        try:
            result = json.loads(
                await process(
                    str(binary),
                    data={"bundle": bundle, "op": op, **kwargs},
                    timeout=max(self.timeout_s, 60)
                    if op == "permission_request"
                    else self.timeout_s,
                )
            )
        except TimeoutError as exc:
            raise ToolError(f"{self.app_name} не ответил вовремя.") from exc
        error = result.get("error")
        if error == "accessibility_permission":
            from backend.permission_policy import requests_allowed

            if requests_allowed.get() and not kwargs.get("_permission_retry"):
                await self.request("permission_request")
                from backend.desktop.platform import open_privacy

                await asyncio.to_thread(open_privacy, "accessibility")
                # The blocked operation has not started. Bound the wait and retry once.
                for _ in range(60):
                    status_op = (
                        "youtube_permissions"
                        if self.source.stem == "youtube_safari"
                        else "permission_status"
                    )
                    status = await self.request(status_op)
                    if status.get("accessibility") == "granted":
                        return await self.request(op, **kwargs, _permission_retry=True)
                    await asyncio.sleep(1)
            raise ToolError(PERMISSION)
        messages = {
            "youtube_multiple_tabs": (
                "В Safari несколько видео YouTube. Оставь одну нужную вкладку; "
                "посторонние вкладки я не закрываю."
            ),
            "youtube_no_playlist": "В активной вкладке Safari нет ссылки на плейлист YouTube.",
            "youtube_tab_changed": "Вкладка YouTube изменилась; запуск остановлен.",
            "youtube_state_unavailable": "Safari не вернул достоверное состояние плеера YouTube.",
            "youtube_safari_permission_or_state": (
                "Safari не подтвердил действие. Проверь вкладку, Automation → Safari "
                "и Develop → Allow JavaScript from Apple Events."
            ),
            "youtube_script": "Не удалось подготовить фиксированную команду Safari.",
            "unsupported_brightness": (
                "У этого дисплея нет однозначного управления яркостью через публичный IOKit API. "
                "Яркость не изменена."
            ),
            "brightness_not_verified": "Изменение яркости не подтверждено дисплеем.",
            "app_changed": "Приложение уже изменилось или завершилось. Получи список заново.",
            "activation_not_verified": "macOS не подтвердила переключение приложения.",
            "user_session_locked": (
                "macOS показывает экран входа. Разблокируй Mac и повтори запуск YouTube Music."
            ),
            "quit_not_verified": (
                "Приложение не завершилось. Возможно, оно ждёт сохранения документа; "
                "принудительно не закрываю."
            ),
            "window_changed": "Окно или фокус изменились. Получи список окон заново.",
            "window_ambiguous": "Несколько одинаковых окон. Нельзя надёжно выбрать цель.",
            "unsupported_window_attribute": "Эта операция с окном недоступна через AX.",
            "window_not_verified": (
                "Изменение окна не подтверждено. Возможно, операция не поддерживается; "
                "проверь состояние окна."
            ),
            "invalid_geometry": "Размер или положение окна выходит за видимую область экрана.",
            "window_geometry_unavailable": (
                "Геометрия окна недоступна. Сначала выйди из полноэкранного режима."
            ),
            "app_closed": f"{self.app_name} закрыт. Открой приложение и повтори команду.",
            "element_changed": f"Окно {self.app_name} обновилось. Повтори команду.",
            "search_focus_unavailable": (
                "Не удалось выбрать поле поиска. Открой окно YouTube Music и повтори."
            ),
            "search_field_unavailable": "Поисковое поле YouTube Music сейчас недоступно.",
        }
        if error in messages:
            raise ToolError(messages[error])
        if error:
            raise ToolError(f"{self.app_name}: {error}. Действие не подтверждено.")
        return result


class SafariMusic(AccessibilityApp):
    def __init__(self, s: Settings) -> None:
        self.config = s.media.youtube_music
        super().__init__(s, "YouTube Music", self.config.timeout_s)
        self._app: tuple[Path, str] | None = None

    @property
    def bundle_id(self) -> str:
        return self.discover()[1]

    @staticmethod
    def application(path: Path | str, *, selected=False) -> dict:
        if not isinstance(path, (Path, str)):
            raise ToolError("Выбери установленное приложение .app.")
        app = Path(path).expanduser().resolve()
        if app.suffix.lower() != ".app" or not app.is_dir():
            raise ToolError("Выбери установленное приложение .app.")
        try:
            info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        except (OSError, ValueError):
            raise ToolError("Не удалось прочитать идентификатор приложения.") from None
        bundle = info.get("CFBundleIdentifier", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,200}", bundle):
            raise ToolError("У приложения нет корректного bundle identifier.")
        start = urlparse((info.get("Manifest") or {}).get("start_url", ""))
        chrome = urlparse(info.get("CrAppModeShortcutURL", ""))
        webapp = (
            bundle.startswith("com.apple.Safari.WebApp.") and start.hostname == "music.youtube.com"
        )
        pwa = chrome.hostname == "music.youtube.com"
        if not (webapp or pwa or selected):
            raise ToolError("Приложение не подтверждено как YouTube Music.")
        return {
            "path": str(app),
            "bundle_id": bundle,
            "name": (info.get("Manifest") or {}).get("name")
            or info.get("CFBundleDisplayName")
            or app.stem,
            "kind": "safari_webapp" if webapp else "browser_pwa" if pwa else "native",
        }

    def discover(self) -> tuple[Path, str]:
        if self._app and self._app[0].exists():
            return self._app
        configured = self.config.app_path
        paths = [configured.expanduser()] if configured else []
        paths += sorted((Path.home() / "Applications").glob("*.app"))
        paths += sorted(Path("/Applications").glob("*.app"))
        for app in dict.fromkeys(paths):
            try:
                details = self.application(
                    app,
                    selected=bool(
                        configured and self.config.bundle_id and app == configured.expanduser()
                    ),
                )
            except ToolError:
                continue
            bundle = details["bundle_id"]
            if self.config.bundle_id not in {None, "com.apple.Safari", bundle}:
                continue
            self._app = Path(details["path"]), bundle
            return self._app
        raise ToolError(
            "Не найден Safari Web App YouTube Music. Открой его или укажи путь в настройках."
        )

    async def launch(self) -> dict:
        app, _ = self.discover()
        result = await self.request("launch", path=str(app))
        if result.get("activated") is not True:
            raise ToolError("macOS не подтвердила активацию установленного YouTube Music.")
        return {"ok": True, "activated": True, "action": "open", "provider": "youtube_music"}

    async def request_access(self):
        return await self.request("permission_request", permission="accessibility")

    async def diagnose(self):
        app, bundle = self.discover()
        state = await self.request("status")
        return {
            "mode": "webapp",
            "bundle_id": bundle,
            "path": str(app),
            **state,
            "message": "Установленное приложение найдено. "
            + ("Accessibility разрешён. " if state.get("trusted") else "Требуется Accessibility. ")
            + ("Приложение активно." if state.get("frontmost") else "Приложение не активно."),
            "playback_confirmed": False,
        }

    async def snapshot(self) -> list[dict]:
        result = await self.request("snapshot")
        nodes = result.get("nodes", [])
        if not any(n.get("role") == "AXWebArea" for n in nodes):
            # Safari can hide its window's AX tree while dormant. Reopen rather than
            # send global media keys to whichever unrelated app happens to be active.
            await self.launch()
            deadline = time.monotonic() + self.config.timeout_s
            while time.monotonic() < deadline:
                await asyncio.sleep(0.2)
                result = await self.request("snapshot")
                nodes = result.get("nodes", [])
                if any(n.get("role") == "AXWebArea" for n in nodes):
                    break
        if not any(n.get("role") == "AXWebArea" for n in nodes):
            raise ToolError("Окно YouTube Music пока недоступно. Открой его и повтори команду.")
        if result.get("truncated"):
            raise ToolError("Страница YouTube Music слишком велика для надёжного управления.")
        return nodes

    async def act(self, node: dict, value: int | float | None = None) -> None:
        if node.get("enabled") == "0":
            raise ToolError("Эта кнопка YouTube Music сейчас недоступна.")
        expected = {k: node[k] for k in ("role", "title", "description", "url") if k in node}
        await self.request(
            "press" if value is None else "set",
            path=node["path"],
            expected=expected,
            **({"value": value} if value is not None else {}),
        )

    async def navigate(self, url: str) -> list[dict]:
        validate_url(url)
        self.discover()
        status = await self.request("status")
        if not status["running"]:
            await self.launch()
        nodes = await self.snapshot()
        links = [n for n in nodes if n.get("role") == "AXLink" and same_page(n.get("url", ""), url)]
        links.sort(
            key=lambda n: not label(n) or label(n).casefold() in ("меню действий", "action menu")
        )
        # A native click uses the site's SPA navigation and keeps its audio session.
        if links:
            await self.act(links[0])
        else:
            # Safari Web Apps may delegate external URL opens to ordinary Safari.
            # Keep navigation inside the selected app, including when a deep link
            # is missing from the currently available library/search results.
            if any(
                n.get("role") == "AXWebArea" and same_page(n.get("url", ""), url) for n in nodes
            ):
                return nodes
            raise ToolError(
                "В выбранном приложении нет ссылки на эту страницу. Открой её в библиотеке "
                "или поиске YouTube Music и повтори команду. Safari не открывался."
            )
        deadline = time.monotonic() + self.config.timeout_s
        while time.monotonic() < deadline:
            nodes = await self.snapshot()
            if any(
                n.get("role") == "AXWebArea" and same_page(n.get("url", ""), url) for n in nodes
            ):
                return nodes
            await asyncio.sleep(0.3)
        raise ToolError(
            "Safari Web App не перешёл на нужную страницу. Воспроизведение не подтверждено."
        )

    async def search(self, query: str) -> list[dict]:
        if not query.strip() or len(query) > 400:
            raise ToolError("Назови короткий поисковый запрос.")
        await self.launch()
        nodes = await self.snapshot()
        fields = [n for n in nodes if n.get("role") == "AXComboBox"]
        if len(fields) > 1:
            fields = [
                n
                for n in fields
                if any(
                    "поиск" in label(child).casefold() or "search" in label(child).casefold()
                    for child in descendants(nodes, n)
                )
            ]
        if len(fields) != 1:
            raise ToolError("Поисковое поле YouTube Music недоступно.")
        field = fields[0]
        expected = {k: field[k] for k in ("role", "title", "description") if k in field}
        await self.request("search", path=field["path"], expected=expected, query=query)
        deadline = time.monotonic() + self.config.timeout_s
        while time.monotonic() < deadline:
            nodes = await self.snapshot()
            for node in nodes:
                if node.get("role") == "AXWebArea":
                    url = urlparse(node.get("url", ""))
                    if url.path == "/search" and parse_qs(url.query).get("q") == [query]:
                        return nodes
            await asyncio.sleep(0.2)
        raise ToolError("YouTube Music не выполнил поиск. Воспроизведение не менялось.")

    async def library_page(self) -> list[dict]:
        await self.launch()
        nodes = await self.snapshot()
        links = [
            n
            for n in nodes
            if n.get("role") == "AXLink" and label(n).casefold() in ("библиотека", "library")
        ]
        if len(links) != 1:
            raise ToolError("Ссылка на библиотеку YouTube Music недоступна.")
        await self.act(links[0])
        deadline = time.monotonic() + self.config.timeout_s
        while time.monotonic() < deadline:
            nodes = await self.snapshot()
            if any(
                n.get("role") == "AXWebArea"
                and urlparse(n.get("url", "")).path.startswith("/library")
                for n in nodes
            ):
                return nodes
            await asyncio.sleep(0.2)
        raise ToolError("YouTube Music не открыл библиотеку.")

    async def now_playing(self) -> dict:
        status = await self.request("status")
        if not status["running"]:
            return {"state": "closed", "provider": "youtube_music"}
        for attempt in range(5):
            state = player_state(await self.snapshot())
            if state["state"] != "unknown" or attempt == 4:
                return state
            await asyncio.sleep(0.2)
        return state

    @staticmethod
    def button(nodes: list[dict], names: tuple[str, ...]) -> dict:
        matching = [
            n
            for n in nodes
            if n.get("role") in ("AXButton", "AXCheckBox") and label(n).casefold() in names
        ]
        if len(matching) != 1:
            raise ToolError("Нужная кнопка YouTube Music не найдена однозначно.")
        return matching[0]

    async def wait_playing(
        self, expected_track: str = "", expected_artist: str = "", expected_album: str = ""
    ) -> dict:
        deadline = time.monotonic() + self.config.timeout_s
        previous = None
        while time.monotonic() < deadline:
            now = await self.now_playing()
            if (
                now["state"] == "playing"
                and (not expected_track or now.get("track") == expected_track)
                and (
                    not expected_artist
                    or expected_artist.casefold() in now.get("artist", "").casefold()
                )
                and (
                    not expected_album
                    or expected_album.casefold() == now.get("album", "").casefold()
                )
            ):
                # A pause button alone isn't enough: require progress advancing too.
                if (
                    previous
                    and previous.get("track") == now.get("track")
                    and now.get("position", 0) > previous.get("position", 0)
                ):
                    return {
                        **now,
                        "ok": True,
                        "playback_confirmed": True,
                        "confirmation": {
                            "source": "safari_ax_player",
                            "position_before": previous["position"],
                            "position_after": now["position"],
                        },
                    }
                previous = now
            await asyncio.sleep(0.4)
        raise ToolError("YouTube Music открыт, но реальное воспроизведение не подтверждено.")

    async def play_page(self, url: str, title: str = "", video: bool = False) -> dict:
        nodes = await self.navigate(url)
        if video:
            # watch links can autoplay; otherwise press the player play button.
            if player_state(nodes)["state"] != "playing":
                await self.act(self.button(player_nodes(nodes), PLAY_NAMES))
        else:
            deadline = time.monotonic() + self.config.timeout_s
            while time.monotonic() < deadline:
                page_title = title or next(
                    (n.get("description", "") for n in nodes if n.get("role") == "AXWebArea"), ""
                )
                names = {"воспроизвести", "play", "воспроизвести всё", "play all"}
                if "/channel/" in url:
                    names |= {"микс", "mix", "слушать радио", "play radio"}
                if page_title:
                    names |= {
                        f"{page_title} – воспроизвести".casefold(),
                        f"play {page_title}".casefold(),
                    }
                already_playing = {f"{page_title} – {name}".casefold() for name in PAUSE_NAMES}
                names |= already_playing
                bar = player_nodes(nodes)
                buttons = [
                    n
                    for n in nodes
                    if n not in bar
                    and n.get("role") == "AXButton"
                    and re.sub(r"\s+", " ", label(n)).strip().casefold() in names
                ]
                if len(buttons) == 1:
                    if (
                        re.sub(r"\s+", " ", label(buttons[0])).strip().casefold()
                        not in already_playing
                    ):
                        await self.act(buttons[0])
                    break
                await asyncio.sleep(0.3)
                nodes = await self.snapshot()
            else:
                raise ToolError("Страница открыта, но кнопка запуска не найдена однозначно.")
            expected_list = parse_qs(urlparse(url).query).get("list", [])
            if expected_list:
                await asyncio.sleep(0.3)
                items = player_nodes(await self.snapshot())
                await self.act(
                    self.button(items, ("открыть страницу проигрывателя", "open player page"))
                )
                for _ in range(15):
                    nodes = await self.snapshot()
                    current = next(
                        (n.get("url", "") for n in nodes if n.get("role") == "AXWebArea"), ""
                    )
                    if parse_qs(urlparse(current).query).get("list") == expected_list:
                        break
                    await asyncio.sleep(0.2)
                else:
                    raise ToolError("Плеер запущен, но очередь нужного плейлиста не подтверждена.")
        return await self.wait_playing(
            title if video else "",
            title if "/channel/" in url else "",
            title if "/browse/" in url else "",
        )

    async def control(self, action: str) -> dict:
        if action == "open":
            await self.launch()
            await self.snapshot()
            return {"ok": True, "action": "open"}
        nodes = await self.snapshot()
        items = player_nodes(nodes)
        before = player_state(nodes)
        for _ in range(5):
            if before["state"] != "unknown":
                break
            await asyncio.sleep(0.2)
            nodes = await self.snapshot()
            items, before = player_nodes(nodes), player_state(nodes)
        if action in ("pause", "stop", "resume"):
            playing = before["state"] == "playing"
            if action == "resume" and not playing:
                await self.act(self.button(items, PLAY_NAMES))
            elif action in ("pause", "stop") and playing:
                await self.act(self.button(items, PAUSE_NAMES))
            if action == "resume":
                return await self.wait_playing()
            after = await self.now_playing()
            if after["state"] != "paused":
                raise ToolError("Пауза не подтверждена.")
            return {"ok": True, "action": action, **after}
        if action in ("next", "previous"):
            names = (
                ("далее", "next", "next song")
                if action == "next"
                else ("назад", "previous", "previous song")
            )
            await self.act(self.button(items, names))
            for _ in range(15):
                await asyncio.sleep(0.3)
                after = await self.now_playing()
                if after.get("track") and after.get("track") != before.get("track"):
                    return {"ok": True, "action": action, **after}
            raise ToolError("Смена трека не подтверждена.")
        if action in ("repeat_on", "repeat_off", "repeat_one"):
            states = {
                "повтор отключен": "off",
                "repeat off": "off",
                "повторять все треки": "all",
                "repeat all": "all",
                "повторять один трек": "one",
                "repeat one": "one",
            }
            desired = {"repeat_on": "all", "repeat_off": "off", "repeat_one": "one"}[action]
            for _ in range(4):
                buttons = [
                    n
                    for n in player_nodes(await self.snapshot())
                    if n.get("role") == "AXButton" and label(n).casefold() in states
                ]
                if len(buttons) != 1:
                    break
                if states[label(buttons[0]).casefold()] == desired:
                    return {"ok": True, "repeat": desired}
                await self.act(buttons[0])
                await asyncio.sleep(0.2)
            raise ToolError("Режим повтора не подтверждён.")
        if action == "like":
            from backend.tools.registry import user_text

            text = user_text.get() or ""
            if not re.search(
                r"поставь.{0,20}лайк|лайкни|добавь.{0,30}понравивш|like this", text, re.I
            ) or re.search(r"\bне\s+(?:ставь|добавляй|лайкай)", text, re.I):
                raise ToolError("Ставить лайк можно только по прямой просьбе.")
            button = self.button(items, ("нравится", "like"))
            if button.get("value") != "1":
                await self.act(button)
            after = self.button(player_nodes(await self.snapshot()), ("нравится", "like"))
            if after.get("value") != "1":
                raise ToolError("Лайк не подтверждён.")
            return {"ok": True, "liked": True}
        raise ToolError(
            "Safari Web App не сообщает состояние перемешивания через Accessibility. "
            "Надёжно включить или выключить его сейчас нельзя; переключи вручную."
        )

    async def seek(self, seconds: float, relative: bool = False) -> dict:
        raise ToolError(
            "Перемотка пока не поддерживается: Safari Web App принимает изменение "
            "ползунка Accessibility, но фактическая позиция трека не меняется."
        )

    async def set_volume(self, level: int) -> dict:
        items = player_nodes(await self.snapshot())
        slider = next(
            (
                n
                for n in items
                if n.get("role") == "AXSlider" and label(n).casefold() in ("громкость", "volume")
            ),
            None,
        )
        if not slider:
            raise ToolError("Ползунок громкости YouTube Music недоступен.")
        level = max(0, min(100, int(level)))
        await self.act(slider, level)
        after = await self.now_playing()
        if abs(after.get("volume", -100) - level) > 1:
            raise ToolError("Изменение громкости не подтверждено.")
        return {"ok": True, "volume": after["volume"], "provider": "youtube_music"}
