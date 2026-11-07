"""Opt-in public browser diagnostic. Reports/screenshots stay outside source control."""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from backend.cinema.browser_diagnostics import BrowserDiagnostics
from backend.cinema.models import Source
from backend.cinema.sources import PublicHTTP, SourceManager


async def check(args):
    source = Source(
        id="diagnostic-source",
        name=urlsplit(args.source).hostname,
        base_url=args.source,
        adapter="direct" if urlsplit(args.source).hostname == "archive.org" else "html5",
        search_mode="archive" if urlsplit(args.source).hostname == "archive.org" else "manual",
        embed_allowed=urlsplit(args.source).hostname == "archive.org",
    ).model_dump()
    query = {
        "title": args.title,
        "year": args.year,
        "kind": args.kind,
        "season": args.season,
        "episode": args.episode,
        "region": "EE",
    }
    proof = None
    http = PublicHTTP()
    manager = SourceManager(None, http=http)
    if source["search_mode"] == "archive":
        rows = await manager.adapter(source).search(query)
        proof = next((r for r in rows if r.get("content_verified")), None)

    async def changed(**kwargs):
        pass

    root = args.output or Path(tempfile.mkdtemp(prefix="chushpan-playwright-"))
    diagnostic = BrowserDiagnostics(root, changed)
    report = await diagnostic.run(
        source,
        query,
        "EE",
        url=args.url or (proof or {}).get("url", ""),
        proof=proof,
        play=args.play,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--source", required=True)
    parser.add_argument("--url", default="")
    parser.add_argument("--title", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--kind", choices=["movie", "series"], default="movie")
    parser.add_argument("--season", type=int, default=1)
    parser.add_argument("--episode", type=int, default=1)
    parser.add_argument("--play", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.live:
        parser.error("Нужен --live: откроется публичная страница во временном Chromium.")
    report = asyncio.run(check(args))
    return int(args.play and not report.get("playback_confirmed"))


if __name__ == "__main__":
    raise SystemExit(main())
