"""Read-only discovery from published sitemaps. No login, player scraping or search bypass.

Robots rules are applied to every fetch and redirect, including duplicate '*' groups.
Only actual sitemap URLs are candidates; a slug is never used to fabricate a page URL.
"""

import asyncio
import json
import re
import time
import unicodedata
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from backend.cinema.models import origin
from backend.tools.registry import ToolError


def key(value):
    return " ".join(
        re.findall(
            r"[^\W_]+'?[^\W_]*",
            unicodedata.normalize("NFKC", str(value)).casefold().replace("ё", "е"),
        )
    )


def slug(value):
    ru = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    latin = [
        "a",
        "b",
        "v",
        "g",
        "d",
        "e",
        "e",
        "zh",
        "z",
        "i",
        "y",
        "k",
        "l",
        "m",
        "n",
        "o",
        "p",
        "r",
        "s",
        "t",
        "u",
        "f",
        "h",
        "ts",
        "ch",
        "sh",
        "sch",
        "",
        "y",
        "",
        "e",
        "yu",
        "ya",
    ]
    return "-".join(key(value).translate(str.maketrans(dict(zip(ru, latin, strict=True)))).split())


class Robots:
    def __init__(self, text):
        groups, agents, rules = [], [], []
        for line in text.splitlines() + ["User-agent: end-of-file"]:
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            name, value = [v.strip() for v in line.split(":", 1)]
            name = name.lower()
            if name == "user-agent":
                if rules:
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.lower())
            elif name in {"allow", "disallow"} and value:
                rules.append((name == "allow", value))
        specific = [
            (a, r) for a, r in groups if any(v != "*" and v in "chushpan-cinema" for v in a)
        ]
        if specific:
            longest = max(
                len(v) for agents, _ in specific for v in agents if v in "chushpan-cinema"
            )
            specific = [
                (agents, rules)
                for agents, rules in specific
                if any(len(v) == longest and v in "chushpan-cinema" for v in agents)
            ]
        chosen = specific or [(a, r) for a, r in groups if "*" in a]
        self.rules = [rule for _, rules in chosen for rule in rules]
        self.maps = re.findall(r"(?im)^sitemap:\s*(https?://\S+)", text)

    def allowed(self, url):
        parsed = urlsplit(url)
        path = unquote(parsed.path + ("?" + parsed.query if parsed.query else ""))
        matched = []
        for allow, pattern in self.rules:
            end = pattern.endswith("$")
            raw = pattern[:-1] if end else pattern
            expression = (
                "^"
                + ".*".join(re.escape(p) for p in unquote(raw).split("*"))
                + ("$" if end else "")
            )
            if re.search(expression, path):
                matched.append((len(raw.replace("*", "")), allow))
        return max(matched, default=(0, True))[1]


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.structured, self.ld, self.buffer = [], False, []
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href") and len(self.links) < 10000:
            self.links.append(attrs["href"])
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self.ld, self.buffer = True, []

    def handle_data(self, data):
        if self.ld:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.ld:
            self.ld = False
            try:
                self.structured.append(json.loads("".join(self.buffer)))
            except ValueError:
                pass

    def entities(self, types=None):
        types = types or {"Movie", "TVSeries"}

        def walk(value):
            if isinstance(value, list):
                for child in value:
                    yield from walk(child)
            elif isinstance(value, dict):
                kinds = value.get("@type", [])
                if set(kinds if isinstance(kinds, list) else [kinds]).intersection(types):
                    yield value
                # Do not accept a recommended item nested in ItemList as the page identity.
                yield from walk(value.get("@graph", []))
                yield from walk(value.get("mainEntity", []))

        for item in self.structured:
            yield from walk(item)


class PublicCatalogue:
    def __init__(self, source, http):
        self.source, self.http = source, http
        self.base = origin(source["base_url"])
        self.rules = None
        self.rules_expires = 0
        self.urls, self.expires = [], 0
        self.last_reason = ""

    async def request(self, url, **kwargs):
        async def policy(target):
            if origin(target) != self.base:
                raise ToolError(
                    "Источник перенаправил на другой сайт; автоматическая проверка остановлена."
                )
            if not self.rules.allowed(target):
                raise ToolError("robots.txt запрещает автоматический доступ к этому адресу.")

        await policy(url)
        response = await self.http.request(url, policy=policy, **kwargs)
        if response.status_code in {401, 403, 429}:
            raise ToolError(
                f"HTTP {response.status_code}: источник ограничил доступ "
                "(авторизация, защита или лимит). Обход не выполнялся."
            )
        if response.status_code != 200:
            raise ToolError(
                f"HTTP {response.status_code}: публичная страница или карта сайта недоступна."
            )
        if any(
            v in response.text[:20000].casefold()
            for v in ("cf-chl-", "just a moment...", "attention required! | cloudflare")
        ):
            raise ToolError(
                "Источник показал защиту Cloudflare; автоматическая проверка остановлена."
            )
        return response

    async def ensure_rules(self):
        if self.rules and self.rules_expires > time.monotonic():
            return

        async def same_site(target):
            if origin(target) != self.base:
                raise ToolError("robots.txt перенаправляет на другой сайт. Проверка остановлена.")

        response = await self.http.request(self.base + "/robots.txt", policy=same_site)
        if response.status_code != 200:
            raise ToolError(
                f"robots.txt: HTTP {response.status_code}. "
                "Разрешение автоматического поиска проверить не удалось."
            )
        self.rules = Robots(response.text)
        self.rules_expires = time.monotonic() + 3600

    async def index(self):
        if self.expires > time.monotonic():
            return self.urls
        await self.ensure_rules()
        maps = [u for u in self.rules.maps if origin(u) == self.base]
        if not maps:
            raise ToolError(
                "В robots.txt нет публичной карты сайта; поисковый путь "
                "Filmix /search запрещён. Надёжный разрешённый автопоиск не найден."
            )
        urls, visited = [], set()
        while maps and len(visited) < 6:
            url = maps.pop(0)
            if url in visited:
                continue
            visited.add(url)
            response = await self.request(url, max_bytes=16_000_000)
            try:
                root = await asyncio.to_thread(ElementTree.fromstring, response.content)
            except ElementTree.ParseError as exc:
                raise ToolError(
                    "Вместо XML-карты сайт вернул другую страницу. Автопоиск недоступен."
                ) from exc
            locations = [
                e.text.strip() for e in root.iter() if e.tag.split("}")[-1] == "loc" and e.text
            ][:100000]
            locations = [
                u
                for u in locations
                if u.startswith(("http://", "https://")) and origin(u) == self.base
            ]
            if root.tag.split("}")[-1] == "sitemapindex":
                maps.extend(locations[:6])
            else:
                urls.extend(locations)
            if len(urls) > 200000:
                raise ToolError("Карта сайта превышает лимит безопасного поиска.")
        self.urls, self.expires = list(dict.fromkeys(urls)), time.monotonic() + 3600
        return self.urls

    async def search(self, query):
        urls = await self.index()
        meta = query.get("metadata", {})
        titles = [
            query["title"],
            meta.get("original_title", ""),
            *meta.get("alternative_titles", []),
        ]
        names = {key(t) for t in titles if t}
        slugs = {slug(t) for t in titles if len(key(t)) >= 3}

        def shortlist():
            candidates = []
            for url in urls:
                path = unquote(urlsplit(url).path).lower()
                # Only actual title pages, not a homepage/season/search/category.
                if self.base == "https://kion.ru" and not re.fullmatch(
                    r"/(serial|film)/[^/]+/?", path
                ):
                    continue
                token = path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".html")
                variants = {token, re.sub(r"-\d{4}$", "", token)}
                if self.base != "https://kion.ru":
                    without_id = re.sub(r"^\d+-", "", token)
                    variants.update({without_id, re.sub(r"-\d{4}$", "", without_id)})
                if variants.intersection(slugs):
                    candidates.append(url)
            return candidates

        candidates = await asyncio.to_thread(shortlist)
        results = []
        self.last_reason = (
            "В публичной карте сайта нет страницы с таким названием."
            if not candidates
            else "Страницы найдены, но метаданные не подтверждают название, год и тип."
        )
        for url in candidates[:6]:
            response = await self.request(url)
            page = Page(response.text)
            for entity in page.entities():
                expected = "TVSeries" if query["kind"] == "series" else "Movie"
                page_names = {key(entity.get("name", "")), key(entity.get("alternateName", ""))}
                year = str(entity.get("datePublished") or entity.get("dateCreated") or "")[:4]
                if (
                    entity.get("@type") != expected
                    or not page_names.intersection(names)
                    or year != str(query.get("year"))
                ):
                    continue
                results.append(
                    {
                        "url": url,
                        "kind": "page",
                        "match": 1.0,
                        "availability": "available",
                        "embedded": False,
                        "language": self.source.get("language", ""),
                        "access_type": self.source.get("access_type", "unknown"),
                        "access_status": "UNKNOWN"
                        if self.source.get("access_status") == "MIXED"
                        else self.source.get("access_status", "UNKNOWN"),
                        "reason": "Публичная карта сайта и метаданные страницы: "
                        "совпали название, год и тип. "
                        "Условия доступа к видео и запуск плеера не подтверждены.",
                        "scope": "series" if query["kind"] == "series" else "movie",
                    }
                )
                if (
                    query["kind"] == "series"
                    and self.base == "https://kion.ru"
                    and query.get("season")
                    and query.get("episode")
                ):
                    episode = await self.kion_episode(page, url, query)
                    if episode:
                        results.insert(0, {**results[-1], **episode})
                break
        return results

    async def kion_episode(self, page, series_url, query):
        from urllib.parse import urljoin

        slug = urlsplit(series_url).path.rstrip("/").rsplit("/", 1)[-1]
        for published in page.links:
            target = urljoin(series_url, published)
            if origin(target) != self.base:
                continue
            match = re.fullmatch(
                r"/(?:serial|video)/" + re.escape(slug) + r"/sezon-(\d+)/seriya-(\d+)/?",
                urlsplit(target).path,
            )
            if not match or (int(match[1]), int(match[2])) != (query["season"], query["episode"]):
                continue
            try:
                response = await self.request(target)
                final = getattr(response, "extensions", {}).get("final_url", target)
                if final.rstrip("/") != target.rstrip("/"):
                    continue
                episodes = Page(response.text).entities({"TVEpisode"})
                if not any(key(e.get("name", "")) == f"серия {query['episode']}" for e in episodes):
                    continue
            except (ToolError, OSError, TimeoutError):
                continue
            return {
                "url": target,
                "scope": "episode",
                "episode_match": True,
                "episode_evidence": "publisher_link+TVEpisode",
                "reason": "Название, год и тип сериала подтверждены. "
                "Ссылка на нужный сезон и серию опубликована KION "
                "и отвечает метаданными TVEpisode. Видео ещё не проверено.",
            }
        return None
