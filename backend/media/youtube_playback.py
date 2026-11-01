"""Safari playback verification, independent from the YouTube Music web app."""

from __future__ import annotations

import asyncio
from pathlib import Path

from backend.config import ROOT_DIR, Settings
from backend.media.safari import AccessibilityApp
from backend.media.youtube_api import watch_url
from backend.tools.registry import ToolError


class SafariPlayback(AccessibilityApp):
    bundle_id = "com.apple.Safari"

    @property
    def source(self) -> Path:
        return ROOT_DIR / "backend/media/youtube_safari.swift"

    def __init__(self, s: Settings):
        super().__init__(s, "Safari")

    async def current_playlist(self) -> str:
        return (await self.request("youtube_playlist"))["playlist_id"]

    async def state(self, target: dict, action="state") -> dict:
        if action not in {"state", "play", "pause"}:
            raise ToolError("Недопустимое действие Safari.")
        return await self.request(
            "youtube_" + action,
            video_id=target["video_id"],
            window=target["window"],
            tab=target["tab"],
        )

    async def launch(self, video: dict) -> dict:
        watch_url(video["id"])
        cfg = self.s.youtube_video
        uuid = (
            (cfg.external_uuid or self.s.night.external_uuid)
            if cfg.preferred_screen == "external"
            else None
        )
        opened = await self.request(
            "youtube_open", video_id=video["id"], external_uuid=uuid, fullscreen=cfg.fullscreen
        )
        target = {"video_id": video["id"], "window": opened["window"], "tab": opened["tab"]}
        warnings = list(opened.get("warnings", []))
        if cfg.preferred_screen == "external" and not uuid:
            warnings.append("Внешний экран не выбран по UUID; видео осталось на текущем экране.")
        state = {}
        # Bounded, on-demand observation only. Never a background poll or another API call.
        for _ in range(6):
            state = await self.state(target, "play")
            if (
                state.get("id") == video["id"]
                and state.get("state") == "playing"
                and not state.get("ad")
            ):
                break
            await asyncio.sleep(0.5)
        verified = (
            state.get("id") == video["id"]
            and state.get("state") == "playing"
            and not state.get("ad")
        )
        if verified and not state.get("autoplay_off"):
            await self.state(target, "pause")
            verified = False
        return {
            "target": target,
            "playing": bool(verified),
            "state": state,
            "warnings": warnings,
        }

    async def control(self, target: dict, action: str) -> dict:
        expected = "paused" if action == "pause" else "playing"
        state = await self.state(target, "pause" if action == "pause" else "play")
        for _ in range(4):
            if (
                state.get("id") == target["video_id"]
                and state.get("state") == expected
                and not state.get("ad")
            ):
                if action != "pause" and not state.get("autoplay_off"):
                    await self.state(target, "pause")
                    raise ToolError("Выключи автопроигрывание YouTube вручную, затем повтори.")
                return state
            await asyncio.sleep(0.25)
            state = await self.state(target)
        raise ToolError(
            "Safari не подтвердил воспроизведение. Проверь вкладку и запрет автозапуска."
        )
