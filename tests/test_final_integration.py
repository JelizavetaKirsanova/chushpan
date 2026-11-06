"""All three changes share real HUD/SQLite; native and BLE access is forbidden."""

import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.brain.conversation import Conversation
from backend.config import Settings
from backend.hud.bus import HudBus
from backend.hud.server import create_app
from backend.mac import MacDesktop
from backend.memory.profile import ProfileMemory
from backend.memory.store import MemoryStore
from backend.personal_modules import PersonalModules
from backend.settings_center import SettingsCenter
from backend.sync.store import SyncStore
from backend.tools import build_registry
from tests.test_critical_fixes import NoModel
from tests.test_devices import devices as devices
from tests.test_devices import request
from tests.test_led import rig as rig


def test_legacy_migration_shared_websocket_and_restart(tmp_path, monkeypatch):
    path = tmp_path / "integration.sqlite"
    with sqlite3.connect(path) as legacy:
        legacy.execute(
            "CREATE TABLE facts (id INTEGER PRIMARY KEY, content TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        legacy.execute("INSERT INTO facts VALUES (1, 'Старый факт', '2026-01-01T00:00:00')")
    native = AsyncMock(side_effect=AssertionError("No physical devices in integration QA"))
    ble = Mock(side_effect=AssertionError("No BLE transport in integration QA"))
    monkeypatch.setattr("backend.led.BleakClient", ble)
    services = {}

    def application():
        bus = HudBus()
        app = create_app(bus, {"http://testserver"})

        @asynccontextmanager
        async def lifespan(_):
            store = MemoryStore(path)
            settings = Settings(language="ru-RU", tools={"enabled": ["system", "memory"]})
            desktop = MacDesktop(settings)
            desktop.bridge = SimpleNamespace(request=native)
            tools = build_registry(settings, store=store, desktop=desktop)
            conv = Conversation(settings, NoModel(), store, tools=tools)
            tools.devices.bind(bus, conv)
            modules = PersonalModules(settings, store, tools, bus)
            center = SettingsCenter(settings, store, conv, bus, startup_permissions=False)
            services.update(store=store, tools=tools, conv=conv, center=center)
            try:
                yield
            finally:
                await center.close()
                await modules.close()
                await tools.devices.close()
                await tools.automations.close()
                store.close()

        app.router.lifespan_context = lifespan
        return app

    def receive(ws, channel, predicate):
        for _ in range(100):
            event = ws.receive_json()
            if event.get("type") == channel and predicate(event):
                assert not event.get("error"), event
                return event
        raise AssertionError("HUD response missing")

    def send(ws, channel, **payload):
        identity = uuid4().hex
        ws.send_json({"type": channel, "value": {"request_id": identity, **payload}})
        return receive(
            ws, channel, lambda e: e.get("operation_id") == identity and not e.get("busy")
        )

    with TestClient(application()) as client:
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
            assert send(ws, "memory_library", op="list")["data"]["total"] == 1
            saved = send(ws, "memory_library", op="add", content="Я изучаю алгоритмы")
            assert saved["data"]["total"] == 2
            palette = send(
                ws, "devices", op="color_save", value={"name": "Лавандовый", "rgb": [180, 80, 255]}
            )
            color_id = palette["result"]["saved_color_id"]
            send(ws, "devices", op="color_preview", id="led-configured", color="#B450FF")
            ws.send_json(
                {
                    "type": "settings_center",
                    "value": {"op": "save", "category": "memory", "value": {"automatic": "off"}},
                }
            )
            settings = receive(ws, "settings_center", lambda e: bool(e.get("message")))
            assert settings["values"]["memory"]["automatic"] == "off"
            send(ws, "analytics", op="policy", value={"enabled": True, "categories": ["devices"]})

            async def seed_history_and_peer():
                store = services["store"]
                cid = store.new_conversation()
                store.add(cid, "user", "Синтетическая история интеграции")
                SyncStore(store).trust(
                    {"id": "synthetic-peer", "name": "QA", "kind": "phone", "key": {}}
                )
                return cid

            cid = client.portal.call(seed_history_and_peer)

    # Fresh app/services/connection on the same file: migrations must be idempotent.
    with TestClient(application()) as client:
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
            facts = send(ws, "memory_library", op="list")["data"]
            assert {f["content"] for f in facts["items"]} == {"Старый факт", "Я изучаю алгоритмы"}
            devices = send(ws, "devices", op="refresh")
            assert devices["user_colors"][0]["id"] == color_id
            assert devices["user_colors"][0]["hex"] == "#B450FF"
            assert services["tools"].devices.colors.preview("led-configured") == "#B450FF"
            assert services["center"].value("memory")["automatic"] == "off"
            assert ProfileMemory(services["store"]).relevant("алгоритмы")
            policy = send(ws, "analytics", op="query")["data"]["policy"]
            assert policy["enabled"] and policy["categories"] == ["devices"]
            store = services["store"]
            assert store.recent(cid, 10)[0]["content"] == "Синтетическая история интеграции"
            assert SyncStore(store).peer("synthetic-peer")["name"] == "QA"
            assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    native.assert_not_awaited()
    ble.assert_not_called()


@pytest.mark.parametrize(
    "guard", ["capability", "agent", "location", "access", "session", "target"]
)
def test_rgb_grant_never_bypasses_physical_device_guards(devices, guard):
    async def run():
        r = devices
        assert r.service.features(r.service.store.get("led-configured"))["color"]
        if guard == "capability":
            r.conv.device = replace(r.conv.device, capabilities=frozenset())
        elif guard == "agent":
            r.tools.mac_agent = None
        elif guard == "location":
            device = r.service.store.get("led-configured")
            r.service.store.save(device.model_copy(update={"place_id": "dorm"}))
        elif guard == "access":
            r.store.set_preference("settings", "access", {"disabled_tools": ["led_control"]})
        elif guard == "session":
            r.conv.device = replace(r.conv.device, authenticated=False)
        elif guard == "target":
            r.conv.device = replace(r.conv.device, target_agent_id="remote-agent")
        result = await request(
            r, op="act", id="led-configured", action="led", parameters={"color": "#B450FF"}
        )
        assert result.get("error") or result.get("result", {}).get("result_status") == "failed"
        r.factory.assert_not_called()
        assert not r.service.store.get("led-configured").sent

    import asyncio

    asyncio.run(run())
