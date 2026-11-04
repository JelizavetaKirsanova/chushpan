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

from backend.cinema.models import Source, web_url
from backend.cinema.personal import normalized
from backend.tools.registry import ToolError


async def addresses(host, port):
    records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(row[4][0] for row in records))


class PublicHTTP:
    def __init__(self, *, transport=None, resolver=addresses):
        self.transport, self.resolver = transport, resolver
        self.semaphore = asyncio.Semaphore(3)

    async def request(self, url, *, method="GET", params=None):
        async with self.semaphore, asyncio.timeout(12):
            for _ in range(4):
                url = web_url(url)
                parsed = urlsplit(url)
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                if port not in {80, 443}:
                    raise ToolError("Проверка поддерживает публичные HTTP(S) сайты на стандартных портах.")
                resolved = await self.resolver(parsed.hostname, port)
                if not resolved or any(not ipaddress.ip_address(a).is_global for a in resolved):
                    raise ToolError("Фоновая проверка локальных и частных адресов запрещена.")
                target = httpx.URL(url).copy_with(host=resolved[0])
                async with httpx.AsyncClient(transport=self.transport, timeout=6, trust_env=False,
                                            follow_redirects=False) as client:
                    async with client.stream(method, target, params=params,
                                             headers={"Host": parsed.netloc,
                                                      "User-Agent": "Chushpan-Cinema/1.0",
                                                      "Accept": "application/json, */*;q=0.5"},
                                             extensions={"sni_hostname": parsed.hostname}) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                break
                            url, params = urljoin(url, location), None
                            continue
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 2_000_000:
                                raise ToolError("Ответ источника слишком большой.")
                        return httpx.Response(response.status_code, headers=response.headers,
                                              content=bytes(body))
            raise ToolError("Слишком много перенаправлений источника.")


def capabilities(source):
    source = Source.model_validate({k: v for k, v in source.items() if k != "health"})
    return {"search": source.search_mode == "archive", "search_page": bool(source.search_url),
            "resolve": source.search_mode == "archive" or source.adapter in {"local", "direct"},
            "episodes": False, "embedded": source.adapter == "direct" and source.embed_allowed,
            "progress": source.adapter in {"html5", "direct"},
            "mode": "Официальный API Internet Archive" if source.search_mode == "archive" else
            "Разрешённое прямое видео" if source.adapter == "direct" else
            "HTML5 через Safari" if source.adapter == "html5" else
            "Локальный файл" if source.adapter == "local" else "Ручной выбор на сайте"}


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
            return {"availability": "available" if exists else "unknown",
                    "reason": "Файл найден." if exists else "Нужен путь к конкретному файлу."}
        try:
            response = await self.http.request(url or self.source["base_url"], method="HEAD")
            status = response.status_code
            available = 200 <= status < 300
            return {"availability": "available" if available else "restricted" if status in {401, 403, 429} else
                    "unknown" if status == 405 else "unavailable",
                    "reason": "Страница отвечает; доступ к видео ещё не подтверждён." if available else
                    "Нужен ручной выбор: сайт ограничил проверку." if status in {401, 403, 405, 429} else
                    "Страница недоступна.", "content_type": response.headers.get("content-type", "").split(";")[0]}
        except (ToolError, httpx.HTTPError, OSError, TimeoutError, ValueError):
            return {"availability": "unknown", "reason": "Проверить адрес не удалось; можно выбрать вручную."}

    async def search(self, query):
        template = self.source.get("search_url")
        term = " ".join(str(v) for v in (query["title"], query.get("year")) if v)
        return [{"url": template.replace("{query}", quote(term, safe="")) if template else self.source["base_url"],
                 "kind": "search" if template else "home", "match": 0., "availability": "unknown",
                 "reason": "Автопоиск не поддерживается. Выбери результат на сайте вручную.",
                 "embedded": False, "language": self.source.get("language", ""),
                 "access_type": self.source.get("access_type", "unknown")}]

    async def resolve(self, content):
        check = await self.checkAvailability(content["url"])
        compatible = (self.source["adapter"] == "direct" and self.source.get("embed_allowed")
                      and check["availability"] == "available"
                      and check.get("content_type") in {"video/mp4", "video/webm", "video/x-m4v"})
        return {**content, **check, "embedded": bool(compatible)}

    async def getEpisodes(self, series):
        return []  # Never fabricate episode URLs on an arbitrary website.

    async def open(self, content):
        if self.opener is None:
            raise ToolError("Открытие сайта не настроено.")
        await self.opener(content["url"])
        return {"page_opened": True, "playback_confirmed": False,
                "message": "Открыта страница источника. Запуск видео не подтверждён."}


class ArchiveAdapter(WebsiteAdapter):
    async def search(self, query):
        if query["kind"] != "movie":
            return await super().search(query)
        titles = list(dict.fromkeys([query["title"], query.get("metadata", {}).get("original_title") or query["title"]]))
        title_query = " OR ".join(f'title:"{normalized(t)}"' for t in titles)
        params = {"q": f"mediatype:movies AND ({title_query})", "output": "json", "rows": 12,
                  "fl[]": ["identifier", "title", "year", "language", "mediatype"]}
        response = await self.http.request("https://archive.org/advancedsearch.php", params=params)
        if response.status_code != 200:
            return await super().search(query)
        try:
            docs = response.json().get("response", {}).get("docs", [])
        except ValueError:
            return await super().search(query)
        result = []
        names = {normalized(t) for t in titles}
        for row in docs[:12]:
            identifier = str(row.get("identifier", ""))
            year = str(row.get("year", ""))[:4]
            if (not re.fullmatch(r"[\w.-]{1,200}", identifier)
                or normalized(str(row.get("title", ""))) not in names
                or not query.get("year") or year != str(query["year"])):
                continue
            response = await self.http.request(f"https://archive.org/metadata/{identifier}")
            if response.status_code != 200:
                continue
            data = response.json()
            meta = data.get("metadata", {})
            if (data.get("is_dark") or str(meta.get("access-restricted-item", "")).casefold() == "true"
                or meta.get("mediatype") != "movies"
                or normalized(str(meta.get("title", ""))) not in names
                or str(meta.get("year") or meta.get("date", ""))[:4] != str(query["year"])):
                continue
            result.append({"url": f"https://archive.org/details/{identifier}", "kind": "page",
                           "match": 1., "availability": "available", "embedded": False,
                           "language": str(meta.get("language", ""))[:40], "access_type": "unknown",
                           "reason": "Официальный API: совпали название, год и тип. "
                           "Доступность воспроизведения и условия доступа проверь на странице."})
        return result or await super().search(query)


class SourceManager:
    def __init__(self, db, *, http=None, opener=None, clock=time.monotonic):
        self.db, self.http, self.opener, self.clock = db, http or PublicHTTP(), opener, clock
        self.adapters = {"manual": WebsiteAdapter, "archive": ArchiveAdapter}
        self.cache = OrderedDict()
        self.results = OrderedDict()
        self.pending = {}

    def adapter(self, source):
        return self.adapters[source.get("search_mode", "manual")](source, self.http, self.opener)

    def invalidate(self):
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

    async def search(self, title_id, episode_id=None, *, alternative=False, refresh=False):
        item = await self.db("detail", title_id)
        ep = next((e for e in item["episodes"] if e["id"] == episode_id),
                  item["current"] if episode_id is None else None)
        if not ep:
            raise ValueError("Серия не найдена.")
        prefs = await self.db("preferences")
        sources = [s for s in await self.db("sources") if s["enabled"]]
        key = (title_id, ep["id"], alternative)
        cached = self.cache.get(key)
        if not refresh and cached and cached[0] > self.clock():
            return cached[1]
        if key in self.pending:
            return await asyncio.shield(self.pending[key])
        task = asyncio.create_task(self._search(item, ep, sources, prefs, alternative))
        self.pending[key] = task
        try:
            result = await task
            self.cache[key] = (self.clock() + 300, result)
            while len(self.cache) > 32:
                self.cache.popitem(last=False)
            return result
        finally:
            self.pending.pop(key, None)

    async def _search(self, item, ep, sources, prefs, alternative):
        memory = item.get("source_memory", {})
        last_link = next((l for l in ep["links"] if l["id"] == ep["last_link_id"]), None)
        last = memory.get("last_source_id") or (last_link or {}).get("source_id")
        semaphore = asyncio.Semaphore(3)
        async def search_source(source):
            async with semaphore:
                adapter = self.adapter(source)
                links = [l for l in ep["links"] if l["source_id"] == source["id"]]
                try:
                    rows = [await adapter.resolve({"url": link["url"], "kind": "page", "match": 1.,
                                                   "link_id": link["id"], "language": source.get("language", ""),
                                                   "access_type": source.get("access_type", "unknown")})
                            for link in links]
                    if not rows or not any(r["availability"] == "available" for r in rows):
                        rows.extend(await adapter.search({**item, "season": ep["season"], "episode": ep["episode"]}))
                except (httpx.HTTPError, ToolError, OSError, TimeoutError, ValueError, TypeError, KeyError):
                    rows = await WebsiteAdapter(source, self.http).search(item) if source["adapter"] != "local" else []
                for row in rows:
                    row.update(source_id=source["id"], source_name=source["name"],
                               title_id=item["id"], episode_id=ep["id"],
                               capabilities=adapter.getPlaybackCapabilities(),
                               playback_confirmed=False)
                    row["access_type"] = row.get("access_type") or "unknown"
                    row["allowed"] = not prefs.free_only or row["access_type"] == "free"
                    if source["adapter"] == "local":
                        row["allowed"] = True
                    row["score"] = (100 * row["match"] + (30 if row["availability"] == "available" else -100)
                                    + (24 if memory.get("pinned_source_id") == source["id"] else 0)
                                    + (16 if prefs.use_last_source and last == source["id"] else 0)
                                    + (8 if source["preferred"] else 0) - min(source["priority"], 100) / 10
                                    + min(4, math.log1p(source.get("health", {}).get("successes", 0)))
                                    + (4 if row.get("language") == prefs.preferred_language else 0))
                return rows
        groups = await asyncio.gather(*(search_source(s) for s in sources))
        rows = [r for group in groups for r in group
                if not alternative or r["source_id"] != last]
        rows.sort(key=lambda r: (r["allowed"], r["score"]), reverse=True)
        for row in rows:
            row["id"] = uuid4().hex
            self.results[row["id"]] = (self.clock() + 300, row)
        while len(self.results) > 256:
            self.results.popitem(last=False)
        verified = [r for r in rows if r["allowed"] and r["availability"] == "available" and r["match"] >= .95]
        return {"title_id": item["id"], "episode_id": ep["id"], "results": rows,
                "selected_id": verified[0]["id"] if verified and prefs.auto_select_source else None,
                "message": "Найден подходящий источник. Нажми «Смотреть», чтобы открыть его."
                if verified else "Проверенного совпадения пока нет. Доступен ручной выбор на подключённых сайтах."}
