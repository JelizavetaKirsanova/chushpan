"""Repeat explicit browser actions on a page supplied by the viewer."""

import asyncio
import contextlib
import ipaddress
import json
import math
import re
import time
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.cinema.adapters import HTML5Adapter
from backend.cinema.browser_search import BrowserSearch, SearchSession
from backend.cinema.models import web_url
from backend.cinema.playback import playback_error
from backend.cinema.site_adapters import OwnedHTML5Bridge, page_adapter, player_engine
from backend.cinema.sources import addresses
from backend.media.safari import AccessibilityApp
from backend.permission_policy import requests_allowed
from backend.tools.registry import ToolError

PAGE = r"""(request => {
 if (location.href !== request.url) return JSON.stringify({changed:true});
 const visible=e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const norm=s=>(s||'').trim().replace(/\s+/g,' ');
 const heading=norm(document.querySelector('h1')?.textContent||document.title);
 const restriction=/just a moment|verify you are human|checking your browser|captcha|access denied/;
 const visibleText=document.title+' '+(document.body?.innerText||'').slice(0,500);
 const restricted=restriction.test(visibleText.toLowerCase()) ||
   /доступ ограничен|недоступно в вашем регионе/i.test(visibleText) ||
   [...document.querySelectorAll('input[type=password]')].some(visible);
 if(restricted) return JSON.stringify({heading,restricted});
 const kinogo=request.page_adapter==='kinogo';
 const tabSelector=kinogo?'.js-player-tabs > li':'[role=tab],button';
 const label=e=>norm(e.getAttribute('aria-label')||e.textContent);
 let tabs=[...document.querySelectorAll(tabSelector)].filter(visible);
 if(!tabs.length)tabs=[...document.querySelectorAll('[role=tab],button,a,[role=button]')]
   .filter(e=>visible(e)&&/^(плеер|player)\s*\d*$/i.test(label(e)));
 const names=tabs.map(label).filter(x=>/^(плеер|player)\s*\d*$|^смотреть онлайн$/i.test(x));
 const choice=tabs.filter(e=>label(e)===request.player);
 if(request.prepare&&choice.length===1){
   choice[0].scrollIntoView({block:'center'});
   if(!choice[0].matches('.current,[aria-selected=true]'))choice[0].click();
 }
 const frames=[...document.querySelectorAll('iframe')].filter(visible).flatMap(e=>{
   if(!e.getAttribute('src'))return [];
   try{
     const u=new URL(e.src), box=e.getBoundingClientRect();
     return u.protocol==='https:'&&box.width>=250&&box.height>=120?[{host:u.hostname}]:[];
   }catch{return [];}
 });
 if(request.reveal&&frames.length===1){
   const frame=[...document.querySelectorAll('iframe')].find(e=>{
     try{return visible(e)&&new URL(e.src).hostname===frames[0].host;}catch{return false;}
   });
   frame?.scrollIntoView({block:request.controls?'start':'end',inline:'start'});
   if(frame&&request.controls){
     // Keep the frame's top controls below a real fixed/sticky page header.
     const headers=[...document.querySelectorAll('body *')].filter(visible).flatMap(e=>{
       if(!['fixed','sticky'].includes(getComputedStyle(e).position))return [];
       const r=e.getBoundingClientRect();
       return r.top<=1&&r.bottom>0&&r.height<innerHeight*.35&&r.width>=innerWidth*.5?[r.bottom]:[];
     });
     if(headers.length)scrollBy(0,-Math.ceil(Math.max(...headers))-8);
   }
 }
 const active=tabs.find(e=>e.matches('.current,[aria-selected=true]'));
 const fs=document.fullscreenElement||document.webkitFullscreenElement||
   document.webkitCurrentFullScreenElement;
 const visibleFrames=[...document.querySelectorAll('iframe')].filter(visible);
 const fullFrame=fs&&visibleFrames.find(e=>e===fs);
 const videos=[...document.querySelectorAll('video')].filter(visible);
 const year=(document.body?.innerText||'')
   .match(/(?:Год выпуска|Год|Release year)\s*:\s*((?:19|20)\d{2})/i);
 if(request.exit_fullscreen&&fs&&(fullFrame||videos.some(v=>v===fs||fs.contains(v)))){
   try{(document.exitFullscreen||document.webkitExitFullscreen)?.call(document);}catch{}
 }
 if(request.exit_fullscreen){for(const video of videos)if(video.webkitDisplayingFullscreen===true){
   try{(video.webkitExitFullscreen||video.webkitExitFullScreen)?.call(video);}catch{}
 }}
 return JSON.stringify({heading,names,frames,selected:active?label(active):request.player,
   videos:videos.length,year:year?Number(year[1]):0,
   fullscreen:!!fullFrame||videos.some(v=>v===fs||fs?.contains(v)||v.webkitDisplayingFullscreen===true),
   fullscreen_host:fullFrame?new URL(fullFrame.src).hostname:''});
})"""
PLAY = r"(?:Смотреть|Воспроизвести|Play)(?:\s*\([^)]*\))?(?:\s*или клик в любом месте)?"
PAUSE = r"(?:Пауза|Pause)(?:\s*\([^)]*\))?"


class SiteRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    url: str
    player: str = Field(default="", max_length=80)
    voice: str = Field(default="", max_length=100)
    engine: Literal["auto", "html5", "semantic_ax", "venom_ax"] = "auto"
    template_id: str = Field(default="", max_length=64, pattern=r"^[a-zA-Z0-9-]*$")

    @field_validator("url")
    @classmethod
    def public_https(cls, value):
        value = web_url(value)
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or "." not in parsed.hostname
            or parsed.fragment
            or "chushpan_session=" in parsed.query
        ):
            raise ValueError("Укажи публичную HTTPS-страницу просмотра.")
        return value


class SafariPlayerAX(AccessibilityApp):
    bundle_id = "com.apple.Safari"

    def __init__(self, settings):
        super().__init__(settings, "Плеер Safari", timeout_s=5)

    async def cinema(self, op, **values):
        token = requests_allowed.set(False)
        try:
            return await self.request(op, **values)
        except ToolError as error:
            for code, message in {
                "cinema_page_unavailable": "Выбранная вкладка Safari недоступна.",
                "cinema_frame_unavailable": "Выбранный iframe плеера ещё недоступен.",
                "cinema_control_offscreen": "Кнопка плеера находится за пределами окна Safari.",
                "cinema_geometry_unavailable": "Safari не сообщил положение элемента плеера.",
                "cinema_hit_unavailable": "Safari не подтвердил положение кнопки плеера.",
                "cinema_hit_changed": "Другая панель перекрыла элемент плеера; нажатие отменено.",
            }.items():
                if code in str(error):
                    raise playback_error(code, message) from None
            raise
        finally:
            requests_allowed.reset(token)


def label(node):
    return node.get("value") or node.get("title") or node.get("description") or node.get("help", "")


def exact(nodes, pattern):
    matches = [n for n in nodes if re.fullmatch(pattern, label(n), re.I)]
    return matches[0] if len(matches) == 1 else None


def voice_option(nodes):
    episode = exact(nodes, r"\d+ серия")
    if episode:
        return next(
            (
                node
                for node in nodes
                if node["path"][:-1] == episode["path"][:-1]
                and node["path"][-1:] > episode["path"][-1:]
                and node.get("role") == "AXStaticText"
                and label(node)
            ),
            None,
        )
    # Movie players have an audio menu without season/episode menus.
    return next(
        (
            node
            for node in nodes
            if node.get("role") == "AXStaticText"
            and label(node).strip()
            and not re.search(r"\d|/", label(node))
            and len(node.get("path", [])) >= 3
        ),
        None,
    )


def seconds(value):
    result = 0
    for part in value.split(":"):
        result = result * 60 + int(part)
    return result


def player_state(nodes):
    times = [
        re.fullmatch(r"(\d{1,3}(?::\d{2}){1,2})\s*/\s*(\d{1,3}(?::\d{2}){1,2})", label(n))
        for n in nodes
        if n.get("role") == "AXStaticText"
    ]
    matches = [match for match in times if match]
    if len(matches) != 1:
        return None
    position, duration = map(seconds, matches[0].groups())
    if position > duration:
        return None
    buttons = [n for n in nodes if n.get("role") == "AXButton"]
    paused = True if exact(buttons, PLAY) else False if exact(buttons, PAUSE) else None
    return {
        "position": position,
        "duration": duration,
        "paused": paused,
        "observed_at": int(time.time() * 1000),
    }


def normalize_title(value):
    return " ".join(re.findall(r"\w+", value.casefold().replace("ё", "е")))


def title_agrees(heading, item):
    heading = normalize_title(re.sub(r"\s*\([^)]*\)\s*$", "", heading))
    names = [item["title"], (item.get("metadata") or {}).get("original_title", "")]
    return bool(heading and heading in {normalize_title(name) for name in names})


class LearnedPlayer:
    def __init__(self, executor, ax, *, tabs=None):
        self.tabs = tabs or BrowserSearch(executor)
        self.ax = ax
        self.session = None
        self.url = self.frame_host = ""
        self.report = {}
        self.episode = None
        self.voice = ""
        self.minimum = 0
        self.last_sample = None
        self.engine = "semantic_ax"
        self.html5 = None
        self.recipe = None
        self.capabilities = {}
        self.playing = False
        self.audio_tracks = []
        self.pending_next = None
        self.next_clock = None

    async def javascript(self, script):
        raw = await self.tabs._owned(
            self.session,
            "return do JavaScript " + json.dumps(script, ensure_ascii=False) + " in ownedTab",
        )
        if raw == "unowned":
            raise ToolError("Safari закрыл или перенаправил учебную вкладку.")
        return json.loads(raw)

    async def page(self, **values):
        adapter = page_adapter(self.url)
        script = (
            PAGE
            + "("
            + json.dumps(
                {**values, "url": self.url, "page_adapter": adapter.id if adapter else "generic"},
                ensure_ascii=False,
            )
            + ")"
        )
        data = await self.javascript(script)
        if data.get("restricted"):
            raise ToolError("Страница требует входа, проверки браузера или ограничивает доступ.")
        if data.get("changed"):
            raise ToolError("Адрес учебной страницы изменился.")
        return data

    async def open_page(self, recipe, item, frame_hint=""):
        parsed = urlsplit(recipe.url)
        ips = await asyncio.wait_for(addresses(parsed.hostname, parsed.port or 443), 5)
        if not ips or not all(ipaddress.ip_address(ip).is_global for ip in ips):
            raise ToolError("Адрес сайта не является публичным.")
        session = SearchSession(uuid4().hex, prefix=recipe.url + "#")
        self.session = session
        self.url = urlunsplit(parsed._replace(fragment=session.tag))
        await self.tabs.open_marked(session, self.url)
        await self.activate()
        self.recipe = recipe
        adapter = page_adapter(recipe.url)
        player = recipe.player or (adapter.preferred_players[0] if adapter else "")
        for _ in range(25):
            data = await self.page(player=player, prepare=False)
            if data.get("heading"):
                break
            await asyncio.sleep(0.2)
        if not title_agrees(data.get("heading", ""), item):
            raise ToolError("Заголовок страницы не совпал с выбранным произведением.")
        if data.get("year") and item.get("year") and abs(data["year"] - item["year"]) > 1:
            raise ToolError("Год на странице не совпал с выбранным произведением.")
        self.report = {
            "page_opened": True,
            "heading": data["heading"],
            "players": data["names"],
            "player": player,
        }
        if data.get("videos") == 1 and not player:
            self.engine = "html5"
            self.frame_host = parsed.hostname
            self.report.update(engine=self.engine, frame_host=self.frame_host)
            return
        if not player:
            if len(data["frames"]) == 1:
                self.frame_host = data["frames"][0]["host"]
                self.engine = player_engine(self.frame_host, recipe.engine)
                self.report.update(engine=self.engine, frame_host=self.frame_host)
                return
            if not data["names"]:
                return
            player = data["names"][0]
            self.report["player"] = player
        if data["names"].count(player) != 1:
            raise ToolError("Сохранённая вкладка плеера исчезла. Проверь её название в сценарии.")
        await self.page(player=player, prepare=True)
        for _ in range(25):
            await asyncio.sleep(0.25)
            data = await self.page(player=player, prepare=False)
            frames = data["frames"]
            if data["selected"] == player and len(frames) == 1:
                host = frames[0]["host"]
                if frame_hint and host != frame_hint:
                    raise ToolError("Домен плеера изменился. Проверь и сохрани сценарий заново.")
                self.frame_host = host
                self.engine = player_engine(host, recipe.engine)
                self.report.update(frame_host=host, engine=self.engine)
                return
        raise ToolError("Выбранная вкладка не показала один доступный iframe плеера.")

    async def activate(self):
        fullscreen = (await self.page()).get("fullscreen")
        action = (
            'activate\nreturn "sent"'
            if fullscreen
            else (
                "if URL of current tab of window id (ownerID as integer) "
                "is not currentAddress then\n"
                "set current tab of window id (ownerID as integer) to ownedTab\nend if\n"
                "try\nif id of window 1 is not (ownerID as integer) then\n"
                "set index of window id (ownerID as integer) to 1\nend if\nend try\n"
                'activate\nreturn "sent"'
            )
        )
        result = await self.tabs._owned(
            self.session,
            action,
        )
        if result != "sent":
            raise ToolError("Учебная вкладка Safari закрыта или изменила адрес.")
        status = await self.ax.cinema("status")
        if not status.get("frontmost"):
            pids = status.get("pids", [])
            if len(pids) != 1:
                raise ToolError("Safari не подтвердил единственный процесс плеера.")
            await self.ax.cinema(
                "desktop_app", target_bundle="com.apple.Safari", pid=pids[0], action="switch"
            )
            if await self.tabs._owned(self.session, action) != "sent":
                raise ToolError("Учебная вкладка Safari закрыта или изменила адрес.")

    async def nodes(self):
        if not self.frame_host:
            return []
        return (await self.ax.cinema("cinema_snapshot", page=self.url, frame_host=self.frame_host))[
            "nodes"
        ]

    async def press(self, node, *, pointer=False, op="cinema_press", raise_window=False):
        expected = {
            k: node[k] for k in ("role", "title", "description", "value", "help") if k in node
        }
        for attempt in range(6):
            try:
                await self.ax.cinema(
                    op,
                    page=self.url,
                    frame_host=self.frame_host,
                    path=node["path"],
                    expected=expected,
                    force_pointer=pointer,
                    raise_window=raise_window,
                )
                return
            except ToolError as error:
                # AX can expose a fading control before it accepts pointer
                # events. Retry only the unchanged target; every attempt still
                # has to pass the native hit test before any click is sent.
                if (
                    op != "cinema_press"
                    or attempt == 5
                    or getattr(error, "code", "")
                    not in {
                        "cinema_hit_changed",
                        "cinema_hit_unavailable",
                        "cinema_control_offscreen",
                    }
                ):
                    raise
                await asyncio.sleep(0.2)
                current = next((n for n in await self.nodes() if n["path"] == node["path"]), None)
                if not current or any(current.get(k, "") != v for k, v in expected.items()):
                    raise ToolError(
                        "Элемент плеера изменился во время ожидания нажатия."
                    ) from error

    async def key(self, key, *, large=False, repeat_count=1):
        if self.engine != "venom_ax":
            raise ToolError("Клавиши этого плеера ещё не проверены. Используй его кнопки.")
        await self.ax.cinema(
            "cinema_key",
            page=self.url,
            frame_host=self.frame_host,
            key=key,
            large=large,
            repeat_count=repeat_count,
        )

    async def seek_keys(self, position, before):
        # Reviewed shortcuts: arrows 5 s, Shift+arrows 20 s. Re-read the
        # selected episode and clock between bounded groups of ordinary keys.
        state = before
        stalled = 0
        for _ in range(40):
            delta = position - state["position"]
            if abs(delta) <= 3:
                return {
                    **state,
                    "seek_confirmed": True,
                    "requested_position": position,
                    "message": f"Плеер подтвердил позицию {int(state['position'])} с.",
                }
            large = abs(delta) >= 25
            step = 20 if large else 5
            count = max(1, min(12, int((abs(delta) - 2) / step)))
            await self.key(
                "seek_forward" if delta > 0 else "seek_back", large=large, repeat_count=count
            )
            await asyncio.sleep(0.3)
            after = await self.sample()
            if after["duration"] != before["duration"]:
                raise ToolError("Видео изменилось во время восстановления позиции.")
            change = after["position"] - state["position"]
            stalled = stalled + 1 if abs(change) < 2 else 0
            if stalled >= 3 or (abs(delta) > 25 and change * delta < 0):
                raise ToolError("Плеер не подтвердил действие штатных клавиш перемотки.")
            state = after
        raise ToolError("Истекло время восстановления позиции плеера.")

    async def reveal(self, *, controls=False):
        await self.activate()
        page = await self.page(
            player=self.report.get("player", ""), prepare=False, reveal=True, controls=controls
        )
        last_error = None
        for _ in range(12):
            try:
                nodes = await self.nodes()
                if nodes:
                    await self.press(
                        nodes[0], op="cinema_hover", raise_window=page.get("fullscreen") is False
                    )
                    return
            except ToolError as error:
                if getattr(error, "code", "") not in {
                    "cinema_page_unavailable",
                    "cinema_frame_unavailable",
                    "cinema_control_offscreen",
                    "cinema_geometry_unavailable",
                    "cinema_hit_unavailable",
                    "cinema_hit_changed",
                }:
                    raise
                last_error = error
            await asyncio.sleep(0.15)
        raise last_error or ToolError("Плеер не подтвердил видимость элементов управления.")

    async def seek(self, position):
        if not math.isfinite(position) or position < 0:
            raise ToolError("Неверная позиция видео.")
        before = await self.sample()
        if position >= before["duration"]:
            raise ToolError("Сохранённый таймкод превышает длительность видео.")
        if self.html5:
            return await self.html5.control("seek", position)
        await self.reveal()
        nodes = await self.nodes()
        if self.engine == "venom_ax":
            playing = self.playing and before.get("paused") is not True
            if playing:
                before = await self.control("pause")
            try:
                result = await self.seek_keys(position, before)
            except ToolError:
                if playing:
                    await self.key("play_pause")
                raise
            if playing:
                await self.key("play_pause")
                actual = await self.confirm(await self.nodes())
                result.update(actual)
                result["message"] = f"Плеер подтвердил позицию около {int(position)} с."
            return result
        else:
            sliders = [
                node
                for node in nodes
                if node.get("role") == "AXSlider"
                and node.get("value_settable")
                and re.search(r"seek|time|прогресс|перемот", label(node), re.I)
            ]
            if len(sliders) != 1:
                raise ToolError("У плеера нет проверенного элемента точной перемотки.")
            slider = sliders[0]
            low, high = float(slider["AXMinValue"]), float(slider["AXMaxValue"])
            value = low + (high - low) * position / before["duration"]
            await self.ax.cinema(
                "cinema_set_value",
                page=self.url,
                frame_host=self.frame_host,
                path=slider["path"],
                expected={k: slider[k] for k in ("role", "title", "description") if k in slider},
                value=value,
            )
        for _ in range(20):
            await asyncio.sleep(0.25)
            after = await self.sample()
            if after["duration"] == before["duration"] and abs(after["position"] - position) <= 3:
                return {
                    **after,
                    "seek_confirmed": True,
                    "requested_position": position,
                    "message": f"Плеер подтвердил позицию {int(after['position'])} с.",
                }
        raise ToolError("Плеер не подтвердил перемотку к сохранённому времени.")

    async def action_button(self, nodes, pattern):
        buttons = [n for n in nodes if n.get("role") == "AXButton"]
        found = exact(buttons, pattern)
        if found:
            return found
        for button in buttons[:12]:
            if label(button):
                continue
            try:
                await self.press(button, op="cinema_hover")
                await asyncio.sleep(0.15)
                tree = await self.nodes()
            except ToolError:
                continue
            current = next((n for n in tree if n["path"] == button["path"]), None)
            if current and (
                exact([current], pattern)
                or any(
                    n["path"][: len(button["path"])] == button["path"]
                    and re.fullmatch(pattern, label(n), re.I)
                    for n in tree
                )
            ):
                return current
        return None

    async def fullscreen(self):
        if self.html5:
            try:
                return await self.html5.control("fullscreen")
            except ToolError:
                await self.reveal()
                await self.html5.read("offer_gesture")
                button = await self.action_button(
                    await self.nodes(), r"Чушпан: запустить видео на весь экран"
                )
                if not button:
                    raise ToolError(
                        "Safari требует нажатия кнопки полного экрана самим пользователем."
                    ) from None
                await self.press(button, pointer=True)
                for _ in range(15):
                    await asyncio.sleep(0.2)
                    after = await self.html5.sample()
                    if after.get("fullscreen") and not after.get("fullscreen_pending"):
                        return {**after, "fullscreen_confirmed": True}
                raise ToolError("HTML5-плеер не подтвердил переход в полный экран.") from None
        await self.reveal()
        state = await self.page()
        if state.get("fullscreen") and state.get("fullscreen_host") == self.frame_host:
            return {
                "fullscreen": True,
                "fullscreen_confirmed": True,
                "capabilities": {"fullscreen": True, "fullscreen_available": True},
            }
        nodes = await self.nodes()
        button = await self.action_button(
            nodes,
            r"(?:На весь экран|Во весь экран|Полный экран|Полноэкранный режим|"
            r"Enter Fullscreen|Full Screen)",
        )
        if button:
            await self.press(button)
        elif self.engine == "venom_ax":
            await self.key("fullscreen")
        else:
            raise ToolError("Плеер не показал проверенную кнопку полного экрана.")
        for attempt in range(20):
            await asyncio.sleep(0.2)
            state = await self.page()
            if state.get("fullscreen") and state.get("fullscreen_host") == self.frame_host:
                return {
                    "fullscreen": True,
                    "fullscreen_confirmed": True,
                    "capabilities": {"fullscreen": True, "fullscreen_available": True},
                    "message": "Полноэкранное состояние видеоплеера подтверждено Fullscreen API.",
                }
            if button and attempt == 6:
                with contextlib.suppress(ToolError):
                    await self.press(button, pointer=True)
            if button and attempt == 12 and self.engine == "venom_ax":
                await self.key("fullscreen")
        raise ToolError("Safari не подтвердил полноэкранное состояние видеоплеера.")

    async def wait_nodes(self, predicate):
        for _ in range(20):
            try:
                nodes = await self.nodes()
            except ToolError as error:
                if getattr(error, "code", "") != "cinema_frame_unavailable":
                    raise
            else:
                if predicate(nodes):
                    return nodes
            await asyncio.sleep(0.25)
        raise playback_error(
            "cinema_controls_timeout",
            "Плеер не показал ожидаемые элементы. Проверь выбранную вкладку и сценарий.",
        )

    async def select(self, nodes, menu, desired):
        control = exact(nodes, menu)
        if not control:
            raise ToolError(f"Плеер не показал однозначный элемент выбора «{desired}».")
        if label(control) == desired:
            return nodes
        for attempt in range(2):
            await self.press(control)
            try:
                nodes = await self.wait_nodes(lambda tree: exact(tree, re.escape(desired)))
                break
            except ToolError as error:
                if getattr(error, "code", "") != "cinema_controls_timeout" or attempt:
                    raise
                # Safari may consume the first click to focus a restored
                # window. Retry only if the same menu control remains intact.
                nodes = await self.nodes()
                current = exact(nodes, menu)
                if not current or current["path"] != control["path"]:
                    raise ToolError("Меню плеера изменилось после нажатия.") from error
                control = current
        await self.press(exact(nodes, re.escape(desired)))
        return await self.wait_nodes(
            lambda tree: any(
                node["path"] == control["path"] and label(node) == desired for node in tree
            )
        )

    async def prepare(self, recipe, episode):
        nodes = await self.wait_nodes(
            lambda tree: (
                bool(exact(tree, r"\d+ сезон") and exact(tree, r"\d+ серия"))
                if episode["season"]
                else len(tree) > 3
            )
        )
        if episode["season"]:
            nodes = await self.select(nodes, r"\d+ сезон", f"{episode['season']} сезон")
            nodes = await self.select(nodes, r"\d+ серия", f"{episode['episode']} серия")
        voice = voice_option(nodes)
        if recipe.voice and not voice:
            self.voice = ""
            self.report["voice_note"] = (
                "Плеер не предоставляет выбор озвучки; используется текущая дорожка."
            )
        elif recipe.voice and label(voice) != recipe.voice:
            try:
                nodes = await self.select(nodes, re.escape(label(voice)), recipe.voice)
            except ToolError:
                # Close the audio menu and retain the actual selected track.
                nodes = await self.nodes()
                current = voice_option(nodes)
                if not current:
                    raise
                await self.press(current)
                nodes = await self.nodes()
                self.voice = label(voice_option(nodes) or current)
                self.report["voice_note"] = (
                    f"Озвучка «{recipe.voice}» недоступна; выбрана «{self.voice}»."
                )
        elif voice:
            self.voice = label(voice)
        self.audio_tracks = [self.voice] if self.voice else []
        return nodes

    async def discover_audio_tracks(self, nodes):
        # The menu is populated after media metadata loads on some players.
        # Read it after playback confirmation, not from the initial skeleton.
        control = voice_option(nodes)
        if control:
            await self.reveal(controls=True)
            nodes = await self.nodes()
            control = voice_option(nodes)
            if not control:
                raise ToolError("Плеер изменил меню озвучки во время проверки.")
            previous = {tuple(node["path"]) for node in nodes}
            opened = False
            try:
                await self.press(control)
                opened = True
                for _ in range(10):
                    await asyncio.sleep(0.2)
                    menu = await self.nodes()
                    options = [
                        label(node)
                        for node in menu
                        if node.get("role") == "AXStaticText"
                        and tuple(node["path"]) not in previous
                        and node["path"][: len(control["path"]) - 1] == control["path"][:-1]
                        and 1 < len(label(node)) <= 100
                        and not re.search(r"[:/]|^\d|сезон|серия", label(node), re.I)
                    ]
                    if options:
                        break
                self.audio_tracks = list(dict.fromkeys([*self.audio_tracks, *options]))[:30]
            finally:
                if opened:
                    await self.press(control)
                await self.reveal()

    async def set_voice(self, voice):
        if voice not in self.audio_tracks:
            raise ToolError("Эта озвучка не обнаружена в выбранном плеере.")
        before = await self.sample()
        was_fullscreen = (await self.page()).get("fullscreen") is True
        if was_fullscreen:
            await self.control("fullscreen")
        full = {}
        try:
            await self.reveal(controls=True)
            nodes = await self.nodes()
            control = voice_option(nodes)
            if not control:
                raise ToolError("Плеер не показал меню озвучки.")
            await self.select(nodes, re.escape(label(control)), voice)
            self.voice = voice
            self.last_sample = None
            self.pending_next = self.next_clock = None
            await self.reveal()
            result = await self.confirm(await self.nodes())
            if before["position"] > 3 and abs(result["position"] - before["position"]) > 3:
                result = await self.seek(before["position"])
            self.last_sample = result
        finally:
            if was_fullscreen and self.last_sample and self.playing:
                try:
                    full = await self.fullscreen()
                except ToolError as error:
                    full = {"fullscreen_error": str(error), "capabilities": {"fullscreen": False}}
        return {
            **result,
            **full,
            "voice_confirmed": True,
            "message": "Плеер подтвердил звуковую дорожку."
            + (
                " Полный экран требует повторного включения."
                if full.get("fullscreen_error")
                else ""
            ),
            "capabilities": {"audio_tracks": self.audio_tracks, **full.get("capabilities", {})},
        }

    def verified_state(self, nodes):
        state = player_state(nodes)
        if not state or state["duration"] < self.minimum:
            return None
        if self.episode["season"] and (
            not exact(nodes, rf"{self.episode['season']} сезон")
            or not exact(nodes, rf"{self.episode['episode']} серия")
        ):
            return None
        if self.voice and not exact(nodes, re.escape(self.voice)):
            return None
        if (
            self.engine == "venom_ax"
            and self.episode["season"]
            and state["duration"] - state["position"] <= 25
            and exact(nodes, "Следующая серия запустится через")
        ):
            for node in nodes:
                match = re.search(r"\((\d+) сезон\)\s*-\s*(\d+) серия", label(node))
                if (
                    match
                    and int(match[1]) == self.episode["season"]
                    and int(match[2]) == self.episode["episode"] + 1
                    and title_agrees(
                        self.report.get("heading", ""), {"title": label(node)[: match.start()]}
                    )
                ):
                    self.pending_next = (int(match[1]), int(match[2]), time.monotonic())
        return state

    def site_advance(self, nodes):
        """Recognize a reviewed player's own countdown followed by the adjacent episode."""
        pending = self.pending_next
        state = player_state(nodes)
        if not (
            pending
            and self.last_sample
            and time.monotonic() - pending[2] <= 150
            and state
            and state["duration"] >= self.minimum
            and state["position"] <= 120
            and exact(nodes, rf"{pending[0]} сезон")
            and exact(nodes, rf"{pending[1]} серия")
            and (not self.voice or exact(nodes, re.escape(self.voice)))
        ):
            return None
        before = self.next_clock
        self.next_clock = state
        result = {
            **self.last_sample,
            "observation_stale": True,
            "ended": False,
            "message": "Сайт выбрал следующую серию. Проверяем движение её таймера.",
        }
        if (
            before
            and before["duration"] == state["duration"]
            and 0 < state["position"] - before["position"] <= 65
        ):
            result["site_next_episode"] = {"season": pending[0], "episode": pending[1]}
        return result

    async def observe_playback(self, nodes, attempts):
        before = self.verified_state(nodes)
        for _ in range(attempts):
            await asyncio.sleep(1)
            nodes = await self.nodes()
            after = self.verified_state(nodes)
            if (
                before
                and after
                and after["paused"] is not True
                and after["duration"] == before["duration"]
                and 0 < after["position"] - before["position"] <= 4
            ):
                self.playing = True
                return {
                    **after,
                    "paused": False,
                    "before": before["position"],
                    "mode": "learned",
                    "content_verified": True,
                    "page_opened": True,
                    "playback_confirmed": True,
                    "message": "Safari подтвердил выбранный эпизод "
                    "и движение таймера полного видео.",
                }, nodes
            before = after
        return None, nodes

    async def confirm(self, nodes):
        # Choosing an episode can start the player itself. Let it settle before
        # clicking the play/pause surface, which would otherwise pause it.
        result, nodes = await self.observe_playback(nodes, 4)
        if result:
            return result
        for _ in range(2):
            # Some players expose a button's label only while it is hovered.
            # Identify that real control before pressing it; the middle of an
            # iframe can instead be a loading surface or an unrelated overlay.
            button = await self.action_button(nodes, PLAY)
            if button:
                await self.press(button, pointer=True)
            elif self.engine == "venom_ax":
                await self.key("play_pause")
            else:
                raise ToolError("В плеере нет однозначной кнопки воспроизведения.")
            result, nodes = await self.observe_playback(await self.nodes(), 15)
            if result:
                return result
        if not self.verified_state(nodes):
            raise ToolError(
                "Плеер не загрузил полный таймер выбранной серии после нажатия Play. "
                "Это может быть загрузка видео или недоступность потока."
            )
        raise ToolError("Таймер выбранной серии не пошёл после запуска плеера Safari.")

    async def open(self, target):
        recipe = SiteRecipe.model_validate(target["recipe"])
        self.episode = target["episode"]
        self.voice = recipe.voice
        runtime = (target["item"].get("metadata") or {}).get("runtime", 0)
        self.minimum = max(12 * 60 if self.episode["season"] else 45 * 60, runtime * 60 * 0.65)
        diagnostic = target.get("diagnostic")

        async def stage(name, status, message):
            if diagnostic:
                await diagnostic(name, status, message)

        try:
            async with asyncio.timeout(85):
                await stage("OPEN_SAFARI", "running", "Открываем точную страницу произведения.")
                await self.open_page(recipe, target["item"], target.get("frame_host", ""))
                await stage("DETECT_PLAYER", "success", f"Обнаружен плеер: {self.engine}.")
                if not target.get("autoplay", True):
                    raise ToolError("Автозапуск отключён в настройках медиатеки.")
                if self.engine == "html5":
                    self.html5 = HTML5Adapter(OwnedHTML5Bridge(self))
                    result = await self.html5.open(
                        {
                            **target,
                            "link": {"url": self.url, "voice": recipe.voice},
                            "episode": {**self.episode, "position": 0},
                            "auto_fullscreen": False,
                        }
                    )
                    if not result.get("playback_confirmed"):
                        raise ToolError(result.get("message", "HTML5-плеер не подтвердил запуск."))
                    self.capabilities = (await self.html5.sample()).get("capabilities", {})
                else:
                    await self.reveal(controls=True)
                    nodes = await self.prepare(recipe, self.episode)
                    await self.reveal()
                    await stage(
                        "PLAY", "running", "Проверяем запуск полного видео по движению времени."
                    )
                    result = await self.confirm(nodes)
                    await self.discover_audio_tracks(await self.nodes())
                    self.capabilities = {
                        "play": True,
                        "pause": self.engine == "venom_ax",
                        "position": True,
                        "seek": self.engine == "venom_ax",
                        "fullscreen_available": self.engine == "venom_ax",
                        "audio_tracks": self.audio_tracks,
                    }
                self.last_sample = result
                position = float(target.get("position", self.episode.get("position", 0)))
                resume = position > 2 or target.get("restart") is True or result["position"] > 10
                if resume:
                    await stage(
                        "RESTORE_POSITION", "running", f"Восстанавливаем позицию {int(position)} с."
                    )
                    try:
                        restored = await self.seek(position)
                        playing = await (
                            self.html5.control("play")
                            if self.html5
                            else self.confirm(await self.nodes())
                        )
                        result = {
                            **playing,
                            "resume_confirmed": True,
                            "requested_position": position,
                            "message": restored["message"] + " Воспроизведение подтверждено.",
                        }
                        await stage("RESTORE_POSITION", "success", restored["message"])
                    except ToolError as error:
                        with contextlib.suppress(ToolError):
                            actual = await self.sample()
                            result.update(
                                {
                                    key: actual[key]
                                    for key in ("position", "duration", "paused", "observed_at")
                                }
                            )
                        result.update(
                            resume_confirmed=False,
                            requested_position=position,
                            resume_error=str(error),
                            message=f"Видео запущено, но восстановление {int(position)} с "
                            f"не подтверждено. {error}",
                        )
                        await stage("RESTORE_POSITION", "limited", result["message"])
                else:
                    result["resume_confirmed"] = True
                if (
                    target.get("auto_fullscreen", True)
                    and result.get("resume_confirmed") is not False
                ):
                    await stage("FULLSCREEN", "running", "Включаем полный экран видеоплеера.")
                    try:
                        full = await self.fullscreen()
                        if full.get("fullscreen_confirmed") is not True:
                            raise ToolError("Плеер не подтвердил полноэкранное состояние видео.")
                        result.update(fullscreen_confirmed=full.get("fullscreen_confirmed") is True)
                        self.capabilities.update(full.get("capabilities", {}))
                        self.capabilities["fullscreen"] = result["fullscreen_confirmed"]
                        await stage(
                            "FULLSCREEN",
                            "success",
                            "Полноэкранное состояние видеоплеера подтверждено.",
                        )
                    except ToolError as error:
                        result.update(fullscreen_confirmed=False, fullscreen_error=str(error))
                        await stage("FULLSCREEN", "limited", str(error))
                result.update(capabilities=self.capabilities, engine=self.engine)
                if self.report.get("voice_note"):
                    result["message"] += " " + self.report["voice_note"]
            self.report.update(result)
            self.last_sample = result
            return result
        except BaseException:
            await self.detach()
            raise

    async def sample(self):
        if self.html5:
            return await self.html5.sample()
        try:
            nodes = await self.nodes()
        except ToolError as error:
            if self.last_sample and getattr(error, "code", "") == "cinema_page_unavailable":
                page = await self.page()
                if page.get("heading") == self.report.get("heading"):
                    return {**self.last_sample, "observation_stale": True}
            raise
        state = self.verified_state(nodes)
        if not state:
            advance = self.site_advance(nodes)
            if advance:
                return advance
        if not state and nodes:
            if (await self.ax.cinema("status")).get("frontmost"):
                await self.press(nodes[0], op="cinema_hover")
                await asyncio.sleep(0.25)
                nodes = await self.nodes()
                state = self.verified_state(nodes)
            elif self.last_sample and not player_state(nodes):
                return {**self.last_sample, "observation_stale": True, "ended": False}
        if not state:
            raise ToolError("Плеер или выбранная серия изменились.")
        if self.last_sample:
            if state["duration"] != self.last_sample["duration"]:
                raise ToolError("Длительность видео изменилась. Проверь источник заново.")
            if state["paused"] is None and state["position"] > self.last_sample["position"]:
                state["paused"] = False
        self.last_sample = state
        state["ended"] = state["position"] >= state["duration"] - 1
        page = await self.page()
        state["capabilities"] = {
            "fullscreen": bool(
                page.get("fullscreen") and page.get("fullscreen_host") == self.frame_host
            )
        }
        return state

    async def control(self, action, position=0):
        await self.activate()
        if self.html5 and action != "fullscreen":
            return await self.html5.control(action, position)
        if action == "seek":
            return await self.seek(position)
        if action == "fullscreen":
            if (await self.page()).get("fullscreen"):
                await self.page(exit_fullscreen=True)
                for _ in range(15):
                    await asyncio.sleep(0.2)
                    if not (await self.page()).get("fullscreen"):
                        return {
                            "fullscreen": False,
                            "fullscreen_confirmed": False,
                            "capabilities": {"fullscreen": False, "fullscreen_available": True},
                            "message": "Видеоплеер вышел из полного экрана; просмотр продолжается.",
                        }
                raise ToolError("Выход из полного экрана не подтверждён.")
            return await self.fullscreen()
        nodes = await self.nodes()
        if not self.verified_state(nodes):
            raise ToolError("Плеер или выбранная серия изменились.")
        if action == "play":
            return await self.confirm(nodes)
        if action == "pause":
            button = exact(nodes, PAUSE)
            if button:
                await self.press(button)
            elif self.engine == "venom_ax":
                await self.key("play_pause")
            else:
                raise ToolError("Плеер не показал проверенную кнопку паузы.")
            if button or self.engine == "venom_ax":
                before = await self.sample()
                await asyncio.sleep(1.5)
                first = await self.sample()
                await asyncio.sleep(1.2)
                after = await self.sample()
                if (
                    before["duration"] == after["duration"]
                    and first["position"] == after["position"]
                ):
                    self.playing = False
                    return {
                        **after,
                        "paused": True,
                        "message": "После команды паузы таймер остановился.",
                    }
                raise ToolError("Плеер не подтвердил паузу.")
        raise ToolError("Это действие выполни кнопкой самого плеера Safari.")

    async def detach(self):
        if self.session:
            with contextlib.suppress(ToolError, OSError, TimeoutError):
                await self.page(exit_fullscreen=True)
                for _ in range(10):
                    if not (await self.page()).get("fullscreen"):
                        break
                    await asyncio.sleep(0.1)
            if self.html5:
                with contextlib.suppress(ToolError, OSError, TimeoutError):
                    await self.html5.detach()
            with contextlib.suppress(ToolError, OSError, TimeoutError):
                await asyncio.shield(self.tabs.close_marked(self.session))
            self.session = None
