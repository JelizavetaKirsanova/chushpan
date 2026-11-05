"""Opt-in real AVKit window, silent generated media and isolated SQLite only.

Run: .venv/bin/python -m scripts.check_cinema_avplayer --live
No microphone, website credentials, installed library or system settings are used.
"""

import argparse
import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from backend.cinema.native_player import AVPlayerAdapter
from backend.cinema.playback import PlaybackState
from backend.cinema.service import Cinema
from backend.config import Settings
from backend.context import DeviceContext
from backend.hud.bus import HudBus
from backend.mac_agent import LocalMacAgent
from backend.memory.store import MemoryStore
from backend.tools.registry import ToolRegistry


async def check(binary=None):
    with tempfile.TemporaryDirectory(prefix="chushpan-avkit-") as directory:
        folder = Path(directory)
        media = folder / "sample.mp4"
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
                "color=c=0x241331:s=320x180:r=10:d=18",
                "-an",
                "-c:v",
                "libx264",
                "-g",
                "10",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(media),
            ],
            check=True,
        )
        settings = Settings(data_dir=folder)
        path = folder / "assistant.sqlite"
        memory = MemoryStore(path)
        tools = ToolRegistry()
        tools.mac_agent = LocalMacAgent()

        def create():
            cinema = Cinema(
                tools,
                settings,
                memory,
                native_factory=lambda: AVPlayerAdapter(settings, binary=binary, muted=True),
            )
            cinema.bind(HudBus(), SimpleNamespace(device=DeviceContext.local_mac()))
            return cinema

        cinema = create()
        try:
            src = await cinema.db(
                "save_source", {"name": "Generated silent fixture", "adapter": "local"}
            )
            entry = {
                "title": "AVKit fixture",
                "year": 2026,
                "kind": "series",
                "source_id": src["id"],
                "url": str(media),
            }
            item = await cinema.db("add", entry)
            item = await cinema.db("add", {**entry, "episode": 2})
            first, second = item["episodes"]
            await cinema.db("progress", first["id"], 9, 18, "manual", first["links"][0]["id"])
            result = await cinema.open(item["id"], first["id"], player="native")
            assert result.get("playback_confirmed"), result
            assert cinema.playback.state == PlaybackState.PLAYING
            assert 6 <= result["position"] < 8, result
            fullscreen = result.get("fullscreen_confirmed") is True
            token = cinema.session["token"]
            await cinema.viewer_control(
                {"op": "playback_control", "token": token, "action": "pause"}
            )
            assert cinema.playback.state == PlaybackState.PAUSED
            await cinema.viewer_control(
                {"op": "playback_control", "token": token, "action": "seek", "position": 11}
            )
            sampled = await cinema.player.sample()
            assert (
                sampled["paused"] and not sampled["seeking"] and abs(sampled["position"] - 11) < 0.5
            ), sampled
            assert not await cinema.save(
                {"position": 0, "duration": 0, "error": "fixture_failure"}, "periodic"
            )
            await cinema.close()
            cinema = create()
            result = await cinema.open(item["id"], first["id"], player="native")
            assert result.get("playback_confirmed") and 8 <= result["position"] < 10, result
            await cinema.db("configure", {"poll_seconds": 2, "auto_next": True})
            await cinema.player.control("seek", 17.7)
            deadline = asyncio.get_running_loop().time() + 18
            while not (
                cinema.session
                and cinema.session["target"]["episode"]["id"] == second["id"]
                and cinema.session.get("counted")
            ):
                assert asyncio.get_running_loop().time() < deadline, cinema.playback.snapshot()
                await asyncio.sleep(0.15)
            assert cinema.session["mode"] == "native"
            await cinema.db("configure", {"auto_next": False})
            await cinema.open(item["id"], first["id"], player="native")
            result = await cinema.execute("Включи следующую серию")
            assert (
                result.get("playback_confirmed")
                and cinema.session["mode"] == "native"
                and cinema.session["target"]["episode"]["id"] == second["id"]
            ), result
            capabilities = cinema.playback.snapshot()["source"]["capabilities"]
            assert capabilities.get("play") and capabilities.get("position"), capabilities
            corrupt = folder / "corrupt.mp4"
            corrupt.write_bytes(b"This is not a video.")
            await cinema.db("add", {**entry, "episode": 3, "url": str(corrupt)})
            corrupt_episode = (await cinema.db("detail", item["id"]))["episodes"][2]
            failed = await cinema.open(item["id"], corrupt_episode["id"], player="native")
            assert (
                not failed.get("playback_confirmed")
                and cinema.playback.state == PlaybackState.FAILED
            ), failed
            print(
                json.dumps(
                    {
                        "status": "PASS",
                        "real_avkit": True,
                        "synthetic_silent_media": True,
                        "restored_position": True,
                        "paused_seek": True,
                        "sqlite_restart": True,
                        "next_episode": True,
                        "auto_next": True,
                        "voice_next_keeps_native": True,
                        "fullscreen_confirmed": fullscreen,
                        "live_websites_tested": False,
                        "corrupt_video_rejected": True,
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            await cinema.close()
            memory.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--binary", type=Path)
    args = parser.parse_args()
    if not args.live:
        parser.error("Нужно явно указать --live: проверка откроет тестовое окно AVKit.")
    asyncio.run(check(args.binary))
