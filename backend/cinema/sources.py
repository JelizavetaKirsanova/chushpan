"""Bounded public API search and honest manual fallbacks for user-owned sources.

HTTP probes never use browser credentials. DNS is resolved and pinned to a public
IP for each request, including redirects, while preserving TLS SNI verification.
"""

import asyncio
import ipaddress
import math
import re
import socket
import time
from collections import OrderedDict
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urljoin, urlsplit
from uuid import uuid4

import httpx

from backend.cinema.classification import FULL_TYPES, PROMO_TYPES, classify
from backend.cinema.models import Source, web_url
from backend.cinema.personal import normalized
from backend.cinema.public_search import PublicCatalogue
from backend.tools.registry import ToolError


async def addresses(host, port):
    records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(row[4][0] for row in records))


class PublicHTTP:
    def __init__(self, *, transport=None, resolver=addresses):
        self.transport, self.resolver = transport, resolver
        self.semaphore = asyncio.Semaphore(3)

    async def request(self, url, *, method="GET", params=None, max_bytes=2_000_000, policy=None):
        async with self.semaphore, asyncio.timeout(30):
            for _ in range(4):
                url = web_url(url)
                if policy:
                    await policy(url)
                parsed = urlsplit(url)
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                if port not in {80, 443}:
                    raise ToolError(
                        "Проверка поддерживает публичные HTTP(S) сайты на стандартных портах."
                    )
                resolved = await self.resolver(parsed.hostname, port)
                if not resolved or any(not ipaddress.ip_address(a).is_global for a in resolved):
                    raise ToolError("Фоновая проверка локальных и частных адресов запрещена.")
                target = httpx.URL(url).copy_with(host=resolved[0])
                async with httpx.AsyncClient(
                    transport=self.transport, timeout=6, trust_env=False, follow_redirects=False
                ) as client:
                    async with client.stream(
                        method,
                        target,
                        params=params,
                        headers={
                            "Host": parsed.netloc,
                            "User-Agent": "Chushpan-Cinema/1.0",
                            "Accept": "application/json, */*;q=0.5",
                        },
                        extensions={"sni_hostname": parsed.hostname},
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                break
                            url, params = urljoin(url, location), None
                            continue
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > min(max_bytes, 16_000_000):
                                raise ToolError("Ответ источника слишком большой.")
                        # aiter_bytes already decoded gzip/br. Preserve semantic headers
                        # without asking the reconstructed response to decode a second time.
                        headers = response.headers.copy()
                        for header in ("content-encoding", "content-length", "transfer-encoding"):
                            headers.pop(header, None)
                        return httpx.Response(
                            response.status_code,
                            headers=headers,
                            content=bytes(body),
                            request=httpx.Request(method, url, params=params),
                            extensions={"final_url": url},
                        )
            raise ToolError("Слишком много перенаправлений источника.")


def capabilities(source):
    source = Source.model_validate({k: v for k, v in source.items() if k != "health"})
    return {
        "search": source.search_mode in {"archive", "sitemap"}
        or urlsplit(source.base_url).hostname
        in {"filmix.gg", "kinogo.is", "hdrezka.sh", "kion.ru"},
        "search_page": bool(source.search_url),
        "resolve": source.search_mode == "archive" or source.adapter in {"local", "direct"},
        "episodes": False,
        "embedded": source.adapter == "direct" and source.embed_allowed,
        "progress": source.adapter in {"html5", "direct"},
        "mode": "Публичная карта сайта · доступ проверяется"
        if source.search_mode == "sitemap"
        or urlsplit(source.base_url).hostname in {"filmix.gg", "kinogo.is", "hdrezka.sh", "kion.ru"}
        else "Официальный API Internet Archive"
        if source.search_mode == "archive"
        else "Разрешённое прямое видео"
        if source.adapter == "direct"
        else "HTML5 через Safari"
        if source.adapter == "html5"
        else "Локальный файл"
        if source.adapter == "local"
        else "Ручной выбор на сайте",
    }


class SourceAdapter(Protocol):
    async def search(self, query: dict) -> list[dict]: ...
    async def resolve(self, content: dict) -> dict: ...
    async def getEpisodes(self, series: dict) -> list[dict]: ...
    async def open(self, content: dict) -> dict: ...
    def getPlaybackCapabilities(self) -> dict: ...
    async def checkAvailability(self, url: str = "") -> dict: ...


class WebsiteAdapter:
    def __init__(self, source, http, opener=None):
        self.source, self.http, self.opener = source, http, opener

    def getPlaybackCapabilities(self):
        return capabilities(self.source)

    async def checkAvailability(self, url=""):
        if self.source["adapter"] == "local":
            exists = bool(url and Path(url).is_file())
            return {
                "availability": "available" if exists else "unknown",
                "reason": "Файл найден." if exists else "Нужен путь к конкретному файлу.",
            }
        try:
            response = await self.http.request(url or self.source["base_url"], method="HEAD")
            final = getattr(response, "extensions", {}).get("final_url")
            if url and final and final.rstrip("/") != url.rstrip("/"):
                return {
                    "availability": "unknown",
                    "reason": (
                        "Сохранённый адрес перенаправляет на другую страницу. "
                        "Требуется новый поиск; открытие отменено."
                    ),
                }
            status = response.status_code
            available = 200 <= status < 300
            return {
                "availability": "available"
                if available
                else "restricted"
                if status in {401, 403, 429}
                else "unknown"
                if status == 405
                else "unavailable",
                "reason": "Страница отвечает; доступ к видео ещё не подтверждён."
                if available
                else "Нужен ручной выбор: сайт ограничил проверку."
                if status in {401, 403, 405, 429}
                else "Страница недоступна.",
                "content_type": response.headers.get("content-type", "").split(";")[0],
            }
        except (ToolError, httpx.HTTPError, OSError, TimeoutError, ValueError):
            return {
                "availability": "unknown",
                "reason": "Проверить адрес не удалось; можно выбрать вручную.",
            }

    async def search(self, query):
        template = self.source.get("search_url")
        term = " ".join(str(v) for v in (query["title"], query.get("year")) if v)
        if query["kind"] == "series" and query.get("season") and query.get("episode"):
            term += f" S{query['season']:02d}E{query['episode']:02d}"
        return [
            {
                "url": template.replace("{query}", quote(term, safe=""))
                if template
                else self.source["base_url"],
                "kind": "search" if template else "home",
                "match": 0.0,
                "availability": "unknown",
                "reason": "Автопоиск не поддерживается. Выбери результат на сайте вручную.",
                "embedded": False,
                "language": self.source.get("language", ""),
                "access_type": self.source.get("access_type", "unknown"),
            }
        ]

    async def resolve(self, content):
        query = content.get("_expected", {})
        content = {k: v for k, v in content.items() if k != "_expected"}
        content.update(classify(query, {"page_url": content["url"]}))
        if self.source["adapter"] != "local" and urlsplit(content["url"]).path in {"", "/"}:
            return {
                **content,
                "availability": "unknown",
                "embedded": False,
                "match": 0.0,
                "reason": "Сохранена главная страница. Нужна конкретная "
                "страница фильма или сериала.",
            }
        check = await self.checkAvailability(content["url"])
        if (
            query
            and self.source["adapter"] != "local"
            and check["availability"] == "available"
            and not check.get("content_type", "").startswith("video/")
        ):
            from backend.cinema.internet import PageVerifier

            try:
                verified = await PageVerifier(self.http).verify(content["url"], query)
                content.update(verified)
            except ToolError as exc:
                content.update(
                    content_verified=False, video_type="unknown", classification_reason=str(exc)
                )
        compatible = (
            self.source["adapter"] == "direct"
            and self.source.get("embed_allowed")
            and check["availability"] == "available"
            and check.get("content_type") in {"video/mp4", "video/webm", "video/x-m4v"}
        )
        return {**content, **check, "embedded": bool(compatible)}

    async def getEpisodes(self, series):
        return []  # Never fabricate episode URLs on an arbitrary website.

    async def open(self, content):
        if self.opener is None:
            raise ToolError("Открытие сайта не настроено.")
        await self.opener(content["url"])
        return {
            "page_opened": True,
            "playback_confirmed": False,
            "message": "Открыта страница источника. Запуск видео не подтверждён.",
        }


class ArchiveAdapter(WebsiteAdapter):
    @staticmethod
    def title_key(value, year):
        title = normalized(str(value))
        suffix = " " + str(year)
        return title[: -len(suffix)] if year and title.endswith(suffix) else title

    async def search(self, query):
        if query["kind"] != "movie":
            return await super().search(query)
        meta = query.get("metadata", {})
        titles = list(
            dict.fromkeys(
                [
                    query["title"],
                    meta.get("original_title") or query["title"],
                    *meta.get("alternative_titles", []),
                ]
            )
        )[:100]
        title_query = " OR ".join(f'title:"{normalized(t)}"' for t in titles)
        year = query.get("year")
        year_query = (
            (f" AND (year:{year} OR date:[{year}-01-01T00:00:00Z TO {year}-12-31T23:59:59Z])")
            if year
            else ""
        )
        params = {
            "q": f"mediatype:movies AND ({title_query}){year_query}",
            "output": "json",
            "rows": 12,
            "fl[]": ["identifier", "title", "year", "date", "language", "mediatype"],
            "sort[]": "downloads desc",
        }
        response = await self.http.request("https://archive.org/advancedsearch.php", params=params)
        if response.status_code != 200:
            return await super().search(query)
        try:
            docs = response.json().get("response", {}).get("docs", [])
        except ValueError:
            return await super().search(query)
        result = []
        names = {self.title_key(t, query.get("year")) for t in titles}
        for row in docs[:12]:
            identifier = str(row.get("identifier", ""))
            year = str(row.get("year") or row.get("date", ""))[:4]
            if (
                not re.fullmatch(r"[\w.-]{1,200}", identifier)
                or self.title_key(row.get("title", ""), query.get("year")) not in names
                or not query.get("year")
                or year != str(query["year"])
            ):
                continue
            response = await self.http.request(f"https://archive.org/metadata/{identifier}")
            if response.status_code != 200:
                continue
            data = response.json()
            meta = data.get("metadata", {})
            if (
                data.get("is_dark")
                or str(meta.get("access-restricted-item", "")).casefold() == "true"
                or meta.get("mediatype") != "movies"
                or self.title_key(meta.get("title", ""), query.get("year")) not in names
                or str(meta.get("year") or meta.get("date", ""))[:4] != str(query["year"])
            ):
                continue
            result.append(
                {
                    "url": f"https://archive.org/details/{identifier}",
                    "kind": "page",
                    "match": 1.0,
                    "availability": "available",
                    "embedded": False,
                    "language": str(meta.get("language", ""))[:40],
                    "access_type": self.source.get("access_type", "unknown"),
                    "reason": "Официальный API: совпали название, год и тип. "
                    "Доступность воспроизведения и условия доступа проверь на странице.",
                }
            )
            if len(result) >= 3:
                break
        return result or await super().search(query)


class SitemapAdapter(WebsiteAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.catalogue = PublicCatalogue(self.source, self.http)

    async def resolve(self, content):
        if content.get("_expected"):
            return await super().resolve(content)
        if urlsplit(content["url"]).path in {"", "/"}:
            return await super().resolve(content)
        try:
            await self.catalogue.ensure_rules()
            response = await self.catalogue.request(content["url"], method="HEAD")
            final = getattr(response, "extensions", {}).get("final_url", content["url"])
            if final.rstrip("/") != content["url"].rstrip("/"):
                raise ToolError(
                    "Сохранённая страница перенаправляет на другой адрес. Повтори поиск."
                )
            return {
                **content,
                "availability": "available",
                "embedded": False,
                "reason": "Сохранённая конкретная страница доступна. Запуск видео не подтверждён.",
            }
        except ToolError as exc:
            return {**content, "availability": "unknown", "embedded": False, "reason": str(exc)}

    async def search(self, query):
        rows = await self.catalogue.search(query)
        if rows:
            return rows
        return [
            {
                "kind": "error",
                "url": "",
                "match": 0.0,
                "availability": "unknown",
                "embedded": False,
                "reason": self.catalogue.last_reason + " Автоматическое открытие отменено.",
            }
        ]


class SourceManager:
    def __init__(self, db, *, http=None, opener=None, clock=time.monotonic):
        self.db, self.http, self.opener, self.clock = db, http or PublicHTTP(), opener, clock
        self.adapters = {
            "manual": WebsiteAdapter,
            "archive": ArchiveAdapter,
            "sitemap": SitemapAdapter,
        }
        self.adapter_cache = OrderedDict()
        self.cache = OrderedDict()
        self.results = OrderedDict()
        self.pending = {}
        self.generation = 0
        self.web = None

    def adapter(self, source):
        mode = source.get("search_mode", "manual")
        if mode == "manual" and urlsplit(source.get("base_url", "")).hostname in {
            "filmix.gg",
            "kinogo.is",
            "hdrezka.sh",
            "kion.ru",
        }:
            mode = "sitemap"
        key = (
            str({k: v for k, v in source.items() if k not in {"health", "capabilities"}}),
            id(self.http),
        )
        if key not in self.adapter_cache:
            self.adapter_cache[key] = self.adapters[mode](source, self.http, self.opener)
            while len(self.adapter_cache) > 32:
                self.adapter_cache.popitem(last=False)
        return self.adapter_cache[key]

    def invalidate(self):
        self.generation += 1
        self.cache.clear()
        self.results.clear()

    async def check(self, source_id):
        source = (await self.db("source", source_id)).model_dump()
        result = await self.adapter(source).checkAvailability()
        await self.db("source_health", source_id, result["availability"], result["reason"])
        self.cache.clear()
        return result

    def result(self, identifier):
        result = self.results.get(identifier)
        if not result or result[0] <= self.clock():
            raise ToolError("Результат поиска устарел. Найди источник заново.")
        return result[1]

    async def search(
        self, title_id, episode_id=None, *, alternative=False, refresh=False, saved_only=False
    ):
        item = await self.db("detail", title_id)
        ep = next(
            (e for e in item["episodes"] if e["id"] == episode_id),
            item["current"] if episode_id is None else None,
        )
        if not ep:
            raise ValueError("Серия не найдена.")
        prefs = await self.db("preferences")
        sources = [s for s in await self.db("sources") if s["enabled"]]
        if saved_only:
            sources = [
                s for s in sources if any(link["source_id"] == s["id"] for link in ep["links"])
            ]
        generation = self.generation
        key = (generation, title_id, ep["id"], alternative, saved_only)
        cached = self.cache.get(key)
        if not refresh and cached and cached[0] > self.clock():
            return cached[1]
        if key in self.pending:
            return await asyncio.shield(self.pending[key])
        task = asyncio.create_task(
            self._search(item, ep, sources, prefs, alternative, generation, saved_only)
        )
        self.pending[key] = task
        try:
            result = await task
            self.cache[key] = (self.clock() + 300, result)
            while len(self.cache) > 32:
                self.cache.popitem(last=False)
            return result
        finally:
            self.pending.pop(key, None)

    async def _search(self, item, ep, sources, prefs, alternative, generation, saved_only=False):
        memory = item.get("source_memory", {})
        last_link = next((link for link in ep["links"] if link["id"] == ep["last_link_id"]), None)
        last = memory.get("last_source_id") or (last_link or {}).get("source_id")
        played = sorted(
            (e for e in item["episodes"] if (e.get("playback_state") or {}).get("confirmed")),
            key=lambda e: e["playback_state"]["updated_at"],
            reverse=True,
        )
        if played:
            successful = next(
                (
                    link
                    for link in played[0]["links"]
                    if link["id"] == played[0]["playback_state"]["link_id"]
                ),
                None,
            )
            last = (successful or {}).get("source_id") or last
        semaphore = asyncio.Semaphore(3)

        async def search_source(source):
            async with semaphore:
                adapter = self.adapter(source)
                links = [link for link in ep["links"] if link["source_id"] == source["id"]]
                try:
                    async with asyncio.timeout(90):
                        rows = [
                            await adapter.resolve(
                                {
                                    "url": link["url"],
                                    "kind": "page",
                                    "match": 1.0,
                                    "link_id": link["id"],
                                    "voice": link.get("voice", ""),
                                    "scope": "series"
                                    if item["kind"] == "series"
                                    and isinstance(adapter, SitemapAdapter)
                                    else "episode",
                                    "language": source.get("language", ""),
                                    "access_type": source.get("access_type", "unknown"),
                                    **await self.db("verify_link", link["id"]),
                                    "_expected": {
                                        **item,
                                        "season": ep["season"],
                                        "episode": ep["episode"],
                                    },
                                }
                            )
                            for link in links
                        ]
                        if not saved_only and (
                            not rows
                            or not any(
                                r["availability"] == "available"
                                and r["match"] >= 0.95
                                and (item["kind"] == "movie" or r.get("scope") == "episode")
                                for r in rows
                            )
                        ):
                            rows.extend(
                                await adapter.search(
                                    {**item, "season": ep["season"], "episode": ep["episode"]}
                                )
                            )
                except (
                    httpx.HTTPError,
                    ToolError,
                    OSError,
                    TimeoutError,
                    ValueError,
                    TypeError,
                    KeyError,
                ) as exc:
                    rows = [
                        {
                            "kind": "error",
                            "url": "",
                            "match": 0.0,
                            "availability": "restricted"
                            if isinstance(exc, ToolError)
                            else "unknown",
                            "embedded": False,
                            "reason": str(exc)
                            if isinstance(exc, ToolError)
                            else f"Проверка не завершена: {type(exc).__name__}. "
                            "Источник недоступен или истекло время ожидания.",
                        }
                    ]
                for row in rows:
                    if row.get("link_id"):
                        await self.db(
                            "link_check", row["link_id"], row["availability"], row["embedded"]
                        )
                    row.update(
                        source_id=source["id"],
                        source_name=source["name"],
                        title_id=item["id"],
                        episode_id=ep["id"],
                        capabilities=adapter.getPlaybackCapabilities(),
                        playback_confirmed=False,
                    )
                    row["access_type"] = row.get("access_type") or "unknown"
                    if row.get("region_checked") and row["region_checked"] != prefs.watch_region:
                        row["region_access"] = "unknown"
                    row["access_status"] = (
                        row.get("access_status", "UNKNOWN")
                        if source.get("access_status") == "MIXED" or row.get("cost_verified")
                        else source.get("access_status", "UNKNOWN")
                    )
                    row["allowed"] = not prefs.free_only or row["access_status"] == "FREE"
                    row["manual_allowed"] = row["access_status"] == "UNKNOWN"
                    row["requires_confirmation"] = prefs.ask_paid and (
                        row["access_status"] in {"PAID", "MIXED"}
                        or source.get("access_status") in {"PAID", "MIXED"}
                    )
                    if source["adapter"] == "local":
                        row["allowed"] = True
                    row["score"] = (
                        100 * row["match"]
                        + (30 if row["availability"] == "available" else -100)
                        + (24 if memory.get("pinned_source_id") == source["id"] else 0)
                        + (16 if prefs.use_last_source and last == source["id"] else 0)
                        + (8 if source["preferred"] else 0)
                        - min(source["priority"], 100) / 10
                        + min(4, math.log1p(source.get("health", {}).get("successes", 0)))
                        + (4 if row.get("language") == prefs.preferred_language else 0)
                    )
                return rows

        groups = await asyncio.gather(*(search_source(s) for s in sources))
        if generation != self.generation:
            raise ToolError("Настройки источников изменились. Повтори поиск.")
        rows = [r for group in groups for r in group if not alternative or r["source_id"] != last]

        def exact(r):
            return (
                r.get("content_verified") is True
                and r.get("video_type") in FULL_TYPES
                and r["match"] >= 0.95
                and r["availability"] == "available"
                and (item["kind"] == "movie" or r.get("scope") == "episode")
            )

        if (
            not saved_only
            and self.web
            and prefs.internet_search
            and not any(exact(r) for r in rows)
        ):
            self.web.http = self.http
            preferred = next(
                (
                    urlsplit(s["base_url"]).hostname
                    for s in sources
                    if s["id"] == last and s["base_url"]
                ),
                "",
            )
            try:
                async with asyncio.timeout(90):
                    discovered = await self.web.search(
                        {
                            **item,
                            "season": ep["season"],
                            "episode": ep["episode"],
                            "preferred_source": preferred,
                        },
                        prefs,
                        await self.db("sources"),
                    )
            except TimeoutError:
                discovered = [
                    self.web.error(
                        "Истекло время интернет-поиска. Прежний прогресс сохранён; "
                        "автоматический повтор не выполнялся."
                    )
                ]
            for row in discovered:
                if alternative and row["source_id"] == last:
                    continue
                row.update(title_id=item["id"], episode_id=ep["id"])
                if row["kind"] == "page":
                    row.update(
                        allowed=not prefs.free_only or row["access_status"] == "FREE",
                        manual_allowed=row["access_status"] == "UNKNOWN",
                        capabilities=capabilities(
                            row.get("source_config")
                            or next(s for s in sources if s["id"] == row["source_id"])
                        ),
                    )
                    row["score"] = 100 + (
                        16 if prefs.use_last_source and last == row["source_id"] else 0
                    )
                rows.append(row)
        if generation != self.generation:
            raise ToolError("Настройки источников изменились. Повтори поиск.")
        for row in rows:
            row.setdefault("video_type", "unknown")
            row.setdefault("content_verified", False)
            row.setdefault("classification_reason", "Тип самого видео не подтверждён.")
            if row["video_type"] in PROMO_TYPES:
                row["allowed"] = row["manual_allowed"] = False
                row["configurable"] = False
            row["score"] += (
                100 if row["content_verified"] and row["video_type"] in FULL_TYPES else -100
            )
            row.setdefault("scope", "movie" if item["kind"] == "movie" else "series")
            row["score"] += (35 if row["scope"] == "episode" else 0) + (
                20 if prefs.prefer_free and row.get("access_status") == "FREE" else 0
            )
            row["score"] += (
                10 if prefs.prefer_russian and row.get("language", "").startswith("ru") else 0
            ) + (12 if row.get("region_access") == "allowed" else 0)
            row["score"] += 6 if row.get("capabilities", {}).get("progress") else 0
            state = ep.get("playback_state") or {}
            if state.get("confirmed") and state.get("link_id") == row.get("link_id"):
                row["score"] += 30
                row["previously_played"] = True
            row["voice_change"] = bool(
                prefs.remember_voice
                and memory.get("last_voice")
                and row.get("voice", "") != memory["last_voice"]
            )
            row["requires_confirmation"] = (
                row.get("requires_confirmation", False)
                or row.get("new_source", False)
                or row["voice_change"]
            )
            if row.get("region_access") == "blocked":
                row["allowed"] = row["manual_allowed"] = False
                row["reason"] += " Регион отсутствует в опубликованном списке доступности."
        rows.sort(
            key=lambda r: (
                r["allowed"],
                r["availability"] == "available"
                or (
                    bool(r.get("previously_played"))
                    and r["availability"] in {"unknown", "restricted"}
                ),
                item["kind"] == "series" and r.get("scope") == "episode",
                r["source_id"] == memory.get("pinned_source_id"),
                prefs.use_last_source and r["source_id"] == last,
                r["score"],
            ),
            reverse=True,
        )
        for row in rows:
            row["id"] = uuid4().hex
            self.results[row["id"]] = (self.clock() + 300, row)
        while len(self.results) > 256:
            self.results.popitem(last=False)
        verified = [
            r
            for r in rows
            if r["allowed"]
            and r.get("content_verified") is True
            and r.get("video_type") in FULL_TYPES
            and r["match"] >= 0.95
            and (
                r["availability"] == "available"
                or (
                    r.get("previously_played")
                    and r.get("scope") in {"episode", "movie"}
                    and r["availability"] in {"unknown", "restricted"}
                )
            )
        ]
        return {
            "title_id": item["id"],
            "episode_id": ep["id"],
            "results": rows,
            "selected_id": verified[0]["id"] if verified and prefs.auto_select_source else None,
            "message": "Найдено совпадение. Выбери вариант просмотра."
            if verified
            else "Источник для просмотра не найден. Тип видео или соответствие произведению "
            "не подтверждены. Причины указаны у источников; трейлер вместо фильма не открывается.",
        }
