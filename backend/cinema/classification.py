"""Separate work identity from the identity of its actual playback media.

A Movie/TVEpisode card, matching title or runtime never proves that its video is
full length. Only an encoding explicitly attached to the matching work can do so.
"""

import re
import unicodedata
from urllib.parse import unquote, urljoin

FULL_TYPES = {"full_movie", "episode"}
PROMO_TYPES = {"trailer", "teaser", "review", "clip"}
LABELS = {
    "full_movie": "Полный фильм",
    "episode": "Эпизод сериала",
    "trailer": "Трейлер",
    "teaser": "Тизер",
    "review": "Обзор",
    "clip": "Клип или фрагмент",
    "unknown": "Неизвестный тип видео",
}


def normalize(value):
    return " ".join(
        re.findall(
            r"[^\W_]+", unicodedata.normalize("NFKC", str(value)).casefold().replace("ё", "е")
        )
    )


def promo_type(value, query=None):
    text = normalize(unquote(str(value)))
    # A legitimate work may itself be called Trailer Park Boys, for example.
    if query:
        meta = query.get("metadata") or {}
        for title in [
            query.get("title", ""),
            meta.get("original_title", ""),
            *meta.get("alternative_titles", []),
        ]:
            name = normalize(title)
            if name:
                text = text.replace(name, " ")
    if re.search(r"\b(trailer|трейлер|трэйлер)\b", text):
        return "trailer"
    if re.search(r"\b(teaser|тизер)\b", text):
        return "teaser"
    if re.search(r"\b(review|обзор|рецензия)\b", text):
        return "review"
    if re.search(
        r"\b(clip|clips|scene|fragment|featurette|клип|фрагмент|отрывок|сцена)\b",
        text,
    ):
        return "clip"
    return "unknown"


def entities(values, depth=0):
    if depth > 12:
        return
    for value in values if isinstance(values, list) else [values]:
        if not isinstance(value, dict):
            continue
        yield value
        yield from entities(value.get("@graph", []), depth + 1)
        yield from entities(value.get("mainEntity", []), depth + 1)


def kinds(entity):
    if not isinstance(entity, dict):
        return set()
    value = entity.get("@type", [])
    return {v for v in (value if isinstance(value, list) else [value]) if isinstance(v, str)}


def same_work(entity, query):
    episode = query.get("kind") == "series"
    if not ({"TVEpisode"} if episode else {"Movie"}) & kinds(entity):
        return False
    identity = entity.get("partOfSeries", {}) if episode else entity
    if not isinstance(identity, dict):
        return False
    meta = query.get("metadata") or {}
    names = {
        normalize(v)
        for v in [
            query.get("title", ""),
            meta.get("original_title", ""),
            *meta.get("alternative_titles", []),
        ]
        if v
    }
    if (
        not {normalize(identity.get("name", "")), normalize(identity.get("alternateName", ""))}
        & names
    ):
        return False
    year = str(identity.get("datePublished") or identity.get("dateCreated") or "")[:4]
    if not query.get("year") or year != str(query["year"]):
        return False
    if episode:
        season = entity.get("partOfSeason", {})
        try:
            return int(season.get("seasonNumber")) == query.get("season") and int(
                entity.get("episodeNumber")
            ) == query.get("episode")
        except (AttributeError, ValueError, TypeError):
            return False
    return True


def media_urls(media, base):
    values = media if isinstance(media, list) else [media]
    result = set()
    for value in values:
        if isinstance(value, dict):
            for field in ("contentUrl", "embedUrl", "url"):
                url = value.get(field)
                if isinstance(url, str) and len(url) <= 4096:
                    result.add(urljoin(base, url))
        elif isinstance(value, str) and len(value) <= 4096:
            result.add(urljoin(base, value))
    return result


def classify(query, evidence):
    """Bounded public observations only; duration may veto, never establish type."""
    base = str(evidence.get("page_url", ""))
    active = str(evidence.get("media_url", ""))
    frame = str(evidence.get("frame_url", ""))
    live = bool(evidence.get("live"))
    result = {
        "video_type": "unknown",
        "content_verified": False,
        "classification_reason": "Метаданные произведения не подтверждают тип самого видео.",
    }
    hints = [base, evidence.get("page_title", ""), evidence.get("video_title", ""), active]
    marked = next((kind for hint in hints if (kind := promo_type(hint, query)) != "unknown"), None)
    nodes = list(entities(evidence.get("structured", [])))[:100]
    full, promos = set(), {}
    for entity in nodes:
        # Promos can be separate nodes or attached to a work. Never treat `video`
        # alone as an encoding: schema.org defines it as any embedded video.
        for url in media_urls(entity.get("trailer", []), base):
            promos[url] = "trailer"
        if "VideoObject" in kinds(entity):
            kind = promo_type(entity.get("name", ""), query)
            if kind in PROMO_TYPES:
                for url in media_urls(entity, base):
                    promos[url] = kind
        if same_work(entity, query):
            for field in ("associatedMedia", "encoding"):
                media = entity.get(field, [])
                for item in media if isinstance(media, list) else [media]:
                    if (
                        isinstance(item, dict)
                        and promo_type(item.get("name", ""), query) in PROMO_TYPES
                    ):
                        for url in media_urls(item, base):
                            promos[url] = promo_type(item.get("name", ""), query)
                    else:
                        full.update(media_urls(item, base))
        if "VideoObject" in kinds(entity) and same_work(
            entity.get("encodesCreativeWork", {}), query
        ):
            full.update(media_urls(entity, base))
    # Active video is decisive even on a page containing both a full film and trailer.
    promotional = promos.get(active) or (promos.get(frame) if frame != base else None)
    if promotional or marked:
        return {
            **result,
            "video_type": promotional or marked,
            "classification_reason": (
                "Обнаружена явная пометка или адрес промо-видео; просмотр отменён."
            ),
        }
    full -= promos.keys()
    matched = active in full or (frame != base and frame in full)
    if full and (matched or not live):
        result.update(
            video_type="episode" if query.get("kind") == "series" else "full_movie",
            content_verified=True,
            classification_reason=(
                "Название, тип и номера серии совпали; известный год проверен. "
                "Опубликована привязка к полному медиа."
            ),
        )
        runtime = (query.get("metadata") or {}).get("runtime", 0)
        duration = evidence.get("duration", 0)
        if (
            live
            and query.get("kind") == "movie"
            and isinstance(runtime, (int, float))
            and runtime > 0
            and isinstance(duration, (int, float))
            and duration > 0
        ):
            expected = runtime * 60
            if abs(duration - expected) > max(90, expected * 0.25):
                result.update(
                    video_type="unknown",
                    content_verified=False,
                    classification_reason=(
                        "Длительность противоречит данным произведения. "
                        "Тип видео не подтверждён; это не доказательство трейлера."
                    ),
                )
    elif promos and not full and not live:
        result.update(
            video_type=next(iter(promos.values())),
            classification_reason=(
                "На странице опубликовано только промо-видео; полного источника нет."
            ),
        )
    return result
