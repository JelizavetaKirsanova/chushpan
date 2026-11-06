"""Opt-in Apple HLS test stream through the production AVKit adapter.

This proves streaming/control capability, not a full movie or third-party site.
No microphone, physical devices, login, media download or user database access.
"""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from backend.cinema.native_player import AVPlayerAdapter
from backend.cinema.sources import PublicHTTP, WebsiteAdapter
from backend.config import Settings

URL = (
    "https://devstreaming-cdn.apple.com/videos/streaming/examples/"
    "bipbop_adv_example_hevc/master.m3u8"
)


async def check():
    report = {
        "stream": "Apple Bip Bop HLS",
        "url": URL,
        "engine": "AVKit",
        "full_movie": False,
        "local_fixture": False,
        "playback_confirmed": False,
    }
    with tempfile.TemporaryDirectory(prefix="chushpan-hls-") as folder:
        adapter = AVPlayerAdapter(Settings(data_dir=Path(folder)), muted=True)
        try:
            source = {
                "adapter": "direct",
                "base_url": "https://devstreaming-cdn.apple.com",
                "embed_allowed": True,
            }
            resolved = await WebsiteAdapter(source, PublicHTTP()).resolve({"url": URL})
            assert resolved["embedded"], resolved
            result = await adapter.open(
                {
                    "link": {"url": URL},
                    "item": {"title": "Apple · тест HLS"},
                    "episode": {"position": 0},
                }
            )
            assert result.get("playback_confirmed"), result
            before = await adapter.sample()
            await asyncio.sleep(1.2)
            after = await adapter.sample()
            assert after["position"] > before["position"] + 0.5, (before, after)
            await adapter.control("pause")
            seek = await adapter.control("seek", 20)
            assert abs(seek["position"] - 20) < 2
            report.update(
                playback_confirmed=True,
                duration=after["duration"],
                currentTime_before=before["position"],
                currentTime_after=after["position"],
                pause=True,
                seek=True,
                fullscreen=result.get("fullscreen_confirmed", False),
            )
        except Exception as exc:
            report.update(error=type(exc).__name__, message=str(exc))
        finally:
            await adapter.detach()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.live:
        parser.error("Нужен --live: откроется временное окно AVKit с тестовым потоком Apple.")
    report = asyncio.run(check())
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["playback_confirmed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
