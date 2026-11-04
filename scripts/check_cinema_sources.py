"""Opt-in, read-only live discovery check. Never opens a browser, logs in or plays video.

python -m scripts.check_cinema_sources --live [--tmdb-directory /private/integrations]
"""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from backend.cinema.public_search import PublicCatalogue
from backend.cinema.sources import PublicHTTP
from backend.cinema.tmdb import TMDB, TMDBCredentials
from backend.tools.registry import ToolError


async def check(directory=None):
    tmdb = TMDB(TMDBCredentials(directory)) if directory else None
    public = PublicHTTP()
    catalogue = PublicCatalogue({"base_url": "https://kion.ru", "access_status": "MIXED"}, public)
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "playback_tested": False,
        "queries": [],
        "sources": [],
    }
    samples = [
        ("tmdb:tv:250727", "Бар «Один звонок»", "series", 2025, ""),
        ("tmdb:movie:157336", "Интерстеллар", "movie", 2014, "Interstellar"),
        ("tmdb:movie:27205", "Начало", "movie", 2010, "Inception"),
        ("tmdb:tv:1396", "Во все тяжкие", "series", 2008, "Breaking Bad"),
    ]
    for identifier, title, kind, year, original in samples:
        query = {
            "title": title,
            "kind": kind,
            "year": year,
            "metadata": {"original_title": original},
        }
        try:
            if tmdb:
                query = await tmdb.detail(identifier)
            rows = await catalogue.search(query)
            report["queries"].append(
                {
                    "tmdb_id": identifier,
                    "title": query["title"],
                    "year": query["year"],
                    "kind": query["kind"],
                    "verified_urls": [r["url"] for r in rows],
                    "reason": "Название, год и тип совпали" if rows else catalogue.last_reason,
                }
            )
        except (ToolError, OSError, TimeoutError, ValueError) as exc:
            report["queries"].append(
                {
                    "tmdb_id": identifier,
                    "error": str(exc) if isinstance(exc, ToolError) else type(exc).__name__,
                }
            )
    for host in ["filmix.gg", "kinogo.is", "hdrezka.sh"]:
        source = PublicCatalogue({"base_url": "https://" + host}, public)
        try:
            rows = await source.search({"title": samples[0][1], "kind": "series", "year": 2025})
            report["sources"].append(
                {
                    "host": host,
                    "verified_urls": [r["url"] for r in rows],
                    "reason": source.last_reason if not rows else "Совпадение проверено",
                    "catalogue_urls": len(source.urls),
                }
            )
        except (ToolError, OSError, TimeoutError, ValueError) as exc:
            report["sources"].append(
                {
                    "host": host,
                    "reason": str(exc) if isinstance(exc, ToolError) else type(exc).__name__,
                }
            )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Разрешить публичные сетевые запросы")
    parser.add_argument("--tmdb-directory", type=Path)
    args = parser.parse_args()
    if not args.live:
        parser.error("Для реальных сетевых запросов явно укажи --live.")
    print(json.dumps(asyncio.run(check(args.tmdb_directory)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
