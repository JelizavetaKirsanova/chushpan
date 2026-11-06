"""Bounded JSON protocol to an owned AVKit process, never a website stream extractor."""

import asyncio
import contextlib
import hashlib
import json
import sys
import time
from pathlib import Path

from backend.cinema.playback import duration_matches, playback_error, valid_sample
from backend.media.safari import process
from backend.paths import native_helper
from backend.tools.registry import ToolError

ERRORS = {
    "protected_player": "Защищённое видео открывается в приложении сервиса или Safari.",
    "unsupported_source": "AVPlayer не поддерживает этот прямой источник. Выбери Safari.",
    "media_error": "AVPlayer не смог загрузить видео. Проверь источник или выбери Safari.",
    "voice_unavailable": "Эта звуковая дорожка отсутствует в плеере.",
    "seek_interrupted": "Перемотка не завершена; прежний прогресс сохранён.",
    "seek_timeout": "Источник не завершил перемотку за 20 секунд. Прогресс сохранён; "
    "можно продолжить вручную в плеере.",
}


class AVPlayerAdapter:
    mode = "native"

    def __init__(self, settings, *, binary=None, muted=False):
        self.settings, self.binary, self.muted = settings, binary, muted
        self.proc = None
        self.lock = asyncio.Lock()
        self.sequence = 0

    async def helper(self):
        if self.binary:
            return Path(self.binary)
        if bundled := native_helper("cinema_player"):
            return bundled
        if getattr(sys, "frozen", False) or sys.platform != "darwin":
            raise playback_error("native_unavailable", "Нативный плеер недоступен в этой сборке.")
        source = Path(__file__).with_name("avplayer.swift")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        cache = self.settings.data_path / "native"
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / f"cinema-player-{digest}"
        if not target.exists():
            temporary = target.with_suffix(".building")
            try:
                await process(
                    "xcrun",
                    "swiftc",
                    "-O",
                    "-module-cache-path",
                    str(cache / "swift-cache"),
                    str(source),
                    "-o",
                    str(temporary),
                    timeout=90,
                )
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        self.binary = target
        return target

    async def request(self, action, **values):
        async with self.lock:
            if not self.proc or self.proc.returncode is not None:
                raise playback_error("native_closed", "Окно нативного плеера закрыто.")
            self.sequence += 1
            key = str(self.sequence)
            try:
                self.proc.stdin.write(
                    (json.dumps({"id": key, "action": action, **values}) + "\n").encode()
                )
                await self.proc.stdin.drain()
                # Wait for remote range I/O without retrying a submitted seek.
                line = await asyncio.wait_for(
                    self.proc.stdout.readline(), 30 if action == "seek" else 10
                )
                result = json.loads(line)
                if result.get("id") != key:
                    raise ValueError("stale response")
            except (OSError, ValueError, TimeoutError) as exc:
                # Never consume a delayed response as confirmation of the next command.
                if self.proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        self.proc.kill()
                await self.proc.wait()
                raise playback_error(
                    "native_unavailable",
                    f"AVPlayer не подтвердил команду {action}: "
                    f"{type(exc).__name__}. Прежний прогресс сохранён.",
                ) from exc
            if result.get("error"):
                code = result["error"]
                raise playback_error(code, ERRORS.get(code, "Нативный плеер сообщил об ошибке."))
            return result

    async def open(self, target):
        binary = await self.helper()
        self.proc = await asyncio.create_subprocess_exec(
            str(binary),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=65536,
        )
        diagnostic = target.get("diagnostic")

        async def stage(name, status, message):
            if diagnostic:
                await diagnostic(name, status, message)

        await stage("OPEN_NATIVE", "running", "Открываем совместимый прямой источник в AVPlayer.")
        await self.request(
            "open", url=target["link"]["url"], title=target["item"]["title"], muted=self.muted
        )
        await stage("DETECT_PLAYER", "running", "Ожидаем готовности AVPlayer и длительности.")
        deadline = time.monotonic() + 20
        while True:
            state = await self.sample()
            if state.get("ready", 0) >= 1 and valid_sample(state):
                break
            if time.monotonic() >= deadline:
                raise playback_error("native_timeout", "AVPlayer не подтвердил загрузку видео.")
            await asyncio.sleep(0.15)
        position = target["episode"]["position"]
        proof = target.get("direct_verification") or {}
        if not duration_matches(proof.get("duration", 0), state["duration"]):
            raise playback_error(
                "duration_mismatch",
                "Длительность видео не совпала с проверенным полным произведением. "
                "Запуск отменён: источник мог вернуть превью.",
            )
        if position >= state["duration"] and position > 0:
            raise playback_error("duration_mismatch", "Таймкод превышает длительность этого видео.")
        if position or target.get("restart"):
            await stage(
                "RESTORE_POSITION", "running", f"Восстанавливаем {position:.1f} с в AVPlayer."
            )
            await self.control("seek", position)
        voice = target["link"].get("voice")
        note = ""
        if voice:
            try:
                await self.set_voice(voice)
            except ToolError as exc:
                if getattr(exc, "code", None) != "voice_unavailable":
                    raise
                note = " Сохранённая озвучка недоступна; выбери дорожку в плеере."
        if not target.get("autoplay", True):
            return {
                **await self.sample(),
                "mode": "native",
                "playback_confirmed": False,
                "message": "Позиция подготовлена. Нажми ▶ в нативном плеере." + note,
            }
        await stage("PLAY", "running", "Проверяем движение времени AVPlayer.")
        result = await self.control("play")
        await stage("PLAY", "success", "AVPlayer подтвердил движение времени.")
        try:
            await self.control("fullscreen")
            return {
                **result,
                "mode": "native",
                "fullscreen_confirmed": True,
                "message": result["message"] + note,
            }
        except ToolError as exc:
            if getattr(exc, "code", None) != "fullscreen_unavailable":
                raise
            return {
                **result,
                "mode": "native",
                "fullscreen_confirmed": False,
                "result_status": "partial_success",
                "message": result["message"] + " Полный экран не подтверждён." + note,
            }

    async def sample(self):
        return await self.request("status")

    async def control(self, action, position=0):
        if action not in {"play", "pause", "seek", "fullscreen"}:
            raise playback_error("unsupported_command", "AVPlayer не поддерживает эту команду.")
        before = await self.sample()
        await self.request(action, **({"position": position} if action == "seek" else {}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = await self.sample()
            if state.get("closed"):
                raise playback_error("native_closed", "Окно нативного плеера закрыто.")
            success = (
                (
                    action == "play"
                    and valid_sample(state)
                    and state.get("paused") is False
                    and state["position"] > before["position"] + 0.05
                )
                or (action == "pause" and state.get("paused") is True and valid_sample(state))
                or (
                    action == "seek"
                    and valid_sample(state)
                    and abs(state["position"] - position) < 2
                )
                or (action == "fullscreen" and state.get("fullscreen") is True)
            )
            if success:
                return {
                    **state,
                    "playback_confirmed": action == "play",
                    "result_status": "success",
                    "message": "Воспроизведение подтверждено в AVPlayer."
                    if action == "play"
                    else "Фильм на паузе."
                    if action == "pause"
                    else "Действие AVPlayer подтверждено.",
                }
            await asyncio.sleep(0.15)
        raise playback_error(
            "fullscreen_unavailable" if action == "fullscreen" else "not_confirmed",
            "AVPlayer не подтвердил действие. Прежний прогресс сохранён.",
        )

    async def set_voice(self, voice):
        return await self.request("voice", voice=voice)

    async def detach(self):
        if not self.proc:
            return
        if self.proc.returncode is None:
            with contextlib.suppress(Exception):
                await self.request("close")
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        self.proc = None
