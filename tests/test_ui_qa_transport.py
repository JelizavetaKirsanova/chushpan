"""Real HUD transport and SQLite with temporary data; native calls are forbidden."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from backend.analytics import Analytics
from backend.config import Settings
from backend.hud.bus import HudBus
from backend.hud.server import create_app
from backend.memory.library import MemoryLibrary
from backend.memory.store import MemoryStore


def test_hud_websocket_memory_and_analytics_round_trip_and_restart(tmp_path):
    database = tmp_path / "qa.sqlite"
    native = AsyncMock(side_effect=AssertionError("QA must not access physical devices"))
    services = {}

    def application():
        bus = HudBus()
        app = create_app(bus, {"http://testserver"})

        @asynccontextmanager
        async def lifespan(_):
            memory = MemoryStore(database)
            settings = Settings(location={"timezone": "Europe/Tallinn"})
            tools = SimpleNamespace(desktop=SimpleNamespace(bridge=SimpleNamespace(request=native)))
            library = MemoryLibrary(memory, settings, bus)
            analytics = Analytics(settings, memory, tools, bus)
            services.update(memory=memory, analytics=analytics)
            try:
                yield
            finally:
                await library.close()
                await analytics.close()
                memory.close()

        app.router.lifespan_context = lifespan
        return app

    def request(ws, channel, identity, **value):
        ws.send_json({"type": channel, "value": {"request_id": identity, **value}})
        for _ in range(20):
            reply = ws.receive_json()
            if reply.get("operation_id") == identity and reply.get("busy") is False:
                return reply
        raise AssertionError("Expected a completed HUD response")

    with TestClient(application()) as client:
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
            first = request(ws, "analytics", "disabled", op="query")
            assert first["data"]["policy"]["enabled"] is False
            assert first["data"]["summary"]["events"] == 0
            saved = request(
                ws, "memory_library", "save", op="add", content="QA: тёплый свет вечером"
            )
            assert saved["data"]["total"] == 1
            assert saved["data"]["items"][0]["content"] == "QA: тёплый свет вечером"
            enabled = request(
                ws,
                "analytics",
                "enable",
                op="policy",
                value={"enabled": True, "categories": ["assistant"]},
            )
            assert enabled["data"]["policy"]["enabled"]

            async def record():
                services["analytics"].record(
                    "assistant.request", metadata={"kind": "text"}, device="qa-client"
                )
                await services["analytics"].flush()

            client.portal.call(record)
            measured = request(ws, "analytics", "filter", op="query", device="qa-client")
            assert measured["data"]["summary"]["count"] == 1
            exported = request(ws, "analytics", "export", op="export", format="json")
            assert len(json.loads(exported["download"]["content"])["events"]) == 1
            rejected = request(ws, "analytics", "invalid", op="query", period="bad-period")
            assert "error" in rejected and "data" not in rejected
            recovered = request(ws, "analytics", "recover", op="query", period="today")
            assert recovered["data"]["summary"]["count"] == 1
            request(ws, "analytics", "disable", op="policy", value={"enabled": False})

    with TestClient(application()) as client:
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
            restored = request(ws, "memory_library", "restore-memory", op="list")
            assert restored["data"]["total"] == 1
            analytics = request(ws, "analytics", "restore-policy", op="query")
            assert analytics["data"]["policy"]["enabled"] is False
            assert analytics["data"]["summary"]["count"] == 1
    native.assert_not_awaited()
