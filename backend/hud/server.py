"""HTTP + WebSocket server for the HUD, running inside the assistant's event loop."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import webbrowser
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from backend.config import ROOT_DIR, Settings
from backend.hud.bus import HudBus

log = logging.getLogger(__name__)

DIST_DIR = ROOT_DIR / "frontend" / "dist"
DEV_PORT = 5173  # `npm run dev` (Vite)
COMMANDS = {"wake", "sleep", "stop"}
# Requests answered by handlers registered on the bus, and the type of their value.
REQUESTS: dict[str, type] = {
    "music_settings": str,
    "autostart": bool,
    "export": type(None),
    "history": int,
    "history_search": dict,
    "dashboard": dict,
    "chat_message": dict,
    "dialogue_settings": dict,
    "voice_settings": dict,
    "voice_status": type(None),
    "performance_stats": int,
    "performance_query": dict,
    "performance_detail": str,
    "automations": dict,
    "settings_center": dict,
    "devices": dict,
}
RECONNECT_S = 1.5  # an already open HUD page reconnects within this; then no new tab

NOT_BUILT = """<!doctype html><html lang="ru"><meta charset="utf-8">
<title>Чушпан · Ассистент</title>
<body style="background:#02050a;color:#7af4ff;font:16px ui-monospace,monospace;padding:3rem">
<p>Интерфейс ещё не собран. Выполните:</p>
<pre>npm --prefix frontend install && npm --prefix frontend run build</pre>
<p>Затем обновите страницу.</p></body></html>"""


def allowed_origins(host: str, port: int) -> set[str]:
    """Pages allowed to connect. Any site open in the browser can try ws://127.0.0.1;
    without this check it could read the conversation or send commands."""
    hosts = {host, "127.0.0.1", "localhost"}
    return {f"http://{h}:{p}" for h in hosts for p in (port, DEV_PORT)}


def create_app(bus: HudBus, origins: set[str], dist: Path = DIST_DIR, native=None) -> FastAPI:
    # FastAPI reads the handlers' annotations, so its names are imported at module level.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    if native is not None:
        native.routes(app)

    @app.websocket("/ws")
    async def events(ws: WebSocket) -> None:
        origin = ws.headers.get("origin")
        if origin is not None and origin not in origins:
            log.warning("HUD connection refused from origin %s", origin)
            await ws.close(code=1008)
            return
        await ws.accept()
        queue = bus.subscribe()

        async def send() -> None:
            for event in bus.snapshot():
                await ws.send_text(json.dumps(event, ensure_ascii=False))
            while True:
                event = await queue.get()
                await ws.send_text(json.dumps(event, ensure_ascii=False))

        async def receive() -> None:
            while True:
                try:
                    msg = json.loads(await ws.receive_text())
                except (ValueError, TypeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("type")
                if kind in COMMANDS:
                    bus.command(kind)
                elif kind in REQUESTS and type(msg.get("value")) is REQUESTS[kind]:
                    bus.request(kind, msg.get("value"))

        tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception()  # retrieved, or asyncio logs it at exit
                if exc and not isinstance(exc, WebSocketDisconnect):
                    log.debug("HUD client dropped: %r", exc)
        finally:
            for task in tasks:
                task.cancel()
            bus.unsubscribe(queue)

    if (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="hud")
    else:

        @app.get("/")
        async def not_built() -> HTMLResponse:
            return HTMLResponse(NOT_BUILT)

    return app


class HudServer:
    """uvicorn on an already-bound socket, without its signal handlers: Ctrl+C belongs
    to the voice loop, and a busy port only turns the HUD off."""

    def __init__(self, s: Settings, bus: HudBus, native=None) -> None:
        self.s = s
        self.bus = bus
        self.native = native
        self._server = None
        self._task: asyncio.Task | None = None

    @property
    def url(self) -> str:
        return f"http://{self.s.server.host}:{self.s.server.port}"

    async def start(self) -> bool:
        import uvicorn

        host, port = self.s.server.host, self.s.server.port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            sock.close()
            log.warning("HUD off: cannot listen on %s:%s (%s)", host, port, exc.strerror)
            return False

        class Server(uvicorn.Server):
            def capture_signals(self):
                return contextlib.nullcontext()

        if port == 0:
            port = sock.getsockname()[1]
            self.s.server.port = port
        app = create_app(self.bus, allowed_origins(host, port), native=self.native)
        config = uvicorn.Config(app, log_level="warning", lifespan="off", ws_ping_interval=None)
        self._server = Server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]))
        while not self._server.started:
            if self._task.done():
                return False
            await asyncio.sleep(0.01)
        return True

    async def stop(self) -> None:
        if self._server is None or self._task is None:
            return
        self._server.should_exit = True
        # Open WebSockets would hold the shutdown for the default 5 s+.
        self._server.force_exit = True
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(self._task, 2)


async def open_hud(bus: HudBus, url: str, window=None) -> None:
    """Open the requested native window, even if a browser tab has reconnected.

    Reuse an existing page only in browser mode or if the native window fails.
    """
    await asyncio.sleep(RECONNECT_S)
    if window is not None and await window.open():
        return
    if bus.clients:
        return
    await asyncio.to_thread(webbrowser.open, url)
