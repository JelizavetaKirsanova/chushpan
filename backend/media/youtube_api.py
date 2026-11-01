"""Read-only YouTube Data API v3, bounded cache and Chushpan's own viewing history."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from urllib.parse import parse_qs, urlparse

import httpx

from backend.config import YouTubeVideoConfig
from backend.memory.store import MemoryStore
from backend.tools.registry import ToolError

log = logging.getLogger(__name__)


def _omit_youtube_http_url(record: logging.LogRecord) -> bool:
    # httpx logs request URLs at INFO. Keep only our method/counter log, not playlist IDs.
    return not any(
        isinstance(arg, httpx.URL)
        and arg.host == "www.googleapis.com"
        and arg.path.startswith("/youtube/v3/")
        for arg in (record.args if isinstance(record.args, tuple) else ())
    )


logging.getLogger("httpx").addFilter(_omit_youtube_http_url)
ID = re.compile(r"[A-Za-z0-9_-]{10,160}\Z")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
STATUSES = {"unwatched", "started", "partially_watched", "watched", "skipped", "unavailable"}


def playlist_id(value: str) -> str:
    value = value.strip()
    if ID.fullmatch(value):
        return value
    try:
        u = urlparse(value)
        ids = parse_qs(u.query).get("list", [])
        if (
            u.scheme == "https"
            and u.netloc.lower() in HOSTS
            and not u.fragment
            and (
                u.path in ("/playlist", "/watch", "/")
                or u.netloc.lower() == "youtu.be"
                and VIDEO_ID.fullmatch(u.path[1:])
            )
            and len(ids) == 1
            and ID.fullmatch(ids[0])
        ):
            return ids[0]
    except ValueError:
        pass
    raise ToolError("Нужна обычная HTTPS-ссылка YouTube с единственным list=ID плейлиста.")


def watch_url(video_id: str) -> str:
    if not VIDEO_ID.fullmatch(video_id):
        raise ToolError("Некорректный YouTube video ID.")
    # No list= parameter: YouTube must not start a playlist queue behind our back.
    return f"https://www.youtube.com/watch?v={video_id}"


def duration_seconds(value: str) -> int | None:
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value)
    if not m or not any(m.groups()):
        return None
    d, h, minutes, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + minutes * 60 + s


class APIError(ToolError):
    def __init__(self, message: str, *, temporary: bool = False):
        super().__init__(message)
        self.temporary = temporary


class YouTubeAPI:
    def __init__(self, config: YouTubeVideoConfig, transport=None):
        self.config, self.transport = config, transport
        self.requests = 0
        self._cooldown_until = 0.0

    async def get(self, client: httpx.AsyncClient, endpoint: str, budget: list[int], **params):
        if endpoint not in {"playlists", "playlistItems", "videos"}:
            raise ToolError("Недопустимый метод YouTube API.")
        if not self.config.api_key.get_secret_value():
            raise APIError("Добавь отдельный YOUTUBE_API_KEY в .env; ключ Gemini не используется.")
        if time.time() < self._cooldown_until:
            raise APIError(
                "Квота YouTube временно ограничена; использую кэш, если он есть.", temporary=True
            )
        for attempt in range(2):
            if budget[0] >= self.config.max_requests:
                raise APIError("Достигнут лимит запросов одного обновления YouTube.")
            budget[0] += 1
            self.requests += 1
            log.info(
                "YouTube API method=%s requests=%d estimated_quota_units=%d",
                endpoint,
                budget[0],
                budget[0],
            )
            try:
                response = await client.get(
                    f"https://www.googleapis.com/youtube/v3/{endpoint}", params=params
                )
            except httpx.TransportError:
                if attempt == 0:
                    continue
                raise APIError(
                    "YouTube не ответил вовремя; доступен последний сохранённый список.",
                    temporary=True,
                ) from None
            status = response.status_code
            if status == 200:
                try:
                    body = response.json()
                    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
                        raise ValueError
                    return body
                except (ValueError, TypeError):
                    raise APIError("YouTube вернул некорректные данные.") from None
            # Never relay response bodies, URLs, headers or raw transport exceptions.
            reason = ""
            try:
                errors = response.json().get("error", {}).get("errors", [])
                reason = errors[0].get("reason", "") if errors else ""
            except (ValueError, TypeError, AttributeError, IndexError):
                pass
            if status == 403 and reason in {
                "quotaExceeded",
                "dailyLimitExceeded",
                "rateLimitExceeded",
            }:
                self._cooldown_until = time.time() + 300
                raise APIError(
                    "Квота YouTube исчерпана; повтор через 5 минут или позже, доступен кэш.",
                    temporary=True,
                )
            if status in {429, 500, 502, 503, 504}:
                if status == 429:
                    self._cooldown_until = time.time() + 60
                    raise APIError(
                        "YouTube ограничил частоту запросов; доступен кэш.", temporary=True
                    )
                if attempt == 0:
                    continue
                raise APIError(
                    "YouTube временно недоступен; доступен последний список.", temporary=True
                )
            messages = {
                400: "YouTube: проверь ID плейлиста и настройки API-ключа.",
                401: "YouTube: ошибка авторизации, проверь отдельный API-ключ.",
                403: (
                    "YouTube запретил доступ: проверь включение API, ограничения ключа и "
                    "публичность плейлиста."
                ),
                404: (
                    "YouTube: плейлист не найден или приватный. Нужен публичный плейлист; "
                    "OAuth не настроен."
                ),
            }
            raise APIError(messages.get(status, f"YouTube API: ошибка HTTP {status}."))

    async def fetch(self, pid: str) -> dict:
        playlist_id(pid)
        budget = [0]
        try:
            async with asyncio.timeout(30):
                async with httpx.AsyncClient(
                    timeout=self.config.timeout_s,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport,
                    headers={"x-goog-api-key": self.config.api_key.get_secret_value()},
                ) as client:
                    info = await self.get(
                        client, "playlists", budget, part="snippet,contentDetails", id=pid
                    )
                    if not info["items"]:
                        raise APIError(
                            "Плейлист не найден или приватный. "
                            "Сделай его публичным и проверь ссылку."
                        )
                    items, token, seen = [], "", set()
                    while True:
                        page = await self.get(
                            client,
                            "playlistItems",
                            budget,
                            part="snippet,contentDetails,status",
                            playlistId=pid,
                            maxResults=50,
                            **({"pageToken": token} if token else {}),
                        )
                        items.extend(page["items"])
                        token = page.get("nextPageToken", "")
                        if not token:
                            break
                        if not isinstance(token, str) or len(token) > 1024 or token in seen:
                            raise APIError(
                                "YouTube повторил токен пагинации; неполный список не сохранён."
                            )
                        seen.add(token)
                    ids = list(
                        dict.fromkeys(i.get("contentDetails", {}).get("videoId", "") for i in items)
                    )
                    ids = [i for i in ids if VIDEO_ID.fullmatch(i)]
                    details = {}
                    for start in range(0, len(ids), 50):
                        data = await self.get(
                            client,
                            "videos",
                            budget,
                            part="snippet,contentDetails,status",
                            id=",".join(ids[start : start + 50]),
                        )
                        details.update({v["id"]: v for v in data["items"]})
                    videos = []
                    for position, item in enumerate(items):
                        sn = item.get("snippet", {})
                        vid = item.get("contentDetails", {}).get("videoId", "")
                        if not VIDEO_ID.fullmatch(vid):
                            continue
                        v = details.get(vid, {})
                        status = v.get("status", {})
                        # Embedding is irrelevant to ordinary browser playback.
                        available = (
                            bool(v)
                            and status.get("privacyStatus") != "private"
                            and status.get("uploadStatus", "processed") == "processed"
                        )
                        videos.append(
                            {
                                "id": vid,
                                "title": str(
                                    v.get("snippet", {}).get(
                                        "title", sn.get("title", "Недоступное видео")
                                    )
                                )[:300],
                                "channel": str(
                                    v.get("snippet", {}).get(
                                        "channelTitle", sn.get("videoOwnerChannelTitle", "")
                                    )
                                )[:200],
                                "channel_id": v.get("snippet", {}).get("channelId", ""),
                                "duration": duration_seconds(
                                    v.get("contentDetails", {}).get("duration", "")
                                ),
                                "position": sn.get("position", position),
                                "available": available,
                                "url": watch_url(vid),
                            }
                        )
                    return {
                        "id": pid,
                        "title": str(info["items"][0].get("snippet", {}).get("title", "Плейлист"))[
                            :300
                        ],
                        "videos": videos,
                        "fetched_at": time.time(),
                        "requests": budget[0],
                    }
        except TimeoutError:
            raise APIError(
                "Обновление YouTube превысило 30 секунд; доступен сохранённый список.",
                temporary=True,
            ) from None
        except (KeyError, TypeError, AttributeError, ValueError):
            raise APIError("YouTube вернул некорректный список; предыдущий кэш сохранён.") from None


class VideoLibrary:
    def __init__(
        self, store: MemoryStore, config: YouTubeVideoConfig, api: YouTubeAPI | None = None
    ):
        self.store, self.config = store, config
        self.api = api or YouTubeAPI(config)
        self.lock = asyncio.Lock()
        with store.memory_transaction():
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS youtube_playlist_cache (playlist_id TEXT "
                "PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL NOT NULL)"
            )
            store.db.execute("""CREATE TABLE IF NOT EXISTS youtube_video_history (
                playlist_id TEXT NOT NULL, video_id TEXT NOT NULL, title TEXT NOT NULL,
                last_started REAL, starts INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'unwatched' CHECK(status IN
                  ('unwatched','started','partially_watched','watched','skipped','unavailable')),
                duration REAL, progress REAL, source TEXT NOT NULL DEFAULT 'playlist',
                updated_at REAL NOT NULL, excluded INTEGER NOT NULL DEFAULT 0,
                prior_status TEXT,
                PRIMARY KEY(playlist_id, video_id))""")

    async def load(self, pid: str, force=False) -> dict:
        async with self.lock:
            row = self.store.db.execute(
                "SELECT * FROM youtube_playlist_cache WHERE playlist_id=?", (pid,)
            ).fetchone()
            if row and not force and time.time() - row["fetched_at"] < self.config.cache_ttl:
                return {**json.loads(row["payload"]), "cache": "fresh"}
            try:
                self.store._require_memory()
                data = await self.api.fetch(pid)
            except APIError as exc:
                if row and exc.temporary and time.time() - row["fetched_at"] < 30 * 86400:
                    return {**json.loads(row["payload"]), "cache": "stale", "warning": str(exc)}
                raise
            self.store._require_memory()
            with self.store.memory_transaction():
                self.store.db.execute(
                    (
                        "INSERT INTO youtube_playlist_cache VALUES (?,?,?) ON "
                        "CONFLICT(playlist_id) DO UPDATE SET "
                        "payload=excluded.payload,fetched_at=excluded.fetched_at"
                    ),
                    (pid, json.dumps(data, ensure_ascii=False), data["fetched_at"]),
                )
                for v in data["videos"]:
                    self.store.db.execute(
                        """INSERT INTO youtube_video_history
                        (playlist_id,video_id,title,status,duration,updated_at)
                        VALUES (?,?,?,?,?,?) ON CONFLICT(playlist_id,video_id) DO UPDATE SET
                        title=excluded.title,duration=excluded.duration,updated_at=excluded.updated_at,
                        prior_status=CASE WHEN excluded.status='unavailable'
                          AND youtube_video_history.status!='unavailable'
                          THEN youtube_video_history.status ELSE prior_status END,
                        status=CASE WHEN excluded.status='unavailable' THEN 'unavailable'
                          WHEN youtube_video_history.status='unavailable'
                          THEN COALESCE(prior_status,
                            CASE WHEN starts>0 THEN 'started' ELSE 'unwatched' END)
                          ELSE youtube_video_history.status END""",
                        (
                            pid,
                            v["id"],
                            v["title"],
                            "unwatched" if v["available"] else "unavailable",
                            v["duration"],
                            time.time(),
                        ),
                    )
            return {**data, "cache": "updated"}

    def history(self, pid: str) -> dict[str, dict]:
        return {
            r["video_id"]: dict(r)
            for r in self.store.db.execute(
                "SELECT * FROM youtube_video_history WHERE playlist_id=?", (pid,)
            )
        }

    def mark(
        self, pid: str, vid: str, status: str, *, progress=None, source="manual", launched=False
    ):
        if status not in STATUSES:
            raise ToolError("Неизвестный статус просмотра.")
        self.store._require_memory()
        with self.store.memory_transaction():
            row = self.history(pid).get(vid)
            if row is None:
                raise ToolError("Видео отсутствует в локальной истории этого плейлиста.")
            if progress is not None and (
                not isinstance(progress, (int, float))
                or not 0 <= progress <= (row["duration"] or 604800)
            ):
                raise ToolError("Недостоверный прогресс просмотра.")
            self.store.db.execute(
                """UPDATE youtube_video_history SET status=?,progress=?,source=?,updated_at=?,
                starts=starts+?,last_started=CASE WHEN ? THEN ? ELSE last_started END,excluded=?
                WHERE playlist_id=? AND video_id=?""",
                (
                    status,
                    progress,
                    source,
                    time.time(),
                    int(launched),
                    int(launched),
                    time.time(),
                    int(status == "skipped"),
                    pid,
                    vid,
                ),
            )

    def reset(self, pid: str):
        self.store._require_memory()
        with self.store.memory_transaction():
            self.store.db.execute(
                """UPDATE youtube_video_history SET status=CASE
                  WHEN status='unavailable' THEN status ELSE 'unwatched' END,
                  starts=0,last_started=NULL,progress=NULL,excluded=0,prior_status=NULL,
                  source='manual_reset',updated_at=? WHERE playlist_id=?""",
                (time.time(), pid),
            )


def select_video(
    data: dict,
    history: dict,
    *,
    mode="random",
    min_seconds=0,
    max_seconds=86400,
    channel="",
    unwatched=True,
    exclude_recent_hours=24,
    allow_repeat=False,
    after_position: int | None = None,
    rng=None,
) -> dict:
    videos = [
        v for v in data["videos"] if v["available"] and not history.get(v["id"], {}).get("excluded")
    ]
    videos = [
        v
        for v in videos
        if (
            v["duration"] is not None
            and 0 < v["duration"]
            and min_seconds <= v["duration"] <= max_seconds
        )
        and (
            not channel
            or channel.casefold() in (v["channel"].casefold(), v.get("channel_id", "").casefold())
        )
    ]
    cutoff = time.time() - exclude_recent_hours * 3600
    videos = [
        v
        for v in videos
        if not exclude_recent_hours or (history.get(v["id"], {}).get("last_started") or 0) < cutoff
    ]
    if after_position is not None:
        videos = [v for v in videos if v["position"] > after_position]
    if not videos:
        raise ToolError(
            "Подходящих видео нет. Фильтры не изменял; уточни длительность, канал "
            "или недавние запуски."
        )
    fresh = [
        v for v in videos if history.get(v["id"], {}).get("status", "unwatched") == "unwatched"
    ]
    if fresh:
        videos = fresh
    elif unwatched and not allow_repeat:
        raise ToolError(
            "Непросмотренных по этим фильтрам не осталось. Скажи «включи повторно "
            "видео для сна», если хочешь повтор."
        )
    if mode == "longest":
        return max(videos, key=lambda v: v["duration"])
    if mode == "shortest":
        return min(videos, key=lambda v: v["duration"])
    if mode == "order":
        return min(videos, key=lambda v: v["position"])
    if mode != "random":
        raise ToolError("Неизвестный порядок выбора видео.")
    return (rng or random.SystemRandom()).choice(videos)
