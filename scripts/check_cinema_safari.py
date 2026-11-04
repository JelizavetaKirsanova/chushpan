"""Opt-in live Safari test on generated silent HTML5 video, isolated SQLite and URLs.

Uses the installed signed executor's actual Apple Events permissions. Never records,
changes Safari security settings, reads other pages or closes unrelated tabs.
"""

import argparse
import asyncio
import json
import socket
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from backend.cinema.adapters import HTML5Adapter, SafariBridge
from backend.cinema.service import Cinema
from backend.config import Settings
from backend.context import DeviceContext
from backend.hud.bus import HudBus
from backend.hud.server import allowed_origins, create_app
from backend.mac_agent import LocalMacAgent
from backend.media.safari import process
from backend.memory.store import MemoryStore
from backend.tools.registry import ToolError, ToolRegistry

HELPER = Path("/Applications/Чушпан.app/Contents/Frameworks/native/music_safari")


class InstalledExecutor:
    async def _run(self, script):
        result = json.loads(
            await process(str(HELPER), data={"op": "script", "source": script}, timeout=12)
        )
        if result.get("error"):
            raise ToolError("Safari: " + str(result["error"]))
        return result.get("result", "")


async def close_fixture(executor, url):
    # Close only the exact unique fixture tab. No current/front-tab heuristic.
    await executor._run(
        """tell application "Safari"
set found to {}
repeat with w in windows
repeat with t in tabs of w
if URL of t is """
        + json.dumps(url)
        + """ then set end of found to t
end repeat
end repeat
if (count of found) is 1 then close item 1 of found
return "closed"
end tell"""
    )


async def wait_until(predicate, timeout=12):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.15)
    raise AssertionError("Safari integration condition timed out")


async def check():
    with tempfile.TemporaryDirectory(prefix="chushpan-safari-") as directory:
        folder = Path(directory)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x241331:s=160x90:r=10:d=14",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(folder / "sample.mp4"),
            ],
            check=True,
        )
        for page in ["one", "two"]:
            (folder / (page + ".html")).write_text(
                '<!doctype html><meta charset="utf-8"><title>Чушпан Safari '
                'test</title><video muted playsinline controls src="sample.mp4"></video>'
            )
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        bus = HudBus()
        bus.cinema_url = base
        app = create_app(bus, allowed_origins("127.0.0.1", sock.getsockname()[1]))
        app.router.routes.insert(0, Mount("/fixture", StaticFiles(directory=folder)))
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off")
        )
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        executor = InstalledExecutor()

        def setup():
            memory = MemoryStore(folder / "assistant.sqlite")
            registry = ToolRegistry()
            registry.mac_agent = LocalMacAgent()
            safari = SafariBridge(Settings(data_dir=folder / "data"), executor=executor)
            cinema = Cinema(
                registry,
                Settings(data_dir=folder / "data"),
                memory,
                adapter_factory=lambda _: HTML5Adapter(safari),
            )
            cinema.bind(bus, SimpleNamespace(device=DeviceContext.local_mac()))
            return memory, cinema

        memory, cinema = setup()
        urls = [base + "/fixture/one.html", base + "/fixture/two.html"]
        try:
            await wait_until(lambda: asyncio.sleep(0, result=server.started))
            source = await cinema.db(
                "save_source",
                {
                    "name": "Safari test",
                    "base_url": base,
                    "adapter": "html5",
                    "access_status": "FREE",
                },
            )
            entry = {
                "title": "Safari test series",
                "kind": "series",
                "year": 2026,
                "source_id": source["id"],
                "url": urls[0],
            }
            item = await cinema.db("add", entry)
            item = await cinema.db("add", {**entry, "episode": 2, "url": urls[1]})
            first, second = item["episodes"]
            await cinema.db("progress", first["id"], 9, 14, "manual", first["links"][0]["id"])
            await cinema.db("configure", {"poll_seconds": 2, "auto_next": False})
            result = await cinema.resume(item["id"], first["id"])
            assert result.get("playback_confirmed"), result
            state = await cinema.session["adapter"].sample()
            assert 6 <= state["position"] < 8, state
            await cinema.call(
                "cinema_command", {"command": "поставь фильм на паузу"}, "поставь фильм на паузу"
            )
            assert (await cinema.session["adapter"].sample())["paused"]
            state = await cinema.session["adapter"].control("seek", 9)
            await cinema.save(state, "seek")
            await close_fixture(executor, urls[0])
            await asyncio.sleep(0.8)
            saved = await cinema.db("detail", item["id"])
            before = saved["episodes"][0]["position"]
            assert before >= 8.8, saved["episodes"][0]
            assert saved["episodes"][0]["playback_state"]["confirmed"]
            await cinema.close()
            memory.close()
            memory, cinema = setup()
            result = await cinema.resume(item["id"], first["id"])
            assert result.get("playback_confirmed"), result
            state = await cinema.session["adapter"].sample()
            assert abs(state["position"] - max(0, before - 3)) < 2, state
            await cinema.session["adapter"].control("seek", 13.7)

            async def offered():
                return cinema.next_episode

            offer = await wait_until(offered)
            assert offer["episode_id"] == second["id"], offer
            assert (await cinema.db("detail", item["id"]))["episodes"][0]["watched"]
            await cinema.db("configure", {"auto_next": True})
            await cinema.resume(item["id"], first["id"])
            await cinema.session["adapter"].control("seek", 13.7)

            async def next_started():
                return (
                    cinema.session
                    and cinema.session["target"]["episode"]["id"] == second["id"]
                    and cinema.session.get("counted")
                )

            await wait_until(next_started)
            await cinema.session["adapter"].control("pause")
            stages = {r["stage"] for r in await cinema.db("diagnostics", item["id"])}
            expected = {
                "SEARCH",
                "VERIFY",
                "OPEN_SAFARI",
                "DETECT_PLAYER",
                "RESTORE_POSITION",
                "PLAY",
                "SAVE_PROGRESS",
            }
            assert expected <= stages, stages
            print(
                json.dumps(
                    {
                        "ok": True,
                        "browser": "Safari",
                        "real_silent_video": True,
                        "exact_page": True,
                        "restored_with_3_second_rewind": True,
                        "pause": True,
                        "seek": True,
                        "close_and_app_restart": True,
                        "next_offer": True,
                        "auto_next": True,
                        "confirmed_progress_persisted": True,
                        "stages": sorted(stages),
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            await cinema.close()
            memory.close()
            for url in urls:
                try:
                    await close_fixture(executor, url)
                except ToolError:
                    pass
            server.should_exit = True
            await serving
            sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("Явно укажи --live: тест открывает собственные вкладки Safari.")
    asyncio.run(check())


if __name__ == "__main__":
    main()
