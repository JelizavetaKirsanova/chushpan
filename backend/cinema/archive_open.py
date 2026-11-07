"""Small licensed open-film catalogue backed by Internet Archive's public API.

No guess from duration or arbitrary item title. Each entry binds a work/episode,
creator, exact original file and licence; live metadata must still match.
"""

import math
import re
from urllib.parse import quote, unquote, urlsplit

from backend.cinema.personal import normalized
from backend.tools.registry import ToolError

FILMS = (
    {
        "id": "Sintel",
        "title": "Sintel",
        "aliases": ("Синтел",),
        "year": 2010,
        "file": "sintel-2048-stereo_512kb.mp4",
        "duration": 888,
    },
    {
        "id": "Caminandes1LlamaDrama",
        "title": "Caminandes 1: Llama Drama",
        "year": 2013,
        "episode": 1,
        "file": "01_llama_drama_1080p.mp4",
        "duration": 90,
    },
    {
        "id": "Caminandes2GranDillama",
        "title": "Caminandes 2: Gran Dillama",
        "year": 2013,
        "episode": 2,
        "file": "02_gran_dillama_1080p.mp4",
        "duration": 146.05,
    },
    {
        "id": "CaminandesLlamigos",
        "title": "Caminandes 3: Llamigos",
        "year": 2016,
        "episode": 3,
        "file": "Caminandes_ Llamigos-1080p.mp4",
        "duration": 150.15,
    },
    {
        "id": "Sita_Sings_the_Blues",
        "title": "Sita Sings the Blues",
        "aliases": ("Сита поёт блюз", "Сита поет блюз"),
        "year": 2008,
        "file": "Sita_Sings_the_Blues.mp4",
        "original": "Sita_Sings_the_Blues.ogv",
        "creator": "nina paley",
        "license": "https://creativecommons.org/publicdomain/zero/1.0/",
        "duration": 4891.24,
    },
)


def find(query=None, url=""):
    for film in FILMS:
        base = f"https://archive.org/download/{film['id']}/{quote(film['file'])}"
        if url and url not in {
            base,
            f"https://archive.org/details/{film['id']}",
            f"https://archive.org/embed/{film['id']}",
        }:
            continue
        if query:
            titles = {
                normalized(query.get("title", "")),
                normalized((query.get("metadata") or {}).get("original_title", "")),
            }
            if query.get("kind") == "series":
                if (
                    not titles.intersection({"caminandes", "каминандес"})
                    or query.get("season") != 1
                    or query.get("episode") != film.get("episode")
                ):
                    continue
                # A series start year differs from later episode release years.
                if query.get("year") not in {0, None, 2013}:
                    continue
            elif not titles.intersection(
                {normalized(film["title"]), *(normalized(v) for v in film.get("aliases", ()))}
            ) or query.get("year") not in {None, 0, film["year"]}:
                continue
        return film


async def resolve(http, source, *, query=None, url=""):
    film = find(query, url)
    if not film:
        return None
    response = await http.request(f"https://archive.org/metadata/{film['id']}")
    if response.status_code != 200:
        raise ToolError(f"Internet Archive metadata: HTTP {response.status_code}.")
    data = response.json()
    meta = data.get("metadata", {})
    # These are well identified public open movies, not a general claim that
    # every user-uploaded Archive item is licensed or contains the full work.
    licence = str(meta.get("licenseurl", "")).replace("http://", "https://", 1)
    creators = meta.get("creator", "")
    creators = " ".join(creators) if isinstance(creators, list) else str(creators)
    allowed_creator = "blender" in creators.casefold() or normalized(creators) in {
        "pablo vazquez",
        "pablo vázquez",
    }
    if film.get("creator"):
        allowed_creator = normalized(creators) == film["creator"]
    if (
        data.get("is_dark")
        or str(meta.get("access-restricted-item", "")).casefold() == "true"
        or meta.get("mediatype") != "movies"
        or not allowed_creator
        or normalized(meta.get("title", "")) != normalized(film["title"])
        or str(meta.get("year") or meta.get("date", ""))[:4] != str(film["year"])
        or (
            licence != film["license"]
            if film.get("license")
            else not licence.startswith("https://creativecommons.org/licenses/by/")
        )
    ):
        raise ToolError(
            "Метаданные, автор или открытая лицензия фильма Internet Archive не совпали."
        )
    media = next(
        (f for f in data.get("files", []) if f.get("name") == film["file"]),
        None,
    )
    if not media or str(media.get("private", "")).lower() == "true":
        raise ToolError("Internet Archive не опубликовал ожидаемый оригинальный MP4.")
    if media.get("source") != "original" and not (
        media.get("source") == "derivative"
        and media.get("original") == film.get("original", "sintel-2048-stereo.mp4")
        and (film.get("original") or film["id"] == "Sintel")
        and any(
            f.get("name") == media["original"] and f.get("source") == "original"
            for f in data.get("files", [])
        )
    ):
        raise ToolError("Не подтверждена связь файла с оригиналом полного фильма.")
    try:
        duration = float(media.get("length", 0))
    except (TypeError, ValueError):
        duration = 0
    if not math.isfinite(duration) or abs(duration - film["duration"]) > 5:
        raise ToolError("Длительность оригинального файла не совпала с каталогом полного фильма.")
    encoded = quote(film["file"])
    media_url = f"https://archive.org/download/{film['id']}/{encoded}"
    return {
        "url": media_url,
        "media_url": media_url,
        "safari_url": f"https://archive.org/embed/{film['id']}",
        "direct_media_urls": [media_url, f"https://archive.org/serve/{film['id']}/{encoded}"],
        "provider": "archive_open",
        "kind": "page",
        "scope": "episode" if query and query.get("kind") == "series" else "movie",
        "video_type": "episode" if query and query.get("kind") == "series" else "full_movie",
        "content_verified": True,
        "classification_reason": "Официальный API: совпали произведение, автор, год, "
        "открытая лицензия и связь конкретного файла с оригиналом; трейлер исключён.",
        "match": 1.0,
        "availability": "available",
        "embedded": source.get("adapter") == "direct" and source.get("embed_allowed") is True,
        "access_status": "FREE",
        "access_type": "free",
        "cost_verified": True,
        "license": licence,
        "reason": "Полный открытый фильм идентифицирован. "
        "Доступность плеера и запуск проверяются отдельно.",
        "duration": duration,
        "language": "",
        "title": film["title"],
        "year": film["year"],
    }


def verified_alias(proof, direct_url, media_url, query=None):
    """Only app-produced public Archive aliases; never accept arbitrary origins."""
    if proof.get("provider") != "archive_open":
        return False
    film = find(query, url=direct_url)
    if not film:
        return False
    parsed = urlsplit(media_url)
    return (
        parsed.scheme == "https"
        and (
            parsed.hostname == "archive.org"
            or bool(re.fullmatch(r"(?:ia|dn)\d+\.[a-z]{2}\.archive\.org", parsed.hostname or ""))
        )
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and not parsed.query
        and (
            parsed.path
            in {
                f"/download/{film['id']}/{quote(film['file'])}",
                f"/serve/{film['id']}/{quote(film['file'])}",
            }
            or (
                parsed.hostname != "archive.org"
                and bool(
                    re.fullmatch(
                        r"/\d{1,3}/items/" + re.escape(film["id"] + "/" + film["file"]),
                        unquote(parsed.path),
                    )
                )
            )
        )
    )
