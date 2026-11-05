"""Opt-in real external playback through the application, isolated from user data.

Uses licensed Blender open films published through the Internet Archive API.
No local video fixture, media download, authentication or protection bypass.
"""

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from backend.cinema.adapters import HTML5Adapter, SafariBridge
from backend.cinema.native_player import AVPlayerAdapter
from backend.cinema.service import Cinema
from backend.config import Settings
from backend.context import DeviceContext
from backend.hud.bus import HudBus
from backend.mac_agent import LocalMacAgent
from backend.memory.store import MemoryStore
from backend.tools.registry import ToolRegistry
from scripts.check_cinema_safari import InstalledExecutor, close_fixture


async def check(engine, title, output=None):
    with tempfile.TemporaryDirectory(prefix="chushpan-external-playback-") as directory:
        root = Path(directory)
        settings = Settings(data_dir=root)
        memory = MemoryStore(root / "assistant.sqlite")
        tools = ToolRegistry()
        tools.mac_agent = LocalMacAgent()
        executor = InstalledExecutor()
        bridge = SafariBridge(settings, executor=executor)
        binary = Path(
            "dist/cinema-playback/Чушпан.app/Contents/Resources/native/cinema_player"
        ).resolve()
        cinema = Cinema(
            tools,
            settings,
            memory,
            adapter_factory=lambda _: HTML5Adapter(bridge),
            native_factory=lambda: AVPlayerAdapter(
                settings, binary=binary if binary.is_file() else None, muted=True
            ),
        )
        cinema.bind(HudBus(), SimpleNamespace(device=DeviceContext.local_mac()))
        report = {
            "engine": engine,
            "real_external_video": True,
            "local_fixture": False,
            "status": "unconfirmed",
            "title": title,
        }
        candidate = None
        try:
            source = await cinema.db(
                "save_source",
                {
                    "name": "Internet Archive · open films",
                    "base_url": "https://archive.org",
                    "adapter": "direct",
                    "search_mode": "archive",
                    "embed_allowed": True,
                    "access_status": "FREE",
                },
            )
            query = (
                {"title": "Sintel", "kind": "movie", "year": 2010, "season": 0, "episode": 0}
                if title == "Sintel"
                else {
                    "title": "Caminandes",
                    "kind": "series",
                    "year": 2013,
                    "season": 1,
                    "episode": int(title[-1]),
                }
            )
            candidate = (await cinema.sources.adapter(source).search(query))[0]
            assert candidate["content_verified"] and candidate["video_type"] in {
                "full_movie",
                "episode",
            }, candidate
            report.update(
                url=candidate["safari_url"],
                media_url=candidate["media_url"],
                search=True,
                content_verified=True,
                video_type=candidate["video_type"],
                license=candidate["license"],
            )
            item = await cinema.db(
                "add", {**query, "source_id": source["id"], "url": candidate["url"]}
            )
            ep = item["current"]
            await cinema.db(
                "progress", ep["id"], 30, candidate["duration"], "manual", ep["links"][0]["id"]
            )
            await cinema.db(
                "configure",
                {"auto_search": False, "poll_seconds": 2, "open_in_safari": engine == "safari"},
            )
            result = await cinema.resume(item["id"], ep["id"], player=engine)
            report["opening"] = {
                k: result.get(k)
                for k in ("playback_confirmed", "fullscreen_confirmed", "message", "error_code")
            }
            assert result.get("playback_confirmed"), result
            if engine == "safari":
                await cinema.session["adapter"].read("mute", 1)
            first = await cinema.player.sample()
            await asyncio.sleep(1.2)
            second = await cinema.player.sample()
            assert second["position"] > first["position"] + 0.5, (first, second)
            report.update(
                currentTime_before=first["position"],
                currentTime_after=second["position"],
                playback_confirmed=True,
                duration=second["duration"],
            )
            token = cinema.session["token"]
            await cinema.viewer_control(
                {"op": "playback_control", "token": token, "action": "pause"}
            )
            report["pause"] = (await cinema.player.sample())["paused"]
            try:
                await cinema.viewer_control(
                    {"op": "playback_control", "token": token, "action": "seek", "position": 40}
                )
                state = await cinema.player.sample()
                report["seek"] = abs(state["position"] - 40) < 2 and not state.get("seeking")
            except Exception as exc:
                report["seek"] = False
                report["seek_error"] = str(exc)
            before_reopen = time.monotonic()
            saved = (await cinema.db("detail", item["id"]))["current"]["position"]
            report["saved_position"] = saved
            await cinema.stop_tracking()
            reopened = await cinema.resume(item["id"], ep["id"], player=engine)
            state = await cinema.player.sample()
            report["resume"] = bool(
                reopened.get("playback_confirmed")
                # Fullscreen may wait for a gesture while the video keeps playing.
                # The play confirmation contains the time immediately after seek.
                and max(0, saved - 3) - 2
                <= reopened.get("position", -100)
                <= max(0, saved - 3) + (time.monotonic() - before_reopen) + 2
            )
            report["resume_position"] = reopened.get("position")
            report["position_after_fullscreen_wait"] = state["position"]
            report["status"] = "confirmed" if report["resume"] else "partial"
        except Exception as exc:
            report["error"] = {
                "code": getattr(exc, "code", type(exc).__name__),
                "message": str(exc)[:500],
            }
        finally:
            await cinema.close()
            memory.close()
            if candidate:
                try:
                    await close_fixture(executor, candidate["safari_url"])
                except Exception:
                    pass
            if output:
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False))
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--engine", choices=["safari", "native"], default="safari")
    parser.add_argument(
        "--title",
        choices=["Sintel", "Caminandes1", "Caminandes2", "Caminandes3"],
        default="Caminandes1",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.live:
        parser.error("Нужен --live: проверка откроет настоящее внешнее видео.")
    report = asyncio.run(check(args.engine, args.title, args.output))
    return 0 if report.get("playback_confirmed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
