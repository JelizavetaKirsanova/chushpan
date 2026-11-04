"""Official Brave Search, followed by independent, robots-aware page verification.

Search snippets never establish identity, legality, region access or playable video.
New domains need an explicit choice unless an official IMDb-linked homepage verifies them.
"""

import asyncio
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from backend.cinema.models import Source, origin, web_url
from backend.cinema.public_search import Page, PublicCatalogue, Robots, key
from backend.tools.registry import ToolError

BRAVE = "https://api.search.brave.com/res/v1/web/search"
COUNTRIES = set(
    "AR AU AT BE BR CA CL DK FI FR DE HK IN ID IT JP KR MY MX NL NZ NO CN PL PT PH RU SA ZA "
    "ES SE CH TW TR AE GB US ALL".split()
)


class SearchCredentials:
    def __init__(self, directory):
        self.path = Path(directory) / "brave-search.json"

    @property
    def configured(self):
        return self.path.is_file()

    def save(self, credential):
        if (
            not isinstance(credential, str)
            or len(credential) > 512
            or (credential and not re.fullmatch(r"[A-Za-z0-9._-]{12,512}", credential))
        ):
            raise ToolError("Укажи ключ Brave Search API без пробелов и переносов строк.")
        if not credential:
            self.path.unlink(missing_ok=True)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(".brave-" + secrets.token_hex(12) + ".new")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as file:
                json.dump({"credential": credential}, file)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self):
        try:
            return json.loads(self.path.read_text())["credential"]
        except (OSError, ValueError, KeyError):
            raise ToolError(
                "Подключи Brave Search API в настройках медиатеки → Поиск и Safari."
            ) from None


class BraveSearch:
    def __init__(self, credentials, *, transport=None):
        self.credentials, self.transport = credentials, transport

    async def search(self, query, region, language):
        token = self.credentials.read()
        params = {
            "q": query[:600],
            "count": 5,
            "country": region if region in COUNTRIES else "ALL",
            "search_lang": language
            if language in {"ru", "en", "et", "uk", "de", "fr", "es"}
            else "en",
            "safesearch": "strict",
            "spellcheck": "false",
        }
        async with httpx.AsyncClient(
            transport=self.transport, trust_env=False, timeout=12, follow_redirects=False
        ) as client:
            async with client.stream(
                "GET",
                BRAVE,
                params=params,
                headers={"X-Subscription-Token": token, "Accept": "application/json"},
            ) as response:
                if response.status_code != 200:
                    raise ToolError(
                        f"Brave Search API: HTTP {response.status_code}. "
                        "Проверь ключ, тариф и лимит запросов. Повтор не выполнялся."
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 2_000_000:
                        raise ToolError("Ответ поискового API превышает лимит.")
        try:
            rows = json.loads(body).get("web", {}).get("results", [])
            return [web_url(str(row["url"])) for row in rows[:5] if row.get("url")]
        except (ValueError, TypeError, KeyError):
            raise ToolError("Brave Search API вернул неподдерживаемый ответ.") from None


def number(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def verified_entity(entity, query):
    kind = entity.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    episode = "TVEpisode" in kinds
    if query["kind"] == "movie" and "Movie" not in kinds:
        return None
    if query["kind"] == "series" and not ({"TVSeries", "TVEpisode"} & set(kinds)):
        return None
    identity = entity.get("partOfSeries", {}) if episode else entity
    if not isinstance(identity, dict):
        return None
    meta = query.get("metadata", {})
    names = {
        key(t)
        for t in [
            query["title"],
            meta.get("original_title", ""),
            *meta.get("alternative_titles", []),
        ]
        if t
    }
    if not {key(identity.get("name", "")), key(identity.get("alternateName", ""))}.intersection(
        names
    ):
        return None
    imdb = meta.get("imdb_id", "")
    ids = json.dumps(identity.get("sameAs", []))
    external = bool(
        imdb
        and re.fullmatch(r"tt\d{5,12}", imdb)
        and re.search(r"imdb\.com/title/" + imdb + r"(?:[/\"?#]|$)", ids)
    )
    year = str(identity.get("datePublished") or identity.get("dateCreated") or "")[:4]
    if not external and (not query.get("year") or year != str(query["year"])):
        return None
    if episode:
        season = entity.get("partOfSeason", {})
        season = season.get("seasonNumber") if isinstance(season, dict) else None
        if number(season) != query.get("season") or number(
            entity.get("episodeNumber")
        ) != query.get("episode"):
            return None
    language = entity.get("inLanguage", "")
    language = language if isinstance(language, str) else ""
    allowed = entity.get("regionsAllowed", identity.get("regionsAllowed", []))
    allowed = re.split(r"[,;\s]+", allowed.strip().upper()) if isinstance(allowed, str) else allowed
    if not isinstance(allowed, list) or not all(
        isinstance(v, str) and re.fullmatch(r"[A-Z]{2}", v) for v in allowed
    ):
        allowed = []
    region = "allowed" if query.get("region") in allowed else "blocked" if allowed else "unknown"
    license_url = entity.get("license", identity.get("license", ""))
    free = (
        entity.get("isAccessibleForFree") is True
        and isinstance(license_url, str)
        and license_url.startswith("https://creativecommons.org/")
    )
    return {
        "scope": "episode" if episode else "series" if query["kind"] == "series" else "movie",
        "episode_match": episode or query["kind"] == "movie",
        "imdb_match": external,
        "access_status": "FREE" if free else "UNKNOWN",
        "license": license_url if free else "",
        "cost_verified": free,
        "language": language[:40],
        "region_access": region,
        "voice": "",
    }


class PageVerifier:
    def __init__(self, http):
        self.http = http

    async def verify(self, url, query):
        url = web_url(url)
        if (
            urlsplit(url).scheme != "https"
            or urlsplit(url).path in {"", "/"}
            or re.search(r"\.(exe|dmg|pkg|zip|torrent)(?:$|[?#])", url, re.I)
        ):
            raise ToolError("Результат не является безопасной HTTPS-страницей произведения.")
        catalogue = PublicCatalogue({"base_url": origin(url)}, self.http)

        # RFC 9309: a missing robots file (404) imposes no crawl rules. 403 remains a stop.
        async def same_site(target):
            if origin(target) != origin(url):
                raise ToolError("robots.txt перенаправляет на другой сайт; проверка остановлена.")

        response = await self.http.request(origin(url) + "/robots.txt", policy=same_site)
        if response.status_code not in {200, 404}:
            raise ToolError(f"robots.txt: HTTP {response.status_code}; проверка остановлена.")
        catalogue.rules = Robots(response.text if response.status_code == 200 else "")
        response = await catalogue.request(url)
        final = getattr(response, "extensions", {}).get("final_url", url)
        if final.rstrip("/") != url.rstrip("/"):
            raise ToolError(
                "Найденный адрес перенаправляет на другую страницу; совпадение не подтверждено."
            )
        for entity in Page(response.text).entities({"Movie", "TVSeries", "TVEpisode"}):
            verified = verified_entity(entity, query)
            if verified:
                return {
                    **verified,
                    "url": url,
                    "kind": "page",
                    "match": 1.0,
                    "availability": "available",
                    "embedded": False,
                    "playback_confirmed": False,
                    "reason": "Страница проверена по структурированным метаданным: название, "
                    "год/IMDb и тип совпали. "
                    + (
                        "Сезон и серия совпали. "
                        if verified["scope"] == "episode"
                        else "Это общая страница сериала. "
                        if query["kind"] == "series"
                        else ""
                    )
                    + "Видео и доступ к нему ещё не подтверждены.",
                }
        raise ToolError(
            "Метаданные страницы не подтвердили название, год/IMDb, тип или номер серии. "
            "Сниппет поисковика не принят за доказательство."
        )


class InternetSearch:
    def __init__(self, credentials, http, *, provider=None):
        self.credentials, self.http = credentials, http
        self.provider = provider or BraveSearch(credentials)

    @staticmethod
    def queries(query, prefs):
        meta = query.get("metadata", {})
        titles = list(
            dict.fromkeys(
                t
                for t in [
                    query["title"],
                    meta.get("original_title"),
                    *meta.get("alternative_titles", []),
                ]
                if t
            )
        )[:3]
        suffix = f" {query.get('year') or ''}"
        if query["kind"] == "series":
            suffix += (
                f" S{query['season']:02d}E{query['episode']:02d} "
                f"{query['season']} сезон {query['episode']} серия"
            )
        queries = [
            (
                f'"{t}"'
                + suffix
                + (" русская озвучка" if prefs.prefer_russian else "")
                + " смотреть легально"
            )[:600]
            for t in titles
        ]
        preferred = query.get("preferred_source", "")
        if (
            preferred
            and re.fullmatch(r"[a-zA-Z0-9.-]+", preferred)
            and "." in preferred
            and queries
        ):
            queries[0] = "site:" + preferred + " " + queries[0]
        return queries

    async def search(self, query, prefs, sources):
        if not self.credentials.configured:
            return [
                self.error(
                    "Brave Search API не подключён. Добавь ключ в настройках «Поиск и "
                    "Safari». Поиск по всему интернету не выполнялся."
                )
            ]
        urls = []
        try:
            for q in self.queries(query, prefs):
                urls.extend(
                    await self.provider.search(
                        q, prefs.watch_region, "ru" if prefs.prefer_russian else "en"
                    )
                )
            rows = []
            verifier = PageVerifier(self.http)
            for url in list(dict.fromkeys(urls))[:8]:
                known = next(
                    (
                        s
                        for s in sources
                        if s.get("base_url") and origin(s["base_url"]) == origin(url)
                    ),
                    None,
                )
                if known and not known["enabled"]:
                    continue
                try:
                    async with asyncio.timeout(20):
                        row = await verifier.verify(url, {**query, "region": prefs.watch_region})
                except (ToolError, httpx.HTTPError, OSError, TimeoutError, ValueError, TypeError):
                    continue
                identifier = "web-" + hashlib.sha256(origin(url).encode()).hexdigest()[:16]
                source = (
                    known
                    or Source(
                        id=identifier,
                        name=urlsplit(url).hostname,
                        base_url=origin(url),
                        adapter="html5",
                    ).model_dump()
                )
                row.update(
                    source_id=source["id"],
                    source_name=source["name"],
                    source_config=None if known else source,
                    new_source=not bool(known),
                    automatic_trust=bool(known),
                    access_type=source.get("access_type", "unknown"),
                )
                if known and source.get("access_status") in {"FREE", "PAID"}:
                    row["access_status"] = source["access_status"]
                rows.append(row)
            return rows or [
                self.error(
                    "Поисковик ответил, но среди результатов нет проверенной страницы "
                    "нужного произведения/серии. Защита, номера серий и недостающие "
                    "метаданные не обходились."
                )
            ]
        except (ToolError, httpx.HTTPError, OSError, TimeoutError, ValueError) as exc:
            return [
                self.error(
                    str(exc)
                    if isinstance(exc, ToolError)
                    else "Поисковый API временно недоступен. Повтор не выполнялся."
                )
            ]

    @staticmethod
    def error(message):
        return {
            "source_id": "",
            "source_name": "Поиск в интернете · Brave",
            "kind": "error",
            "url": "",
            "match": 0.0,
            "availability": "unknown",
            "embedded": False,
            "reason": message,
            "access_status": "UNKNOWN",
            "access_type": "unknown",
            "language": "",
            "configurable": False,
            "score": -200,
            "allowed": False,
            "manual_allowed": False,
            "capabilities": {"mode": "Официальный поисковый API"},
        }
