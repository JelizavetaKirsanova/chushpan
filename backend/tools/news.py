"""AI news from RSS and Atom feeds (no API key), merged, deduplicated and cached."""

from __future__ import annotations

import asyncio
import html
import itertools
import json
import logging
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from backend.config import Settings
from backend.tools.registry import Tool, ToolError

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) my-jarvis"
MAX_AGE_H = 72  # search feeds (Google News) also return old articles
SUMMARY_CHARS = 220
DELFI_LATEST = "https://rus.delfi.ee/archive/latest"
REPLY_HINT = (
    "Follow the system personality, form of address and reply language. By default, give "
    "a brief spoken summary of the most relevant headlines. If the user asks for detail, "
    "explain fully without a sentence limit, using only the supplied facts. "
    "Use flowing speech, without markdown, numbering or lists."
)

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


@dataclass
class NewsItem:
    title: str
    source: str
    published: datetime | None = None
    summary: str = ""
    link: str = ""

    def hours_ago(self, now: datetime) -> int | None:
        if self.published is None:
            return None
        return max(0, int((now - self.published).total_seconds() // 3600))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, *names: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return _SPACE.sub(" ", html.unescape(_TAG.sub(" ", "".join(el.itertext())))).strip()


def _date(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)  # RSS: RFC 2822
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))  # Atom: ISO 8601
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def is_web_link(url: str) -> bool:
    """Only http(s) links reach the HUD: a feed could carry `javascript:` or `file:`."""
    return urlparse(url).scheme in ("http", "https") and bool(urlparse(url).netloc)


def _site_name(channel_title: str, url: str) -> str:
    # "AI News & Artificial Intelligence | TechCrunch" → "TechCrunch"
    name = re.split(r"\s+[|–-]\s+", channel_title)[-1].strip() if channel_title else ""
    return name or urlparse(url).netloc.removeprefix("www.")


def parse_feed(xml_text: str, url: str = "") -> list[NewsItem]:
    root = ET.fromstring(xml_text)
    channel = _child(root, "channel") if _local(root.tag) == "rss" else root
    site = _site_name(_text(_child(channel, "title")) if channel is not None else "", url)
    items: list[NewsItem] = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title = _text(_child(el, "title"))
        if not title:
            continue
        source = _text(_child(el, "source"))  # Google News names the original outlet
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].rstrip()
        summary = _text(_child(el, "description", "summary"))
        if _norm(summary).startswith(_norm(title)[:40]):
            summary = ""  # Google News repeats the title as an HTML link
        if len(summary) > SUMMARY_CHARS:
            summary = summary[:SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"
        link_el = _child(el, "link")
        link = ""
        if link_el is not None:
            link = link_el.get("href") or (link_el.text or "").strip()
        published = _child(el, "pubDate", "published", "updated", "date")
        items.append(
            NewsItem(
                title=title,
                source=source or site,
                published=_date(published.text or "") if published is not None else None,
                summary=summary,
                link=link,
            )
        )
    return items


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if c.isalnum() or c == " ").strip()


def merge(feeds: list[list[NewsItem]], now: datetime, max_age_h: int = MAX_AGE_H) -> list[NewsItem]:
    """Feeds interleaved, newest first, without duplicates or stale items.

    Round by round, each feed gives its next newest item. Sorting everything by date
    alone let the busy Google News searches (~90% of items, many from small outlets)
    push the curated feeds out of the top five.
    """
    oldest = datetime.min.replace(tzinfo=UTC)

    def newest_first(items: list[NewsItem]) -> list[NewsItem]:
        return sorted(items, key=lambda i: i.published or oldest, reverse=True)

    seen: set[str] = set()
    fresh: list[list[NewsItem]] = []
    for feed in feeds:
        kept = []
        for item in feed:
            key = _norm(item.title)[:60]
            hours = item.hours_ago(now)
            if key in seen or (hours is not None and hours > max_age_h):
                continue
            seen.add(key)
            kept.append(item)
        fresh.append(newest_first(kept))
    out: list[NewsItem] = []
    for round_ in itertools.zip_longest(*fresh):
        out += newest_first([i for i in round_ if i is not None])
    return out


class NewsService:
    def __init__(self, s: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.s = s
        self._client = client
        self._cache: tuple[float, list[NewsItem]] | None = None

    async def _fetch(self, client: httpx.AsyncClient, url: str) -> list[NewsItem]:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            return parse_feed(resp.text, url)
        except (httpx.HTTPError, ET.ParseError) as exc:
            log.warning("News feed failed %s: %s", url, exc)
            return []

    async def items(self) -> list[NewsItem]:
        ttl = self.s.news.cache_minutes * 60
        if self._cache and time.monotonic() - self._cache[0] < ttl:
            return self._cache[1]
        feeds = self.s.news.feeds
        if not feeds:
            raise ToolError("no news feeds configured")
        if self._client is not None:
            results = await asyncio.gather(*(self._fetch(self._client, u) for u in feeds))
        else:
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=True, headers={"User-Agent": USER_AGENT}
            ) as client:
                results = await asyncio.gather(*(self._fetch(client, u) for u in feeds))
        if not any(results):
            raise ToolError("news feeds unavailable right now")
        merged = merge(list(results), datetime.now(UTC))
        self._cache = (time.monotonic(), merged)
        return merged

    async def headlines(
        self, topic: str | None = None, count: int | None = None, links: bool = False
    ) -> dict:
        """`links`: for the HUD, which opens them. Never for the model: tokens it does not
        need, and it would read them out."""
        count = max(1, min(10, int(count or self.s.news.max_items)))
        items = await self.items()
        if topic:
            words = _norm(topic).split()
            items = [i for i in items if all(w in _norm(f"{i.title} {i.summary}") for w in words)]
        now = datetime.now(UTC)
        return {
            "topic": topic or "artificial intelligence",
            "found": len(items),
            "items": [
                {
                    "title": i.title,
                    "source": i.source,
                    "hours_ago": i.hours_ago(now),
                    **({"summary": i.summary} if i.summary else {}),
                    **({"link": i.link} if links and is_web_link(i.link) else {}),
                }
                for i in items[:count]
            ],
            # Last, next to where the answer starts: without it the reply came back as a
            # numbered list 6 times out of 6 once the history was left out (D-18).
            "how_to_reply": REPLY_HINT,
        }

    async def delfi_headlines(self) -> list[dict]:
        """Five latest publications from Delfi's Russian edition, never curated top stories.

        The public latest page embeds its article data as Nuxt's indexed JSON payload.
        Read only article title/date/id fields; never execute page scripts or read paywalls.
        This intentionally bypasses the half-hour AI-news cache for each morning request.
        """
        try:
            if self._client is not None:
                response = await self._client.get(DELFI_LATEST)
            else:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                    response = await client.get(DELFI_LATEST)
            response.raise_for_status()
            return parse_delfi_latest(response.text)
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
            raise ToolError("Не удалось загрузить свежие заголовки Delfi.") from exc


def parse_delfi_latest(page: str) -> list[dict]:
    payload = re.search(
        r'<script\b[^>]*\bid=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>', page, re.S
    )
    if not payload:
        raise ValueError("Delfi latest data is missing")
    data = json.loads(payload.group(1))
    if not isinstance(data, list):
        raise ValueError("Invalid Delfi data")

    def ref(index):
        if isinstance(index, int) and 0 <= index < len(data):
            return data[index]
        return None

    found: dict[str, dict] = {}
    now = datetime.now(UTC)
    for node in data:
        if not isinstance(node, dict) or ref(node.get("__typename")) != "Article":
            continue
        content = ref(node.get("content"))
        title = ref(content.get("title")) if isinstance(content, dict) else None
        title = ref(title.get("text")) if isinstance(title, dict) else None
        published = ref(node.get("publishAt"))
        published = _date(published) if isinstance(published, str) else None
        article_id = ref(node.get("id"))
        if not isinstance(title, str) or not title.strip() or not published or not article_id:
            continue
        if published > now or (now - published).total_seconds() > MAX_AGE_H * 3600:
            continue
        found[str(article_id)] = {
            "title": html.unescape(_TAG.sub("", title)).strip(),
            "published": published.isoformat(),
            "source": "Delfi",
            "link": f"https://rus.delfi.ee/artikkel/{article_id}",
        }
    items = sorted(found.values(), key=lambda item: item["published"], reverse=True)[:5]
    if not items:
        raise ValueError("No fresh Delfi headlines")
    return items


def news_tools(s: Settings, service: NewsService | None = None) -> list[Tool]:
    service = service or NewsService(s)
    return [
        Tool(
            name="get_ai_news",
            description=(
                "Latest artificial intelligence news headlines from several sources, newest "
                "first. Headlines may be in different languages: retell them in the reply "
                "language. Summarise briefly by default; provide more detail when asked."
            ),
            handler=service.headlines,
            parameters={
                "topic": {
                    "type": "string",
                    "description": "Optional keyword filter, e.g. 'OpenAI' or 'robots'",
                },
                "count": {"type": "integer", "description": "How many headlines (1-10)"},
            },
            slow=True,
            capability={
                "ru": "получать последние новости искусственного интеллекта",
                "pt": "trazer as últimas notícias de inteligência artificial",
                "en": "fetch the latest artificial intelligence news",
            },
        )
    ]
