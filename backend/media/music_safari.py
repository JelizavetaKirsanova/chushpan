"""YouTube Music in an existing Safari tab. Uses Apple Events and public DOM only."""

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from backend.media.safari import process
from backend.media.youtube import YouTubeMusic, normal, scenario_key
from backend.tools.registry import ToolError, user_text

ERRORS = {
    "automation_denied": "Нужно разрешить Автоматизация → Safari процессу запуска Чушпана в "
    "Системные настройки → Конфиденциальность и безопасность.",
    "automation_unknown": "Доступ Apple Events к Safari не определён. Открой Safari и скажи "
    "«Чушпан, разреши управление YouTube Music» или явно выполни music-check --request-access "
    "из среды запуска Чушпана; права другой копии приложения "
    "не считаются "
    "подтверждёнными.",
    "javascript_disabled": "Safari не разрешил JavaScript из Apple Events. В Safari включи "
    "Настройки → Разработчик → Разрешить JavaScript из событий Apple (Allow "
    "JavaScript from Apple Events).",
    "safari_closed": "Safari недоступен этому процессу: браузер закрыт или среда ограничивает "
    "Apple Events. Открой Safari и вкладку https://music.youtube.com/; повтори проверку "
    "из того же приложения, что запускает Чушпана.",
    "no_tab": "В Safari нет вкладки YouTube Music. Открой https://music.youtube.com/.",
    "multiple_tabs": "В Safari несколько вкладок YouTube Music. Оставь одну нужную; остальные "
    "вкладки не закрываю.",
    "tab_changed": "Вкладка YouTube Music изменилась. Действие остановлено.",
    "authorization_required": "Войди в YouTube Music в Safari вручную. Логины, cookies и токены "
    "в чат не присылай.",
    "autoplay_blocked": "Safari заблокировал звук. Safari → Настройки → Веб-сайты → "
    "Автовоспроизведение → music.youtube.com → Разрешить всё "
    "автовоспроизведение. Затем запусти песню вручную один раз.",
    "playback_blocked": "YouTube Music не подтвердил воспроизведение. Проверь доступность трека "
    "и плеер вручную.",
    "play_button_missing": "Кнопка воспроизведения на странице не найдена однозначно. Музыка не "
    "подтверждена.",
    "control_missing": "Кнопка управления YouTube Music не найдена или недоступна.",
    "no_media": "Вкладка открыта, но последний трек недоступен. Выбери песню в YouTube Music "
    "или назови её.",
    "audio_muted": "Плеер YouTube Music выключил звук или его громкость равна нулю. "
    "Включи звук в плеере вручную; воспроизведение со звуком не подтверждено.",
    "unsupported": "Эта команда YouTube Music не поддерживается данным адаптером Safari.",
    "service_unavailable": "Safari или YouTube Music не ответили. Автоматический повтор не "
    "выполняется.",
}


class MusicBrowserError(ToolError):
    def __init__(self, code):
        self.code = code if code in ERRORS else "service_unavailable"
        super().__init__(ERRORS[self.code])


def validate_music_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "music.youtube.com"
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.path.lstrip("/").split("/")[0]
        not in {"", "search", "watch", "playlist", "channel", "browse", "library"}
    ):
        raise MusicBrowserError("tab_changed")
    return url


class SafariMusicTab:
    def __init__(self, settings, runner=process):
        self.s, self.runner = settings, runner
        self.timeout = settings.media.youtube_music.timeout_s
        self._binary = None
        self._build_lock = asyncio.Lock()
        self._permission = None
        self.stages = {
            "search_executed": False,
            "track_found": False,
            "play_requested": False,
            "play_submitted": False,
            "playback_confirmed": False,
        }

    async def _helper(self) -> Path:
        from backend.paths import native_helper

        if bundled := native_helper("music_safari"):
            return bundled
        async with self._build_lock:
            if self._binary is not None:
                return self._binary
            source = Path(__file__).with_suffix(".swift")
            info = source.with_suffix(".plist")
            digest = hashlib.sha256(source.read_bytes() + info.read_bytes()).hexdigest()[:16]
            cache = self.s.data_path / "native"
            cache.mkdir(parents=True, exist_ok=True)
            target = cache / f"music-safari-{digest}"
            if not target.exists():
                temporary = target.with_suffix(".building")
                try:
                    await self.runner(
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
                        str(info),
                        "-o",
                        str(temporary),
                        timeout=90,
                    )
                    temporary.replace(target)
                except (ToolError, TimeoutError, OSError):
                    temporary.unlink(missing_ok=True)
                    raise MusicBrowserError("service_unavailable") from None
            self._binary = target
            return target

    async def _native(self, request: dict) -> dict:
        try:
            binary = await self._helper()
            result = json.loads(
                await self.runner(
                    str(binary),
                    data=request,
                    timeout=max(self.timeout, 60)
                    if request.get("request_access")
                    else self.timeout,
                )
            )
            if not isinstance(result, dict):
                raise ValueError("invalid helper response")
        except (ToolError, TimeoutError, OSError, ValueError):
            raise MusicBrowserError("service_unavailable") from None
        if result.get("error"):
            if result["error"] in {"automation_denied", "automation_unknown"}:
                self._permission = None
            raise MusicBrowserError(result["error"])
        return result

    async def _run(self, script: str) -> str:
        result = await self._native({"op": "script", "source": script, "request_access": False})
        return result.get("result", "")

    async def request_access(self):
        # Explicit user action only: a read-only event may show the macOS consent dialog.
        await self._native(
            {
                "op": "script",
                "source": 'tell application "Safari" to count windows',
                "request_access": True,
            }
        )
        self._permission = None

    async def _ready(self):
        if self._permission is None or self._permission.get("automation") != "granted":
            self._permission = await self._native({"op": "probe"})
        state = self._permission["automation"]
        if state != "granted":
            raise MusicBrowserError(
                "automation_denied"
                if state == "denied"
                else "safari_closed"
                if state == "target_not_running"
                else "automation_unknown"
            )

    async def diagnose(self) -> dict:
        try:
            permission = await self._native({"op": "probe"})
        except MusicBrowserError as exc:
            return {"error_code": exc.code, "message": str(exc), **self.stages}
        self._permission = permission
        report = {
            **permission,
            "launcher": {"pid": os.getpid(), "ppid": os.getppid(), "executable": sys.executable},
            "permission_scope": "current_launch_only; another app/Terminal may differ",
            "safari_found": any(
                Path(path).exists()
                for path in ("/Applications/Safari.app", "/System/Applications/Safari.app")
            ),
            "tab_found": None,
            "javascript": "not_checked",
            "authorized": "unknown",
            "state": "unknown",
            **self.stages,
        }
        try:
            tabs = await self.tabs()
            report["tab_count"] = len(tabs)
            report["tab_found"] = bool(tabs)
            if len(tabs) == 1:
                state = await self.now_playing()
                report.update(
                    javascript="granted",
                    authorized=False
                    if state.get("logged_out")
                    else True
                    if state.get("signed_in")
                    else "unknown",
                    state=state.get("state", "unknown"),
                )
                reason = (
                    "authorization_required"
                    if state.get("logged_out")
                    else (state.get("play_error") if state.get("state") != "playing" else None)
                )
                if reason in ERRORS:
                    report.update(error_code=reason, message=ERRORS[reason])
                if state.get("state") == "playing":
                    await asyncio.sleep(0.3)
                    after = await self.now_playing()
                    report["playback_confirmed"] = bool(
                        after.get("ready")
                        and not after.get("muted")
                        and after.get("volume", 100) != 0
                        and not after.get("ad")
                        and after.get("track")
                        and after.get("track") == state.get("track")
                        and after.get("position", 0) > state.get("position", 0)
                    )
        except MusicBrowserError as exc:
            report.update(error_code=exc.code, message=str(exc))
        return report

    async def tabs(self) -> list[dict]:
        await self._ready()
        running = await self._run('application id "com.apple.Safari" is running')
        if running.strip() != "true":
            raise MusicBrowserError("safari_closed")
        raw = await self._run("""tell application "Safari"
set found to ""
repeat with w in windows
repeat with n from 1 to count of tabs of w
set u to URL of tab n of w
if u starts with "https://music.youtube.com/" then
set found to found & (id of w as text) & ":" & n & linefeed
end if
end repeat
end repeat
return found
end tell""")
        return [
            {"window": int(line.split(":")[0]), "tab": int(line.split(":")[1])}
            for line in raw.splitlines()
            if re.fullmatch(r"\d+:\d+", line)
        ]

    async def target(self, *, create=False) -> dict:
        tabs = await self.tabs()
        if len(tabs) > 1:
            raise MusicBrowserError("multiple_tabs")
        if not tabs and create:
            await self._run("""tell application "Safari"
if (count of windows) is 0 then
make new document with properties {URL:"https://music.youtube.com/"}
else
make new tab at end of tabs of front window with properties {URL:"https://music.youtube.com/"}
end if
end tell""")
            tabs = await self.tabs()
        if len(tabs) != 1:
            raise MusicBrowserError("no_tab")
        return tabs[0]

    async def call(self, op: str, **payload) -> dict:
        if op not in {
            "inspect",
            "items",
            "page_play",
            "resume",
            "pause",
            "next",
            "previous",
            "volume",
            "seek",
        }:
            raise MusicBrowserError("unsupported")
        target = await self.target()
        source = Path(__file__).with_name("music_tab.js").read_text()
        js = (
            "JSON.stringify((function(){"
            + source
            + "return chushpanMusic("
            + json.dumps({"op": op, **payload}, ensure_ascii=False)
            + ");})())"
        )
        script = f"""tell application "Safari"
set t to tab {target["tab"]} of window id {target["window"]}
if URL of t does not start with "https://music.youtube.com/" then return "tab_changed"
return do JavaScript {json.dumps(js, ensure_ascii=False)} in t
end tell"""
        raw = await self._run(script)
        if raw == "tab_changed":
            raise MusicBrowserError("tab_changed")
        try:
            result = json.loads(raw)
        except (ValueError, TypeError):
            raise MusicBrowserError("service_unavailable") from None
        if result.get("error"):
            raise MusicBrowserError(result["error"])
        return result

    async def navigate(self, url: str):
        validate_music_url(url)
        target = await self.target(create=True)
        actual = await self._run(f"""tell application "Safari"
set t to tab {target["tab"]} of window id {target["window"]}
if URL of t does not start with "https://music.youtube.com/" then error "tab changed"
set current tab of window id {target["window"]} to t
set index of window id {target["window"]} to 1
activate
set URL of t to {json.dumps(url)}
return URL of t
end tell""")
        expected, observed = urlparse(url), urlparse(actual)
        if (
            observed.scheme != "https"
            or observed.netloc != "music.youtube.com"
            or observed.path != expected.path
            or not all(
                parse_qs(observed.query).get(key) == value
                for key, value in parse_qs(expected.query).items()
            )
        ):
            raise MusicBrowserError("tab_changed")

    async def now_playing(self):
        return {**await self.call("inspect"), "provider": "youtube_music"}

    async def launch(self):
        # Compatibility with the existing morning routine; no playback here.
        await self.target(create=True)
        return {"ok": True, "action": "opened"}

    async def start_song(self, title):
        # Navigation is asynchronous: never resume the previous page's media element.
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            state = await self.now_playing()
            if state.get("logged_out"):
                raise MusicBrowserError("authorization_required")
            if state.get("muted") or state.get("volume") == 0:
                raise MusicBrowserError("audio_muted")
            if normal(state.get("track", "")) == normal(title) and state.get("ready"):
                self.stages["play_requested"] = True
                await self.call("resume")
                self.stages["play_submitted"] = True
                return
            await asyncio.sleep(0.3)
        raise MusicBrowserError("no_media")

    async def wait_playing(self, title="", artist="", playlist="", playlist_title=""):
        deadline, previous = time.monotonic() + self.timeout, None
        while time.monotonic() < deadline:
            state = await self.now_playing()
            if state.get("play_error"):
                raise MusicBrowserError(state["play_error"])
            if state.get("logged_out"):
                raise MusicBrowserError("authorization_required")
            if state.get("muted") or state.get("volume") == 0:
                raise MusicBrowserError("audio_muted")
            playlist_matches = not playlist or state.get("playlist_id") == playlist
            if (
                playlist
                and not state.get("playlist_id")
                and playlist_title
                and state.get("page_playlist_id") == playlist
                and normal(state.get("playlist_title", "")) == normal(playlist_title)
            ):
                playlist_matches = True
            if (
                state.get("state") == "playing"
                and state.get("ready")
                and state.get("track")
                and not state.get("ad")
                and (not title or normal(title) == normal(state["track"]))
                and playlist_matches
                and (not artist or normal(artist) in normal(state.get("artist", "")))
            ):
                if (
                    previous
                    and previous.get("track") == state.get("track")
                    and previous.get("playlist_id") == state.get("playlist_id")
                    and previous.get("playlist_title") == state.get("playlist_title")
                    and state["position"] > previous["position"]
                ):
                    self.stages["playback_confirmed"] = True
                    return {
                        **state,
                        "ok": True,
                        "playlist_id": playlist or state.get("playlist_id"),
                        "playback_confirmed": True,
                        "confirmation": {
                            "source": "safari_media_element",
                            "position_before": previous["position"],
                            "position_after": state["position"],
                            "playlist_id": playlist or state.get("playlist_id"),
                            "playlist_source": "player_bar"
                            if state.get("playlist_id")
                            else "queue_title_and_page_id"
                            if playlist
                            else None,
                        },
                    }
                previous = state
            else:
                previous = None
            await asyncio.sleep(0.3)
        raise MusicBrowserError("playback_blocked")

    async def start_page(self, title):
        # Retry only an explicit no-click result while the SPA loads. An ambiguous
        # Apple Events/network error is never retried after a possible click.
        deadline = time.monotonic() + self.timeout
        self.stages["play_requested"] = True
        while True:
            try:
                result = await self.call("page_play", title=title)
            except MusicBrowserError as exc:
                if exc.code != "play_button_missing" or time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.3)
                continue
            if result.get("requested") is not True:
                raise MusicBrowserError("playback_blocked")
            self.stages["play_submitted"] = True
            return

    async def control(self, action):
        if action == "open":
            await self.target(create=True)
            return {"ok": True, "action": "opened"}
        before = await self.now_playing()
        op = "pause" if action == "stop" else action
        await self.call(op)
        self.stages["play_submitted"] = True
        if op == "pause":
            after = await self.now_playing()
            if after["state"] != "paused":
                raise MusicBrowserError("playback_blocked")
            return {"ok": True, **after}
        if op in {"next", "previous"}:
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                after = await self.now_playing()
                if after.get("track") and after["track"] != before.get("track"):
                    return await self.wait_playing(after["track"])
                await asyncio.sleep(0.3)
            raise MusicBrowserError("playback_blocked")
        self.stages["play_requested"] = True
        return await self.wait_playing()

    async def set_volume(self, level):
        await self.call("volume", level=max(0, min(100, int(level))))
        state = await self.now_playing()
        if state.get("volume") != max(0, min(100, int(level))):
            raise MusicBrowserError("playback_blocked")
        return {"ok": True, "volume": state["volume"]}

    async def seek(self, seconds, relative=False):
        before = await self.now_playing()
        target = max(0, seconds + (before.get("position", 0) if relative else 0))
        await self.call("seek", seconds=seconds, relative=relative)
        after = await self.now_playing()
        if abs(after.get("position", 0) - target) > 2:
            raise MusicBrowserError("playback_blocked")
        return {"ok": True, **after}


class SafariYouTubeMusic(YouTubeMusic):
    def __init__(self, settings, store=None, app=None):
        super().__init__(settings, store, app=app or SafariMusicTab(settings))

    async def library(self):
        async with self.lock:
            await self.app.navigate("https://music.youtube.com/library/playlists")
            return {
                "playlists": await self._items("playlist"),
                "complete": False,
                "source": "safari_dom",
            }

    async def _items(self, kind):
        deadline = time.monotonic() + self.s.media.youtube_music.timeout_s
        while time.monotonic() < deadline:
            result = await self.app.call("items", kind=kind)
            items = result.get("items", [])
            if items:
                for item in items:
                    validate_music_url(item["href"])
                    url = urlparse(item["href"])
                    item["id"] = parse_qs(url.query).get(
                        "list", parse_qs(url.query).get("v", [url.path.rsplit("/", 1)[-1]])
                    )[0]
                return items
            await asyncio.sleep(0.3)
        return []

    async def search(self, query, kind="song"):
        if kind not in {"song", "artist", "album", "playlist"}:
            raise MusicBrowserError("unsupported")
        async with self.lock:
            await self.app.navigate("https://music.youtube.com/search?" + urlencode({"q": query}))
            self.app.stages["search_executed"] = True
            items = await self._items(kind)
        exact = [
            item
            for item in items
            if normal(query)
            in {normal(item["title"]), normal(item["title"] + " " + item.get("artist", ""))}
        ]
        if len(exact) == 1:
            self.app.stages["track_found"] = True
            return {"item": exact[0]}
        return {"status": "choose" if items else "not_found", "candidates": items[:5]}

    async def play(self, query="", kind="auto", item_id="", situation="", open_only=False):
        for key in self.app.stages:
            self.app.stages[key] = False
        if situation:
            if scenario_key(situation) != "rest" or not re.search(
                r"спокой|отдых", user_text.get(), re.I
            ):
                raise MusicBrowserError("unsupported")
        if not query and not item_id and kind != "playlist" and not situation:
            async with self.lock:
                await self.app.target(create=True)
                return await self.app.control("resume")
        if (kind == "playlist" and not query) or situation:
            cfg = (
                self.s.media.youtube_music.scenarios.get(scenario_key(situation))
                if situation
                else self.s.media.youtube_music.main_playlist
            )
            known = self.preference(scenario_key(situation))
            query = (known or {}).get("title") or (cfg.title if cfg else "")
            identity = (known or {}).get("id") or (cfg.id if cfg else "")
            if identity:
                if not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
                    raise MusicBrowserError("tab_changed")
                item_id, item = identity, None
                kind = "playlist"
            elif not query:
                return {"status": "choose", **await self.library()}
            else:
                item = None
                kind = "playlist"
        else:
            item = None
        if item is None:
            if kind == "playlist":
                from backend.media.youtube import candidates

                library = await self.library()
                found = candidates(item_id or query, library["playlists"])
                found["complete"] = library.get("complete", False)
            else:
                found = await self.search(query, "song" if kind == "auto" else kind)
            item = found.get("item")
            if item is None:
                return found
            if item_id and item_id != item.get("id"):
                raise MusicBrowserError("tab_changed")
        async with self.lock:
            await self.app.navigate(item["href"])
            if open_only:
                return {"ok": True, "action": "opened"}
            if kind in {"auto", "song"}:
                await self.app.start_song(item["title"])
            else:
                await self.app.start_page(item["title"])
            playlist_id = parse_qs(urlparse(item["href"]).query).get("list", [""])[0]
            return await self.app.wait_playing(
                item["title"] if kind in {"song", "auto"} else "",
                item["title"] if kind == "artist" else "",
                playlist_id,
                item["title"] if playlist_id else "",
            )

    async def playlist(self, action="list", query="", playlist_id="", situation="default"):
        if action == "get":
            return {"playlist": self.preference(scenario_key(situation))}
        if action == "list":
            return await self.library()
        if action != "assign":
            raise MusicBrowserError("unsupported")
        from backend.media.youtube import candidates

        if not re.search(r"назначь|запомни|сделай|выбери|поставь", user_text.get(), re.I):
            raise MusicBrowserError("unsupported")
        found = candidates(playlist_id or query, (await self.library())["playlists"])
        if "item" not in found:
            return found
        self.preference(scenario_key(situation), found["item"])
        return {"ok": True, "playlist": found["item"]}
