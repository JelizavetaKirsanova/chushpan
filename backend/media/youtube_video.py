"""Pause/resume the real YouTube player in Safari through the shared native AX bridge."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from backend.config import Settings
from backend.media.safari import AccessibilityApp, descendants, label
from backend.tools.registry import ToolError

_HINT = re.compile(r'для активации кнопки ["«“](.+?)["»”] нажмите [kк]', re.I)


def button_state(node: dict) -> str | None:
    name = label(node).strip()
    hint = _HINT.fullmatch(name)
    if hint:
        name = hint.group(1)
    name = re.sub(r"\s*\([kк]\)$", "", name, flags=re.I).casefold()
    if name in ("пауза", "приостановить", "pause"):
        return "playing"
    if name in ("воспроизвести", "смотреть", "play"):
        return "paused"
    return None


def has_shortcut(node: dict) -> bool:
    name = label(node).strip()
    return bool(_HINT.fullmatch(name) or re.search(r"\([kк]\)$", name, re.I))


def video_key(url: str) -> str | None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() not in ("youtube.com", "www.youtube.com", "m.youtube.com")
        or parsed.username
        or parsed.password
    ):
        return None
    if parsed.path == "/watch":
        return (parse_qs(parsed.query).get("v") or [None])[0]
    if parsed.path.startswith(("/shorts/", "/live/")):
        return parsed.path.split("/")[2] or None
    return None


@dataclass
class VideoPlayer:
    page: dict
    button: dict | None
    key: str
    main: bool = False

    @property
    def state(self) -> str:
        if self.button is None:
            return "unknown"
        return button_state(self.button) or "unknown"


def video_players(nodes: list[dict]) -> list[VideoPlayer]:
    windows = [node for node in nodes if node.get("role") in ("AXWindow", "AXDialog")]
    players = []
    for page in nodes:
        key = video_key(page.get("url", ""))
        if page.get("role") != "AXWebArea" or not key:
            continue
        items = descendants(nodes, page)
        # Exclude recommendations and unrelated controls elsewhere on the page.
        groups = [
            node
            for node in items
            if "проигрыватель youtube" in label(node).casefold()
            or "youtube video player" in label(node).casefold()
        ]
        controls = descendants(items, groups[0]) if groups else items
        buttons = [
            node
            for node in controls
            if node.get("role") == "AXButton"
            and node.get("enabled") != "0"
            and button_state(node) is not None
        ]
        shortcut = [node for node in buttons if has_shortcut(node)]
        buttons = shortcut or (buttons if groups else [])
        main = any(
            page["path"][: len(window["path"])] == window["path"]
            and (window.get("main") == "1" or window.get("focused") == "1")
            for window in windows
        )
        players.append(VideoPlayer(page, buttons[0] if len(buttons) == 1 else None, key, main))
    return players


class SafariYouTube(AccessibilityApp):
    bundle_id = "com.apple.Safari"

    def __init__(self, s: Settings) -> None:
        super().__init__(s, "Safari")
        self._last_key: str | None = None

    async def snapshot(self) -> list[dict]:
        result = await self.request("snapshot")
        if result.get("truncated"):
            raise ToolError(
                "Safari вернул неполную страницу. Оставь нужный ролик в активной вкладке."
            )
        return result.get("nodes", [])

    async def has_active_video(self) -> bool:
        status = await self.request("status")
        if not all(status.get(key) for key in ("trusted", "running", "frontmost")):
            return False
        return bool(video_players(await self.snapshot()))

    def choose(self, players: list[VideoPlayer]) -> VideoPlayer:
        if not players:
            raise ToolError("Открой нужный ролик YouTube в активной вкладке Safari.")
        main = [player for player in players if player.main]
        previous = [player for player in players if player.key == self._last_key]
        candidates = main or previous or players
        if len(candidates) != 1:
            raise ToolError(
                "В Safari открыто несколько роликов. Сделай окно с нужным видео активным."
            )
        player = candidates[0]
        if player.button is None:
            raise ToolError("Кнопка плеера YouTube недоступна. Открой ролик в Safari и повтори.")
        return player

    async def control(self, action: str) -> dict:
        if action not in ("pause", "stop", "resume"):
            raise ToolError(
                "Для видео YouTube доступны только пауза и продолжение воспроизведения."
            )
        desired = "playing" if action == "resume" else "paused"
        player = self.choose(video_players(await self.snapshot()))
        changed = player.state != desired
        if changed:
            button = player.button
            expected = {
                key: button[key]
                for key in ("role", "title", "description", "identifier")
                if key in button
            }
            await self.request(
                "press",
                path=button["path"],
                expected=expected,
                scope={
                    "path": player.page["path"],
                    "expected": {"role": "AXWebArea", "url": player.page["url"]},
                },
            )
            deadline = time.monotonic() + 3
            while True:
                same = [p for p in video_players(await self.snapshot()) if p.key == player.key]
                if len(same) != 1:
                    raise ToolError("Вкладка YouTube сменилась. Действие не подтверждено.")
                if same[0].state == desired:
                    break
                if time.monotonic() >= deadline:
                    raise ToolError("Плеер YouTube не подтвердил изменение воспроизведения.")
                await asyncio.sleep(0.1)
        self._last_key = player.key
        return {
            "ok": True,
            "provider": "youtube_video",
            "app": "Safari",
            "action": action,
            "state": desired,
            "changed": changed,
        }
