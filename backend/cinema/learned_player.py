"""Repeat explicit browser actions on a page supplied by the viewer."""

import asyncio
import contextlib
import ipaddress
import json
import re
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.cinema.browser_search import BrowserSearch, SearchSession
from backend.cinema.models import web_url
from backend.cinema.playback import playback_error
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
 const kinogo=location.hostname==='kinogo.online';
 const tabSelector=kinogo?'.js-player-tabs > li':'[role=tab],button';
 const tabs=[...document.querySelectorAll(tabSelector)].filter(visible);
 const label=e=>norm(e.getAttribute('aria-label')||e.textContent);
 const names=kinogo?tabs.map(label):tabs.map(label).filter(x=>/^(плеер|player)\s*\d*$/i.test(x));
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
 const active=tabs.find(e=>e.matches('.current,[aria-selected=true]'));
 return JSON.stringify({heading,names,frames,selected:active?label(active):request.player});
})"""
PLAY = r"(?:Смотреть|Воспроизвести|Play)(?:\s*\([^)]*\))?(?:\s*или клик в любом месте)?"
PAUSE = r"(?:Пауза|Pause)(?:\s*\([^)]*\))?"


class SiteRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    url: str
    player: str = Field(default="", max_length=80)
    voice: str = Field(default="", max_length=100)

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
    return None


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

    async def page(self, **values):
        script = PAGE + "(" + json.dumps({**values, "url": self.url}, ensure_ascii=False) + ")"
        raw = await self.tabs._owned(
            self.session,
            "return do JavaScript " + json.dumps(script, ensure_ascii=False) + " in ownedTab",
        )
        if raw == "unowned":
            raise ToolError("Safari закрыл или перенаправил учебную вкладку.")
        data = json.loads(raw)
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
        player = recipe.player or ("Плеер 1" if parsed.hostname == "kinogo.online" else "")
        for _ in range(25):
            data = await self.page(player=player, prepare=False)
            if data.get("heading"):
                break
            await asyncio.sleep(0.2)
        if not title_agrees(data.get("heading", ""), item):
            raise ToolError("Заголовок страницы не совпал с выбранным произведением.")
        self.report = {
            "page_opened": True,
            "heading": data["heading"],
            "players": data["names"],
            "player": player,
        }
        if not player:
            return
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
                self.report["frame_host"] = host
                return
        raise ToolError("Выбранная вкладка не показала один доступный iframe плеера.")

    async def activate(self):
        result = await self.tabs._owned(
            self.session,
            "set current tab of window id (ownerID as integer) to ownedTab\n"
            'activate\nreturn "sent"',
        )
        if result != "sent":
            raise ToolError("Учебная вкладка Safari закрыта или изменила адрес.")

    async def nodes(self):
        if not self.frame_host:
            return []
        return (await self.ax.cinema("cinema_snapshot", page=self.url, frame_host=self.frame_host))[
            "nodes"
        ]

    async def press(self, node):
        expected = {
            k: node[k] for k in ("role", "title", "description", "value", "help") if k in node
        }
        await self.ax.cinema(
            "cinema_press",
            page=self.url,
            frame_host=self.frame_host,
            path=node["path"],
            expected=expected,
        )

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
        raise ToolError(
            "Плеер не показал ожидаемые элементы. Проверь выбранную вкладку и сценарий."
        )

    async def select(self, nodes, menu, desired):
        control = exact(nodes, menu)
        if not control:
            raise ToolError(f"Плеер не показал однозначный элемент выбора «{desired}».")
        if label(control) == desired:
            return nodes
        await self.press(control)
        nodes = await self.wait_nodes(lambda tree: exact(tree, re.escape(desired)))
        await self.press(exact(nodes, re.escape(desired)))
        return await self.wait_nodes(
            lambda tree: any(
                node["path"] == control["path"] and label(node) == desired for node in tree
            )
        )

    async def prepare(self, recipe, episode):
        nodes = await self.wait_nodes(lambda tree: len(tree) > 3)
        if episode["season"]:
            nodes = await self.select(nodes, r"\d+ сезон", f"{episode['season']} сезон")
            nodes = await self.select(nodes, r"\d+ серия", f"{episode['episode']} серия")
        voice = voice_option(nodes)
        if recipe.voice and (not voice or label(voice) != recipe.voice):
            if not voice:
                raise ToolError("Плеер не показал выбор озвучки.")
            nodes = await self.select(nodes, re.escape(label(voice)), recipe.voice)
        elif voice:
            self.voice = label(voice)
        return nodes

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
        return state

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
            button = exact([n for n in nodes if n.get("role") == "AXButton"], PLAY)
            if not button and self.frame_host != "api.nextembed.ws":
                raise ToolError("В плеере нет однозначной кнопки воспроизведения.")
            await self.press(button or nodes[0])
            result, nodes = await self.observe_playback(nodes, 7)
            if result:
                return result
        raise ToolError("Таймер выбранной серии не пошёл после запуска плеера Safari.")

    async def open(self, target):
        recipe = SiteRecipe.model_validate(target["recipe"])
        self.episode = target["episode"]
        self.voice = recipe.voice
        runtime = (target["item"].get("metadata") or {}).get("runtime", 0)
        self.minimum = max(12 * 60 if self.episode["season"] else 45 * 60, runtime * 60 * 0.65)
        try:
            async with asyncio.timeout(45):
                await self.open_page(recipe, target["item"], target.get("frame_host", ""))
                nodes = await self.prepare(recipe, self.episode)
                if not target.get("autoplay", True):
                    raise ToolError("Автозапуск отключён в настройках медиатеки.")
                result = await self.confirm(nodes)
            self.report.update(result)
            return result
        except BaseException:
            await self.detach()
            raise

    async def sample(self):
        state = self.verified_state(await self.nodes())
        if not state:
            raise ToolError("Плеер или выбранная серия изменились.")
        return state

    async def control(self, action, position=0):
        await self.activate()
        nodes = await self.nodes()
        if not self.verified_state(nodes):
            raise ToolError("Плеер или выбранная серия изменились.")
        if action == "play":
            return await self.confirm(nodes)
        if action == "pause":
            button = exact(nodes, PAUSE)
            if button:
                await self.press(button)
                return {**await self.sample(), "message": "Плеер Safari поставлен на паузу."}
        raise ToolError("Это действие выполни кнопкой самого плеера Safari.")

    async def detach(self):
        if self.session:
            with contextlib.suppress(ToolError, OSError, TimeoutError):
                await asyncio.shield(self.tabs.close_marked(self.session))
            self.session = None
