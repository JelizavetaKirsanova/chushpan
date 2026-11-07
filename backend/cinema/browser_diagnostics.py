"""Opt-in Playwright checks in an isolated browser, never in the user's profile.

Only public page/player observations are retained. No cookies, request headers,
response bodies, HAR or tokens are exported. Ordinary playback never starts this browser.
"""

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from backend.cinema.evidence import public_url, safe_reason
from backend.cinema.internet import PageVerifier
from backend.cinema.models import web_url
from backend.cinema.playback import SourceValidator
from backend.cinema.public_search import key
from backend.cinema.sources import addresses
from backend.tools.registry import ToolError


def configure_browser_cache():
    # Playwright otherwise uses a writable .local-browsers folder inside a frozen
    # app. Use its standard macOS cache and keep the signed bundle immutable.
    if getattr(sys, "frozen", False) and sys.platform == "darwin":
        os.environ.setdefault(
            "PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / "Library/Caches/ms-playwright")
        )


# Semantic roles/labels survive class-name changes. Site profiles only add aliases;
# none accepts scripts, executable selectors or credentials from source settings.
PLAYER_PROFILES = {
    "archive.org": r"play|воспроизвести|start",
    "kion.ru": r"смотреть|продолжить просмотр|play",
    "default": r"play|воспроизвести|смотреть (фильм|серию)|начать просмотр",
}
INSPECT = """() => {
 const roots=[document];for(let i=0;i<roots.length&&i<60;i++)
   for(const e of roots[i].querySelectorAll('*'))if(e.shadowRoot)roots.push(e.shadowRoot);
 const videos=roots.flatMap(r=>[...r.querySelectorAll('video')]).map(v=>({
   position:v.currentTime,duration:Number.isFinite(v.duration)?v.duration:0,
   paused:v.paused,seeking:v.seeking,ready:v.readyState,error:v.error?.code||null,
   protected:!!v.mediaKeys,media_url:v.currentSrc||v.src,
   video_title:v.title||v.getAttribute('aria-label')||'',
   sources:[...v.querySelectorAll('source')].map(s=>({type:s.type,url:s.src})).slice(0,8)
 }));
 const structured=[...document.querySelectorAll('script[type="application/ld+json"]')]
   .slice(0,12).flatMap(s=>{try{return [JSON.parse(s.textContent.slice(0,30000))];}
     catch{return [];}});
 const controls=[...document.querySelectorAll('button,[role=button],select')]
   .filter(e=>e.getBoundingClientRect().width>0).slice(0,100)
   .map(e=>({role:e.getAttribute('role')||e.tagName.toLowerCase(),
     label:(e.getAttribute('aria-label')||e.title||e.textContent||'').trim().slice(0,120)}));
 return {title:document.title,heading:document.querySelector('h1')?.textContent||'',
   url:location.href,structured,videos,controls,
   frames:[...document.querySelectorAll('iframe')].map(f=>f.src).slice(0,20),
   restricted:/verify you are human|checking your browser|just a moment|captcha|доступ ограничен/i
     .test((document.body?.innerText||'').slice(0,600)),
   geo_restricted:new RegExp('not available in your (country|region)|'+
     'недоступ[а-я]* в (ваш|текущ)[а-я]* (стран|регион)','i')
     .test((document.body?.innerText||'').slice(0,3000)),
   login:[...document.querySelectorAll('input[type=password]')].some(e=>
     e.getBoundingClientRect().width>0&&getComputedStyle(e).visibility!=='hidden')};
}"""


async def page_identity(page, query):
    try:
        return PageVerifier.inspect(await page.content(), page.url, query), ""
    except ToolError as exc:
        return None, str(exc)


async def attach_public_routes(context):
    cache = {}

    async def public_only(route):
        target = urlsplit(route.request.url)
        if target.scheme in {"data", "blob"}:
            return await route.continue_()
        try:
            if target.scheme not in {"http", "https"} or target.username or target.password:
                raise ValueError
            host = target.hostname
            if host not in cache:
                resolved = await addresses(host, target.port or 443)
                cache[host] = bool(resolved) and all(
                    ipaddress.ip_address(a).is_global for a in resolved
                )
            if not cache[host]:
                raise ValueError
        except (OSError, ValueError):
            return await route.abort("blockedbyclient")
        await route.continue_()

    await context.route("**/*", public_only)


class BrowserDiagnostics:
    def __init__(self, root, changed):
        self.root = Path(root) / "cinema-browser-diagnostics"
        self.changed = changed
        self.task = None
        self.resolved_url = ""
        self.report = {"running": False, "status": "not_checked"}
        try:
            self.report = json.loads((self.root / "latest.json").read_text())
            self.report["running"] = False
        except (OSError, ValueError):
            pass

    async def publish(self):
        await self.changed(browser_diagnostics=self.report)

    def stage(self, name, status, message, started):
        self.report.setdefault("stages", []).append(
            {
                "name": name,
                "status": status,
                "message": safe_reason(message),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        )

    async def cancel(self):
        task = self.task
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run(self, source, query, region, *, url="", proof=None, play=False):
        if self.task:
            raise ToolError("Проверка Playwright уже выполняется; можно отменить её.")
        self.task = asyncio.current_task()
        started = time.monotonic()
        identifier = uuid4().hex
        previous = self.report.get("structure_hash")
        self.report = {
            "id": identifier,
            "running": True,
            "status": "running",
            "checked_at": datetime.now(UTC).isoformat(),
            "source": source["name"],
            "source_id": source["id"],
            "title": query.get("title", ""),
            "year": query.get("year"),
            "season": query.get("season"),
            "episode": query.get("episode"),
            "region": region,
            "region_access": "unknown",
            "network_location_verified": False,
            "playback_confirmed": False,
            "content_verified": False,
            "stages": [],
            "network_errors": [],
            "javascript_errors": [],
            "frames": [],
        }
        folder = self.root / identifier
        folder.mkdir(parents=True, mode=0o700)
        browser = context = page = runtime = driver = None
        await self.publish()
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import async_playwright

            configure_browser_cache()
            driver = async_playwright()
            runtime = await driver.start()
            async with asyncio.timeout(75):
                try:
                    browser = await runtime.chromium.launch(headless=True)
                except PlaywrightError as exc:
                    if "Executable doesn't exist" not in str(exc):
                        raise
                    command = (
                        "/Applications/Чушпан.app/Contents/MacOS/Chushpan --install-cinema-browser"
                        if getattr(sys, "frozen", False)
                        else "python -m playwright install --only-shell chromium"
                    )
                    raise ToolError(
                        "Для диагностики нужен Chromium этой версии Playwright. "
                        f"Установи его один раз в Терминале: {command}"
                    ) from None
                context = await browser.new_context(
                    locale="ru-RU",
                    accept_downloads=False,
                    service_workers="block",
                    viewport={"width": 1280, "height": 800},
                )
                await attach_public_routes(context)
                page = await context.new_page()
                page.set_default_timeout(4000)
                # No page is allowed to open a payment/login/pop-up workflow for us.
                context.on(
                    "page",
                    lambda extra: asyncio.create_task(extra.close()) if extra != page else None,
                )
                page.on(
                    "pageerror",
                    lambda error: (
                        self.report["javascript_errors"].append(
                            safe_reason(str(error).splitlines()[0])
                        )
                        if len(self.report["javascript_errors"]) < 30
                        else None
                    ),
                )
                page.on(
                    "requestfailed",
                    lambda request: self.network_error(request.url, request.failure),
                )
                page.on(
                    "response",
                    lambda response: (
                        self.network_error(response.url, "HTTP " + str(response.status))
                        if response.status >= 400
                        else None
                    ),
                )
                target = web_url(url or source["base_url"])
                response = await page.goto(target, wait_until="domcontentloaded", timeout=22000)
                self.report.update(
                    url=public_url(page.url), http_status=response.status if response else None
                )
                self.stage("page", "confirmed", "Страница загружена", started)
                if response and response.status >= 400:
                    self.report["region_access"] = (
                        "blocked" if response.status == 451 else "unknown"
                    )
                    raise ToolError(
                        f"HTTP {response.status}: доступ ограничен; защита не обходилась."
                    )
                await page.wait_for_timeout(1200)
                main = await page.evaluate(INSPECT)
                if main["restricted"] or main["login"]:
                    raise ToolError(
                        "Страница требует CAPTCHA, проверки браузера или входа. "
                        "Автоматизация остановлена."
                    )
                if main["geo_restricted"]:
                    self.report["region_access"] = "blocked"
                    raise ToolError("Страница сообщает об ограничении для текущей страны/региона.")
                if not url and query.get("title"):
                    await self.find_title(page, query)
                    main = await page.evaluate(INSPECT)
                self.report["url"] = public_url(page.url)
                self.resolved_url = web_url(page.url)
                identity, identity_error = await page_identity(page, {**query, "region": region})
                if play and identity and identity.get("scope") == "series":
                    await self.select_coordinates(page, query)
                    main = await page.evaluate(INSPECT)
                    identity, identity_error = await page_identity(
                        page, {**query, "region": region}
                    )
                if proof and proof.get("content_verified"):
                    identity = {
                        "scope": proof.get("scope"),
                        "region_access": proof.get("region_access", "unknown"),
                    }
                self.report["identity_status"] = (
                    identity.get("identity_status", "confirmed") if identity else "unconfirmed"
                )
                self.report["episode_status"] = (
                    "confirmed"
                    if identity
                    and self.report["identity_status"] == "confirmed"
                    and identity.get("scope") in {"episode", "movie"}
                    else (identity or {}).get("episode_status", "unconfirmed")
                )
                self.report["identity_signals"] = (identity or {}).get("identity_signals", [])
                if identity:
                    self.report["region_access"] = identity.get("region_access", "unknown")
                self.stage(
                    "identity",
                    self.report["identity_status"],
                    identity.get("reason", "Названия и координаты сверены с источником.")
                    if identity
                    else identity_error,
                    started,
                )
                if self.report["region_access"] == "blocked":
                    raise ToolError("Источник исключает EE из опубликованной доступности.")
                frame_data = await self.inspect_frames(page)
                if play and identity:
                    for frame, _ in frame_data:
                        with contextlib.suppress(Exception):
                            await frame.locator("video").evaluate_all(
                                "vs=>vs.forEach(v=>{if(v.readyState===0){v.preload='metadata';v.load();}})"
                            )
                    for _ in range(15):
                        if any(v["duration"] > 0 for _, d in frame_data for v in d["videos"]):
                            break
                        await page.wait_for_timeout(700)
                        frame_data = await self.inspect_frames(page)
                self.report["frames"] = [
                    {
                        "url": public_url(d["url"]),
                        "videos": len(d["videos"]),
                        "controls": d["controls"],
                        "iframe_count": len(d["frames"]),
                    }
                    for _, d in frame_data
                ]
                shape = json.dumps(
                    [(d["controls"], len(d["videos"]), len(d["frames"])) for _, d in frame_data]
                )
                self.report["structure_hash"] = hashlib.sha256(shape.encode()).hexdigest()
                self.report["structure_changed"] = bool(
                    previous and previous != self.report["structure_hash"]
                )
                # Re-discover on each run; never reuse stale class names or saved DOM handles.
                candidates = [
                    (f, d, n, v)
                    for f, d in frame_data
                    for n, v in enumerate(d["videos"])
                    if not v["protected"]
                ]
                if any(v["protected"] for _, d in frame_data for v in d["videos"]):
                    raise ToolError(
                        "Плеер использует DRM (MediaKeys). Нужен официальный плеер; обход запрещён."
                    )
                self.report["player_found"] = bool(candidates)
                self.stage(
                    "player",
                    "confirmed" if candidates else "unconfirmed",
                    f"video: {len(candidates)}; iframe: {len(page.frames) - 1}",
                    started,
                )
                if play and identity and not candidates:
                    await self.semantic_play(page, source, main, query)
                    await page.wait_for_timeout(1500)
                    frame_data = await self.inspect_frames(page)
                    candidates = [
                        (f, d, n, v)
                        for f, d in frame_data
                        for n, v in enumerate(d["videos"])
                        if not v["protected"]
                    ]
                if not identity:
                    raise ToolError(
                        "Нужное произведение, сезон и серия не подтверждены на этой странице. "
                        "Плеер проверен отдельно; это не подтверждение отсутствия фильма."
                    )
                if len(candidates) != 1:
                    raise ToolError(
                        "Не найден единственный доступный HTML5-плеер. Возможны iframe, "
                        "динамический плеер, DRM или несколько видео."
                    )
                frame, data, index, sample = candidates[0]
                evidence = {
                    **sample,
                    "live": True,
                    "page_url": page.url,
                    "frame_url": data["url"],
                    "page_title": main["title"],
                    "structured": main["structured"] + data["structured"],
                }
                verdict = SourceValidator.player(
                    query, evidence, (proof or {}).get("media_url", ""), proof
                )
                self.report.update(
                    content_verified=verdict["content_verified"],
                    video_type=verdict["video_type"],
                    content_reason=verdict["classification_reason"],
                    player_found=True,
                    duration=sample["duration"],
                    media_url=public_url(sample["media_url"]),
                )
                if not play:
                    self.report.update(
                        status="inspected",
                        message="Страница и плеер исследованы без запуска видео.",
                    )
                elif not identity or not verdict["content_verified"]:
                    raise ToolError(
                        verdict["classification_reason"]
                        + " Страница доступна для ручного просмотра; это не отсутствие фильма."
                    )
                else:
                    self.stage(
                        "play",
                        "running",
                        "Нажимаем доступный Play или вызываем HTMLMediaElement.play",
                        started,
                    )
                    await self.start_video(frame, index)
                    first = (
                        await frame.locator("video")
                        .nth(index)
                        .evaluate("v=>({time:v.currentTime,src:v.currentSrc,duration:v.duration})")
                    )
                    await page.wait_for_timeout(1800)
                    second = (
                        await frame.locator("video")
                        .nth(index)
                        .evaluate(
                            "v=>({time:v.currentTime,paused:v.paused,src:v.currentSrc,duration:v.duration})"
                        )
                    )
                    confirmed = (
                        second["time"] > first["time"] + 0.5
                        and not second["paused"]
                        and first["src"] == second["src"]
                        and first["duration"] == second["duration"]
                    )
                    self.report.update(
                        currentTime_before=first["time"],
                        currentTime_after=second["time"],
                        playback_confirmed=confirmed,
                    )
                    if not confirmed:
                        raise ToolError(
                            "После Play движение currentTime не подтвердилось: "
                            "autoplay, сеть или плеер требуют действия."
                        )
                    fullscreen = (
                        await frame.locator("video")
                        .nth(index)
                        .evaluate(
                            """async v=>{try{await v.requestFullscreen();
                            return !!document.fullscreenElement;}catch{return false;}}"""
                        )
                    )
                    self.report.update(
                        status="confirmed",
                        fullscreen=fullscreen,
                        message="Полное видео подтверждено; currentTime увеличивается.",
                    )
                    self.stage(
                        "play",
                        "confirmed",
                        f"currentTime {first['time']:.3f} → {second['time']:.3f}",
                        started,
                    )
                await page.screenshot(path=str(folder / "page.png"), timeout=5000)
                await context.close()
                context = None
                await browser.close()
                browser = None
        except asyncio.CancelledError:
            self.report.update(
                status="cancelled", message="Проверка отменена; прогресс медиатеки не менялся."
            )
        except Exception as exc:
            self.report.update(
                status="limited",
                message=safe_reason(exc)
                if isinstance(exc, ToolError)
                else "Playwright: "
                + type(exc).__name__
                + ". "
                + safe_reason(str(exc).splitlines()[0]),
            )
            self.stage("result", "limited", self.report["message"], started)
            if page:
                with contextlib.suppress(Exception):
                    await page.screenshot(path=str(folder / "page.png"), timeout=3000)
        finally:
            if context:
                with contextlib.suppress(Exception):
                    await context.close()
            if browser:
                with contextlib.suppress(Exception):
                    await browser.close()
            if runtime:
                with contextlib.suppress(Exception):
                    await runtime.stop()
            elif driver:
                with contextlib.suppress(Exception):
                    await driver.__aexit__(None, None, None)
            self.report.update(running=False, elapsed_ms=round((time.monotonic() - started) * 1000))
            if (folder / "page.png").exists():
                self.report["screenshot_path"] = str(folder / "page.png")
            self.report["report_path"] = str(folder / "report.json")
            encoded = json.dumps(self.report, ensure_ascii=False, indent=2, allow_nan=False)
            (folder / "report.json").write_text(encoded)
            (self.root / "latest.json").write_text(encoded)
            self.task = None
            await self.publish()
        return self.report

    def network_error(self, url, error):
        errors = self.report["network_errors"]
        if len(errors) < 50:
            errors.append({"url": public_url(url), "error": safe_reason(error)})

    async def inspect_frames(self, page):
        rows = []
        for frame in page.frames[:16]:
            with contextlib.suppress(Exception):
                rows.append((frame, await frame.evaluate(INSPECT)))
        return rows

    @staticmethod
    async def find_title(page, query):
        metadata = query.get("metadata") or {}
        names = [
            query["title"],
            metadata.get("original_title", ""),
            *metadata.get("alternative_titles", []),
        ]
        fields = page.locator(
            "input[type=search],input[name=q],input[name=story],input[name=search],input[role=searchbox]"
        )
        for field in await fields.all():
            if await field.is_visible():
                await field.fill(query["title"])
                await field.press("Enter")
                await page.wait_for_timeout(1500)
                break
        # Some sites open a search form after Enter and require its labelled submit.
        search = page.get_by_role(
            "button", name=re.compile(r"^(начать поиск|найти|поиск|search)$", re.I)
        )
        visible = [button for button in await search.all() if await button.is_visible()]
        if len(visible) == 1:
            await visible[0].click()
            await page.wait_for_timeout(2500)
        links = await page.locator("a[href]").evaluate_all(
            """es=>es.slice(0,1000).map(e=>({url:e.href,text:e.textContent,title:e.title,
            context:(e.closest('article')||e.parentElement?.parentElement)?.innerText?.slice(0,1500)}))"""
        )
        for link in links:
            text = key((link.get("text") or "") + " " + (link.get("title") or ""))
            target = urljoin(page.url, link["url"])
            if (
                urlsplit(target).hostname == urlsplit(page.url).hostname
                and urlsplit(target).path not in {"", "/"}
                and any(key(n) and key(n) in text for n in names)
                and (not query.get("year") or str(query["year"]) in (link.get("context") or text))
                and not re.search(r"трейлер|trailer|тизер|teaser", text)
            ):
                response = await page.goto(
                    web_url(target), wait_until="domcontentloaded", timeout=15000
                )
                if response and response.status >= 400:
                    raise ToolError(f"Страница произведения: HTTP {response.status}.")
                return

    async def select_coordinates(self, page, query):
        for label, coordinate in [
            (r"сезон|season", "season"),
            (r"серия|эпизод|episode", "episode"),
        ]:
            fields = page.get_by_label(re.compile(label, re.I))
            visible = [
                field
                for field in await fields.all()
                if await field.is_visible() and await field.evaluate("e=>e.tagName==='SELECT'")
            ]
            if len(visible) != 1:
                continue
            options = (
                await visible[0]
                .locator("option")
                .evaluate_all("es=>es.map(e=>({value:e.value,label:e.textContent.trim()}))")
            )
            pattern = r"^(?:сезон|серия|эпизод|season|episode)?\s*" + str(query[coordinate]) + r"$"
            matches = [option for option in options if re.fullmatch(pattern, option["label"], re.I)]
            if len(matches) == 1:
                await visible[0].select_option(value=matches[0]["value"])
                await page.wait_for_timeout(500)

    async def semantic_play(self, page, source, main, query):
        # A poster click can load a trailer. Only a page that already binds full
        # media may be activated automatically, then the active URL is rechecked.
        from backend.cinema.classification import classify

        proof = classify(
            query,
            {"page_url": page.url, "page_title": main["title"], "structured": main["structured"]},
        )
        if not proof["content_verified"]:
            return
        pattern = PLAYER_PROFILES.get(
            urlsplit(source["base_url"]).hostname, PLAYER_PROFILES["default"]
        )
        for frame in page.frames[:16]:
            buttons = frame.get_by_role("button", name=re.compile(pattern, re.I))
            visible = [b for b in await buttons.all() if await b.is_visible()]
            if len(visible) == 1:
                await visible[0].click(timeout=3000)
                return

    async def start_video(self, frame, index):
        video = frame.locator("video").nth(index)
        await video.evaluate("v=>{v.muted=true;}")
        buttons = frame.get_by_role(
            "button", name=re.compile(r"^(play|воспроизвести|начать просмотр)$", re.I)
        )
        if await buttons.count() == 1 and await buttons.first.is_visible():
            await buttons.first.click()
        else:
            await video.evaluate("async v=>{await v.play();}")
        await video.evaluate(
            """v=>new Promise((resolve,reject)=>{
            if(v.readyState>=2&&!v.paused)return resolve();
            const t=setTimeout(()=>reject(Error('player_loading')),8000);
            v.addEventListener('playing',()=>{clearTimeout(t);resolve();},{once:true});})"""
        )
