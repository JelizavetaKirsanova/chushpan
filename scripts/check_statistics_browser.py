"""Serve an isolated copy of saved data for dashboard UI acceptance, without audio or network.

Usage: .venv/bin/python scripts/check_statistics_browser.py --database data/jarvis.db
The browser connects to /ws using the production HUD handlers. Library edits affect only
the temporary copy. Catalogue browsing uses cached library metadata, never API credentials.
"""

import argparse
import asyncio
import json
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.cinema.store import CinemaStore  # noqa: E402
from backend.hud.bus import HudBus  # noqa: E402
from backend.hud.server import allowed_origins, create_app  # noqa: E402
from backend.memory.store import MemoryStore  # noqa: E402


async def check(args):
    with tempfile.TemporaryDirectory(prefix="chushpan-redesign-") as tmp:
        path = Path(tmp) / "acceptance.db"
        with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(path) as target:
                source.backup(target)
        memory, library = MemoryStore(path), CinemaStore(path)
        bus = HudBus()
        memory.performance.bind(bus, "Europe/Tallinn")
        cached = library.snapshot()
        seen = []

        def snapshot(**extra):
            bus.publish(
                {
                    "type": "cinema",
                    **library.snapshot(),
                    "devices": [],
                    "location": "general",
                    "durable": True,
                    "session": None,
                    "message": "",
                    "tmdb": {"configured": True},
                    **extra,
                }
            )

        def cinema(value):
            op = value.get("op")
            seen.append(op)
            if op == "catalogue_page":
                filters = value.get("filters", {})
                query = filters.get("query", "").casefold()
                items = [
                    {**item, "library_id": item["id"]}
                    for item in cached["items"]
                    if item.get("catalogue_key")
                    and query in item["title"].casefold()
                    and filters.get("kind", "all") in {"all", item["kind"]}
                ]
                page = filters.get("page", 1)
                snapshot(
                    catalogue_page={
                        "request_id": value["request_id"],
                        "page": page,
                        "items": items[(page - 1) * 20 : page * 20],
                        "next_page": page + 1 if len(items) > page * 20 else None,
                        "total_pages": max(1, (len(items) + 19) // 20),
                    }
                )
            elif op == "catalogue_config":
                snapshot(
                    catalogue_config={
                        "feeds": [],
                        "genres": ["триллер", "драма"],
                        "countries": [],
                        "languages": [],
                    }
                )
            elif op == "update":
                library.update_title(value["id"], value["values"])
                snapshot()
            elif op == "manual_progress":
                library.manual_progress(
                    value["title_id"],
                    value["season"],
                    value["episode"],
                    value["position"],
                    value["duration"],
                    value["status"],
                    value.get("last_viewed"),
                )
                snapshot(saved=value["title_id"])
            elif op == "preferences":
                # UI drafts are enough for this read-only acceptance fixture.
                snapshot()
            else:
                snapshot()

        bus.handle("cinema", cinema)
        bus.publish({"type": "state", "state": "sleeping"})
        snapshot()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        config = uvicorn.Config(
            create_app(bus, allowed_origins("127.0.0.1", port)), log_level="error"
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            while not server.started:
                await asyncio.sleep(0.02)
            process = await asyncio.create_subprocess_exec(
                "node",
                str(ROOT / "scripts/check_statistics_browser.mjs"),
                args.playwright,
                args.chromium,
                f"http://127.0.0.1:{port}",
                args.screenshot,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await process.communicate()
            print(out.decode().strip())
            if process.returncode:
                print(err.decode(), file=sys.stderr)
                raise RuntimeError("Browser acceptance failed")
            print(json.dumps({"copy_only": True, "cinema_requests": len(seen)}))
        finally:
            server.should_exit = True
            await task
            library.close()
            memory.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--playwright", required=True)
    parser.add_argument("--chromium", required=True)
    parser.add_argument("--screenshot", default="/tmp/chushpan-redesign.png")
    asyncio.run(check(parser.parse_args()))
