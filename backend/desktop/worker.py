"""One supervised assistant process; private controls share the existing HUD server."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import signal
from pathlib import Path

from fastapi import HTTPException, Request

from backend.config import Settings
from backend.hud.bus import HudBus
from backend.hud.server import HudServer


class DesktopEngine:
    def __init__(self, settings, token, *, smoke=False):
        self.s, self.token, self.smoke = settings, token, smoke
        self.bus = HudBus()
        self.enabled = asyncio.Event()
        self.changed = asyncio.Event()
        self.loop = None
        self.mic = None
        self.components = {}
        self.error = ""
        self.ready = False
        self.ollama = None

    async def ensure_ollama(self):
        from urllib.parse import urlparse

        import httpx

        parsed = urlparse(self.s.llm.host)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return
        async with httpx.AsyncClient(timeout=1) as client:
            try:
                response = await client.get(self.s.llm.host.rstrip("/") + "/api/tags")
                if response.is_success:
                    self.components["ollama"] = "ready"
                    return
            except httpx.HTTPError:
                pass
        program = shutil.which("ollama")
        if program is None:
            candidate = Path("/Applications/Ollama.app/Contents/Resources/ollama")
            program = str(candidate) if candidate.exists() else None
        if not program:
            self.components["ollama"] = "unavailable"
            return
        if self.ollama is None or self.ollama.returncode is not None:
            self.ollama = await asyncio.create_subprocess_exec(program, "serve")
        self.components["ollama"] = "starting"

    def routes(self, app):
        @app.post("/_desktop")
        async def control(request: Request):
            if not secrets.compare_digest(request.headers.get("X-Chushpan-Token", ""), self.token):
                raise HTTPException(403)
            if request.headers.get("origin") is not None:
                raise HTTPException(403)
            value = await request.json()
            if not isinstance(value, dict):
                raise HTTPException(400)
            op = value.get("op")
            if op == "status":
                return self.snapshot()
            if op == "listen" and type(value.get("enabled")) is bool:
                if self.smoke and value["enabled"]:
                    raise HTTPException(409, "Микрофон запрещён в проверочном режиме")
                self.set_listening(value["enabled"])
                return self.snapshot()
            raise HTTPException(400)

    def set_listening(self, enabled):
        if enabled:
            self.enabled.set()
        else:
            self.enabled.clear()
            if self.mic:
                self.mic.stop()
            if self.loop:
                self.loop.command("stop")
                self.loop._close_session()
                self.loop.microphone_running = False
                self.loop._publish_voice()
        self.changed.set()

    def snapshot(self):
        from backend.desktop.platform import process_usage, resident_mb

        state = "loading" if not self.ready else "ready"
        if self.error:
            state = "error"
        elif self.loop and self.loop.microphone_running:
            state = {
                "sleeping": "listening",
                "thinking": "processing",
                "speaking": "answering",
            }.get(self.loop.state.state.value, "listening")
        elif self.enabled.is_set():
            state = "loading"
        elif self.ready:
            state = "paused"
        return {
            "state": state,
            "listening": bool(self.loop and self.loop.microphone_running),
            "requested": self.enabled.is_set(),
            "components": self.components,
            "error": self.error,
            "memory_mb": resident_mb(),
            **process_usage(os.getpid()),
        }

    async def capture(self, mic, loop, warmup):
        """Stop closes the input immediately; waiting uses events, never audio polling."""
        self.loop, self.mic, self.ready = loop, mic, True
        self.components.update(
            ui="ready", voice="on_demand", whisper="on_demand", wake="on_demand", model="on_demand"
        )
        loaded = False
        original_command = loop.command

        def command(name):
            if name == "wake" and not self.enabled.is_set():
                self.bus.publish(
                    {"type": "notice", "message": "Включи микрофон в меню Чушпана.", "error": False}
                )
                return
            original_command(name)

        self.bus.on_command = command
        while True:
            await self.enabled.wait()
            try:
                if not loaded:
                    self.components.update(voice="loading", whisper="loading", wake="loading")
                    await warmup()
                    loaded = True
                if not self.enabled.is_set():
                    continue
                mic.start()
                self.error = ""
                loop.microphone_running = True
                loop._publish_voice()
                frames = mic.frames().__aiter__()
                while self.enabled.is_set():
                    self.changed.clear()
                    next_frame = asyncio.create_task(anext(frames))
                    changed = asyncio.create_task(self.changed.wait())
                    try:
                        done, _ = await asyncio.wait(
                            (next_frame, changed), return_when=asyncio.FIRST_COMPLETED
                        )
                        if next_frame in done and self.enabled.is_set():
                            yield next_frame.result()
                    finally:
                        next_frame.cancel()
                        changed.cancel()
                        await asyncio.gather(next_frame, changed, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = f"Голосовой движок: {type(exc).__name__}. Проверь модели и разрешения."
                self.enabled.clear()
                self.bus.publish({"type": "notice", "message": self.error, "error": True})
            finally:
                mic.stop()
                mic.flush()
                loop.microphone_running = False
                loop._publish_voice()


async def run_worker(token: str, endpoint: Path, *, smoke=False, parent=0):
    from backend.instance import InstanceLock

    settings = Settings()
    settings.server.host, settings.server.port = "127.0.0.1", 0
    settings.hud.window = settings.hud.open_on_start = False
    settings.briefing.enabled = False
    lock = InstanceLock(settings.data_path / "jarvis.lock")
    if not lock.acquire():
        raise RuntimeError("Голосовой движок уже работает с этой базой.")
    engine = DesktopEngine(settings, token, smoke=smoke)
    server = HudServer(settings, engine.bus, native=engine)
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)

    async def watch_parent():
        while True:
            await asyncio.sleep(1)
            if parent and os.getppid() != parent:
                task.cancel()
                return

    watcher = asyncio.create_task(watch_parent())
    try:
        if not await server.start():
            raise RuntimeError("Не удалось запустить интерфейс.")
        endpoint.write_text(json.dumps({"url": server.url, "pid": os.getpid()}))
        endpoint.chmod(0o600)
        if smoke:
            engine.ready = True
            engine.components = {"ui": "ready", "microphone": "disabled_for_test"}
            engine.bus.publish({"type": "state", "state": "sleeping"})
            engine.bus.publish({"type": "boot", "steps": [], "progress": 100})
            engine.bus.publish(
                {
                    "type": "hello",
                    "name": "Чушпан",
                    "language": "ru-RU",
                    "model": "Проверка приложения",
                    "tools": [],
                    "wake_word": True,
                }
            )
            await asyncio.Event().wait()
        else:
            from backend.desktop.storage import load_preferences
            from backend.paths import USER_ROOT
            from backend.voice import _run_voice

            prefs = load_preferences(USER_ROOT)
            if prefs.energy_saving:
                settings.resources.profile = "economy"
            await _run_voice(settings, resume=prefs.restore_session, briefing=False, desktop=engine)
    except asyncio.CancelledError:
        pass
    finally:
        watcher.cancel()
        if engine.ollama and engine.ollama.returncode is None:
            engine.ollama.terminate()
            try:
                await asyncio.wait_for(engine.ollama.wait(), 3)
            except TimeoutError:
                engine.ollama.kill()
                await engine.ollama.wait()
        await server.stop()
        endpoint.unlink(missing_ok=True)
        lock.release()
