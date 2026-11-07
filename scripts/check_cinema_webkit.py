"""Opt-in real WKWebView + production HUD + licensed external film + temporary SQLite.

No microphone, browser account, fixture video, installed app update or persistent service.
Run after `npm --prefix frontend run build` with --live. Audio stays muted.
"""

import argparse
import asyncio
import json
import socket
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import uvicorn

from backend.cinema.service import Cinema
from backend.config import Settings
from backend.context import DeviceContext
from backend.hud.bus import HudBus
from backend.hud.server import allowed_origins, create_app
from backend.hud.window import _Api
from backend.mac_agent import LocalMacAgent
from backend.memory.store import MemoryStore
from backend.tools.registry import ToolRegistry


async def check(window, report, title):
    async def js(expression):
        return await asyncio.to_thread(window.evaluate_js, expression)

    async def until(probe, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = await probe()
            if value:
                return value
            await asyncio.sleep(0.15)
        raise TimeoutError("Живой плеер не подтвердил ожидаемое состояние.")

    async def video():
        return await js(
            "(()=>{const v=document.querySelector('.cinema-embedded video');"
            "return v?{position:v.currentTime,duration:v.duration,paused:v.paused,"
            "seeking:v.seeking,ready:v.readyState,url:v.currentSrc,"
            "muted:v.muted,error_code:v.error?.code??null}:null})()"
        )

    with tempfile.TemporaryDirectory(prefix="chushpan-source-webkit-") as folder:
        root = Path(folder)
        memory = MemoryStore(root / "cinema.sqlite")
        tools = ToolRegistry()
        tools.mac_agent = LocalMacAgent()
        cinema = Cinema(tools, Settings(data_dir=root), memory)
        bus = HudBus()
        cinema.bind(bus, SimpleNamespace(device=DeviceContext.local_mac()))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        app = create_app(bus, allowed_origins("127.0.0.1", port))
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off")
        )
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            await until(lambda: asyncio.sleep(0, result=server.started))
            source = await cinema.db(
                "save_source",
                {
                    "name": "Blender · Internet Archive",
                    "base_url": "https://archive.org",
                    "adapter": "direct",
                    "search_mode": "archive",
                    "embed_allowed": True,
                    "access_status": "FREE",
                },
            )
            query = {"title": "Sintel", "kind": "movie", "year": 2010}
            report["searches"] = []
            for query_name in ("Синтел", "Sintel"):
                rows = await cinema.sources.adapter(source).search({**query, "title": query_name})
                assert rows[0]["content_verified"] and rows[0]["video_type"] == "full_movie"
                report["searches"].append({"query": query_name, "url": rows[0]["media_url"]})
            if title == "Sita":
                query = {"title": "Сита поёт блюз", "kind": "movie", "year": 2008}
            elif title != "Sintel":
                query = {
                    "title": "Каминандес",
                    "kind": "series",
                    "year": 2013,
                    "season": 1,
                    "episode": int(title[-1]),
                }
            candidate = (await cinema.sources.adapter(source).search(query))[0]
            assert candidate["content_verified"]
            report.update(
                media_url=candidate["media_url"],
                license=candidate["license"],
                video_type=candidate["video_type"],
                expected_duration=candidate["duration"],
            )
            item = await cinema.db("add", query)
            await cinema.db(
                "configure", {"internet_search": False, "auto_search": True, "poll_seconds": 2}
            )
            await cinema.publish()
            window.load_url(f"http://127.0.0.1:{port}")
            await until(lambda: js("!!document.querySelector('.workspace-nav [data-view=cinema]')"))
            await js(
                "window.__cinemaMediaEvents=[];"
                "['loadedmetadata','play','playing','pause','seeking','seeked','error','emptied']"
                ".forEach(name=>document.addEventListener(name,e=>{if(e.target.tagName==='VIDEO'){"
                "window.__cinemaMediaEvents.push({event:name,time:e.target.currentTime,"
                "paused:e.target.paused,muted:e.target.muted});"
                "if(window.__cinemaMediaEvents.length>60)window.__cinemaMediaEvents.shift();}},true));"
                "new MutationObserver(()=>document.querySelectorAll('video').forEach(v=>"
                "v.muted=true)).observe(document.body,{childList:true,subtree:true});"
                "document.querySelector('.workspace-nav [data-view=cinema]').click();true"
            )
            await until(lambda: asyncio.sleep(0, result=bus.clients > 0))
            # Production search → SourceResolver → binding → production HUD player.
            result = await cinema.resume(item["id"], player="embedded")
            if not cinema.session:
                choices = await cinema.resolver.search(item["id"])
                selected = next(r for r in choices["results"] if r.get("content_verified"))
                report["first_source_explicitly_selected"] = True
                result = await cinema.source_result(
                    selected["id"], open_result=True, player="embedded"
                )
            report["opening"] = result

            async def playing():
                state = await video()
                return state if state and cinema.playback.state == "PLAYING" else None

            state = await until(playing, 60)
            assert abs(state["duration"] - candidate["duration"]) < 2
            before = state["position"]
            await asyncio.sleep(1.2)
            state = await video()
            assert not state["paused"] and state["position"] > before + 0.5
            report.update(playback_confirmed=True, before=before, after=state["position"])
            paused = await cinema.execute("Поставь фильм на паузу")
            assert (await video())["paused"], paused
            report["pause"] = True
            old = (await video())["position"]
            await cinema.execute("Перемотай на десять секунд")
            state = await video()
            assert not state["seeking"] and abs(state["position"] - old - 10) < 2
            report["seek"] = True
            await js(
                "[...document.querySelectorAll('.cinema-embedded button')]"
                ".find(b=>b.textContent==='Полный экран').click();true"
            )

            async def fullscreen():
                from AppKit import NSWindowStyleMaskFullScreen
                from PyObjCTools.AppHelper import callAfter

                future = asyncio.get_running_loop().create_future()
                loop = asyncio.get_running_loop()

                def read():
                    value = bool(window.native.styleMask() & NSWindowStyleMaskFullScreen)
                    loop.call_soon_threadsafe(future.set_result, value)

                callAfter(read)
                return await future

            await until(fullscreen, 10)
            report["fullscreen"] = True
            saved = (await cinema.db("detail", item["id"]))["current"]["position"]
            assert saved >= old + 8
            report["saved_position"] = saved
            await cinema.stop_tracking()
            # Recreate SQLite service, not only the HTML media element.
            await cinema.close()
            cinema = Cinema(tools, Settings(data_dir=root), memory)
            cinema.bind(bus, SimpleNamespace(device=DeviceContext.local_mac()))
            await cinema.resume(item["id"], player="embedded")
            state = await until(playing, 60)
            assert abs(state["position"] - max(0, saved - 3)) < 4
            report.update(resume=True, resume_position=state["position"])
            await cinema.execute("Поставь фильм на паузу")
            second = await cinema.db(
                "save_source",
                {**source, "id": "", "name": "Archive · второй профиль", "priority": 1},
            )
            item = await cinema.db(
                "add",
                {
                    **query,
                    "title_id": item["id"],
                    "source_id": second["id"],
                    "url": candidate["safari_url"],
                },
            )
            link = next(
                link for link in item["current"]["links"] if link["source_id"] == second["id"]
            )
            old_token = cinema.session["token"]
            await cinema.open(item["id"], link_id=link["id"], player="embedded")
            await until(playing, 60)
            assert cinema.session["token"] != old_token
            assert cinema.session["target"]["source"]["id"] == second["id"]
            report.update(
                source_switch={"confirmed": True, "same_provider": True}, status="confirmed"
            )
        except Exception as exc:
            report["error"] = {
                "code": getattr(exc, "code", type(exc).__name__),
                "message": str(exc)[:500],
            }
            report["last_video"] = await video()
            report["media_events"] = await js("window.__cinemaMediaEvents??[]")
            report["playback_state"] = cinema.playback.state
            report["player_message"] = await js(
                "document.querySelector('.cinema-embedded [role=status]')?.textContent??''"
            )
        finally:
            await cinema.close()
            memory.close()
            server.should_exit = True
            await serving


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--title", choices=["Sintel", "Sita", "Caminandes1", "Caminandes2"], default="Sintel"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.live:
        parser.error("Нужен --live: откроется временное окно Чушпана с внешним видео.")
    import webview

    report = {
        "engine": "WKWebView",
        "title": args.title,
        "local_fixture": False,
        "status": "unconfirmed",
    }
    api = _Api()
    window = webview.create_window(
        "Чушпан · прототип киноисточника",
        html="<p>Проверка открытого киноисточника…</p>",
        js_api=api,
        width=1440,
        height=900,
        background_color="#15121f",
    )
    api.window = window

    def run():
        try:
            asyncio.run(check(window, report, args.title))
        finally:
            if args.output:
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False), flush=True)
            window.destroy()

    with tempfile.TemporaryDirectory(prefix="chushpan-webkit-storage-") as storage:
        webview.start(run, gui="cocoa", private_mode=True, storage_path=storage)
    return 0 if report["status"] == "confirmed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
