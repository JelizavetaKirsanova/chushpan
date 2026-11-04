"""Isolated real HTML5 playback + HUD + SQLite restart check. No Safari or hardware.

Requires ffmpeg, a Playwright module and its Chromium browser. Pass --playwright
with the absolute path to playwright/index.mjs. Everything private is temporary.
"""

import argparse
import asyncio
import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import uvicorn
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from backend.brain.conversation import Conversation
from backend.cinema.adapters import PLAYER_ERRORS, HTML5Adapter
from backend.cinema.service import Cinema
from backend.cinema.tmdb import TMDB, TMDBCredentials
from backend.config import Settings
from backend.hud.bus import HudBus
from backend.hud.server import allowed_origins, create_app
from backend.mac_agent import LocalMacAgent
from backend.memory.store import MemoryStore
from backend.tools.registry import ToolError, ToolRegistry

ROOT = Path(__file__).resolve().parents[1]


class BrowserBridge:
    def __init__(self, process):
        self.process, self.lock = process, asyncio.Lock()

    async def request(self, op, **values):
        async with self.lock:
            self.process.stdin.write((json.dumps({"op": op, **values}) + "\n").encode())
            await self.process.stdin.drain()
            response = json.loads(await self.process.stdout.readline())
            if not response.get("ok"):
                raise ToolError(response.get("error", "Browser failed"))
            return response.get("result", {})

    async def evaluate(self, url, payload):
        result = await self.request("eval", url=url, payload=payload)
        if result.get("error"):
            raise ToolError(PLAYER_ERRORS.get(result["error"], "Player failed"))
        return result

    async def open(self, url):
        await self.request("open", url=url)


class NoLLM:
    async def chat_stream(self, *args, **kwargs):
        raise AssertionError("A simple cinema command must not call an LLM")
        yield  # pragma: no cover


async def check(module, screenshot, chromium=None, catalogue=False):
    with tempfile.TemporaryDirectory(prefix="chushpan-cinema-") as folder:
        path = Path(folder)
        media = path / "fixture"
        media.mkdir()
        fixture = (
            (ROOT / "tests/fixtures/cinema/player.html")
            .read_text()
            .replace('src="/sample.mp4"', 'src="sample.mp4"')
        )
        (media / "one.html").write_text(fixture)
        (media / "two.html").write_text(fixture.replace("серия 1", "серия 2"))
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x302139:s=320x180:r=10",
                "-t",
                "12",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(media / "sample.mp4"),
            ],
            check=True,
        )
        process = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "scripts/check_cinema_browser.mjs"),
            str(Path(module).resolve()),
            chromium or "",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        bridge = BrowserBridge(process)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        bus = HudBus()
        bus.cinema_url = base
        settings = Settings(language="ru-RU")

        failures = set()

        def catalogue_fixture(request):
            if "/genre/" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "genres": [
                            {"id": 53, "name": "триллер"},
                            {"id": 878, "name": "фантастика"},
                            {"id": 35, "name": "комедия"},
                        ]
                    },
                )
            if "configuration/countries" in request.url.path:
                return httpx.Response(200, json=[{"iso_3166_1": "US", "english_name": "США"}])
            if "configuration/languages" in request.url.path:
                return httpx.Response(200, json=[{"iso_639_1": "en", "name": "Английский"}])
            page = int(request.url.params.get("page", 1))
            tv = "/tv" in request.url.path
            if (
                page == 3
                and not tv
                and page not in failures
                and ("/discover/" in request.url.path or "/popular" in request.url.path)
            ):
                failures.add(page)
                return httpx.Response(503)
            rows = [
                {
                    "id": (2000 if tv else 1000) + (page - 1) * 20 + i,
                    "title": f"История {i + (page - 1) * 20}",
                    "name": f"Сериал {i + (page - 1) * 20}",
                    "original_title": f"Original story {i}",
                    "original_name": f"Series {i}",
                    "release_date": "2025-01-01",
                    "first_air_date": "2025-01-01",
                    "poster_path": f"/fixture{i}.jpg",
                    "original_language": "en",
                    "genre_ids": [53, 878, 35],
                    "vote_average": 7 + i / 20,
                    "popularity": 100 - i,
                    "overview": "Тестовые метаданные для проверки каталога.",
                }
                for i in range(1, 21)
            ]
            if request.url.path.rsplit("/", 1)[-1].isdigit():
                identifier = int(request.url.path.rsplit("/", 1)[-1])
                row = {
                    **rows[0],
                    "id": identifier,
                    "title": f"История {identifier - 1000}",
                    "genres": [{"id": 53, "name": "триллер"}],
                }
                return httpx.Response(200, json=row)
            return httpx.Response(
                200, json={"page": page, "total_pages": 8, "total_results": 160, "results": rows}
            )

        def setup():
            memory = MemoryStore(path / "assistant.sqlite")
            tools = ToolRegistry()
            tools.mac_agent = LocalMacAgent()
            tmdb = None
            if catalogue:
                credentials = TMDBCredentials(path / "integrations")
                credentials.save("fixture-token-for-catalogue")
                tmdb = TMDB(
                    credentials,
                    transport=httpx.MockTransport(catalogue_fixture),
                    request_interval=0,
                )
            cinema = Cinema(
                tools,
                settings,
                memory,
                adapter_factory=lambda _: HTML5Adapter(bridge, bridge.open),
                tmdb=tmdb,
            )
            tools.cinema = cinema
            conv = Conversation(settings, NoLLM(), memory, tools=tools)
            cinema.bind(bus, conv)
            return memory, cinema, conv

        memory, cinema, conv = setup()
        app = create_app(bus, allowed_origins("127.0.0.1", port))
        app.router.routes.insert(0, Mount("/fixture", StaticFiles(directory=media)))
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off")
        )
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            while not server.started:
                await asyncio.sleep(0.01)
            source = await cinema.db(
                "save_source", {"name": "Локальный HTML5", "base_url": base, "adapter": "html5"}
            )
            entry = {
                "title": "Тестовый сериал",
                "source_id": source["id"],
                "url": f"{base}/fixture/one.html",
            }
            item = await cinema.db("add", entry)
            item = await cinema.db(
                "add", {**entry, "episode": 2, "url": f"{base}/fixture/two.html"}
            )
            first, second = item["episodes"]
            await cinema.db("progress", first["id"], 3, 12, "manual", first["links"][0]["id"])

            async def say(text):
                answer = "".join([part async for part in conv.reply(text)])
                assert conv.last_stats.tools == ["cinema_command"], answer
                return answer

            await say("Продолжи мой сериал")
            assert cinema.session["target"]["episode"]["position"] >= 3
            await say("Поставь фильм на паузу")
            state = await cinema.session["adapter"].sample()
            assert state["paused"]
            await say("Перемотай на пять секунд")
            state = await cinema.session["adapter"].sample()
            assert 8 <= state["position"] < 10
            await say("Включи следующую серию")
            assert cinema.session["target"]["episode"]["id"] == second["id"]
            await say("Продолжи сериал и включи на телевизоре")
            assert "AirPlay" in cinema.airplay_result()["message"]
            await cinema.db("configure", {"auto_next": True, "poll_seconds": 2})
            await cinema.db("progress", first["id"], 11, 12, "manual", first["links"][0]["id"])
            await cinema.call(
                "cinema_open",
                {"title_id": item["id"], "episode_id": first["id"]},
                "Открой сериал Тестовый сериал",
            )
            for _ in range(100):
                if cinema.session and cinema.session["target"]["episode"]["id"] == second["id"]:
                    break
                await asyncio.sleep(0.1)
            assert cinema.session and cinema.session["target"]["episode"]["id"] == second["id"], (
                "Auto-next failed"
            )
            await asyncio.sleep(0.5)
            await bridge.request("close_page")
            for _ in range(50):
                if not cinema.session:
                    break
                await asyncio.sleep(0.1)
            assert cinema.session is None, "pagehide progress beacon not received"
            before = (await cinema.db("target", item["id"], second["id"]))["episode"]["position"]
            assert before > 0
            await cinema.close()
            memory.close()
            memory, cinema, conv = setup()
            await say("Продолжи мой сериал")
            assert cinema.session["target"]["episode"]["id"] == second["id"]
            assert cinema.session["target"]["episode"]["position"] >= before
            async with cinema.lock:
                await cinema.stop_tracking()
            await bridge.request("ui", url=base, screenshot=screenshot)
            if catalogue:
                await bridge.request("catalogue_ui", url=base, screenshot=screenshot)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "checks": [
                            "real_muted_html5",
                            "restore",
                            "pause",
                            "seek",
                            "next",
                            "auto_next",
                            "pagehide_save",
                            "sqlite_restart",
                            "local_voice_no_llm",
                            "hud_desktop_mobile",
                        ],
                        "catalogue_tested": catalogue,
                        "safari_tested": False,
                        "external_sources_tested": False,
                    }
                )
            )
        except Exception as exc:
            print(f"Cinema browser check failed: {exc}", file=sys.stderr, flush=True)
            raise
        finally:
            await cinema.close()
            memory.close()
            server.should_exit = True
            await bridge.request("shutdown")
            process.stdin.close()
            await asyncio.wait_for(process.wait(), 10)
            await asyncio.wait_for(serving, 10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playwright", required=True)
    parser.add_argument("--screenshot")
    parser.add_argument("--chromium", help="Optional installed Chromium executable")
    parser.add_argument(
        "--catalogue", action="store_true", help="Also test paged catalogue with MockTransport"
    )
    args = parser.parse_args()
    asyncio.run(check(args.playwright, args.screenshot, args.chromium, args.catalogue))
