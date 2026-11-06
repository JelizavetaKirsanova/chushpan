"""QA regressions: temporary SQLite, mock device actions and ephemeral loopback TLS only."""

import asyncio
import json
import socket
import ssl
import sys
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend import analytics
from backend.analytics import Analytics, CollectionPolicy
from backend.center import Center
from backend.config import Settings
from backend.hud.bus import HudBus
from backend.memory.store import MemoryStore
from backend.security import Risk
from backend.sync.identity import KeychainIdentity, certificates
from backend.tools.registry import Tool, ToolRegistry
from tests.test_device_sync import identity, signed, trust
from tests.test_device_sync import setup as sync_setup
from tests.test_personal_analytics import setup as analytics_setup


def test_nested_device_tools_are_counted_once_and_separate_commands_remain_separate(tmp_path):
    async def run():
        calls = []

        async def leaf():
            calls.append(True)
            return {"level": 50, "result_status": "success"}

        async def wrapper():
            return json.loads(await tools.call("set_brightness", {}))

        tools = ToolRegistry(
            [
                Tool("device_command", "test wrapper", wrapper, risk=Risk.READ),
                Tool("set_brightness", "test leaf", leaf, risk=Risk.READ),
            ]
        )
        tools.desktop = SimpleNamespace(bridge=SimpleNamespace(request=AsyncMock()))
        memory = MemoryStore(tmp_path / "analytics.sqlite")
        service = Analytics(Settings(), memory, tools, HudBus())
        service.policy = CollectionPolicy(enabled=True)
        try:
            await tools.call("device_command", {})
            await tools.call("device_command", {})
            await tools.call("set_brightness", {})
            await service.flush()
            summary = service.snapshot()["summary"]
            assert len(calls) == 3
            commands = {c["name"]: c for c in summary["commands"]}
            assert commands["device_command"]["count"] == 2
            assert commands["set_brightness"]["count"] == 1
            assert all(c["category"] == "devices.command" for c in commands.values())
        finally:
            await service.close()
            memory.close()

    asyncio.run(run())


def test_analytics_legacy_utc_traces_and_disabled_collection_preserve_saved_events(tmp_path):
    async def run():
        memory, service = analytics_setup(tmp_path)
        try:
            service.policy = CollectionPolicy(enabled=True)
            created = datetime.now(UTC).replace(tzinfo=None).isoformat()
            trace = {
                "request_id": "qa-utc",
                "created_at": created,
                "kind": "text",
                "status": "completed",
                "total_s": 1,
                "first_content_s": 0,
                "metadata": {},
                "stages": [],
            }
            service.observe_trace(trace)
            service.observe_trace(trace)  # same trace, not another request
            await service.flush()
            row = memory.db.execute("SELECT timestamp FROM analytics_events").fetchone()
            assert row[0] == datetime.fromisoformat(created).replace(tzinfo=UTC).timestamp()
            assert service.snapshot()["summary"]["count"] == 1
            assert service.snapshot()["summary"]["mean_s"] == 0
            await service.handle({"op": "policy", "value": {"enabled": False}})
            service.observe_trace({**trace, "request_id": "should-not-be-stored"})
            await service.flush()
            exported = await service.handle({"op": "export", "format": "json"})
            assert len(json.loads(exported["download"]["content"])["events"]) == 1
            assert not service.snapshot()["policy"]["enabled"]
        finally:
            await service.close()
            memory.close()

    asyncio.run(run())


def test_analytics_reports_refuse_silent_truncation(tmp_path, monkeypatch):
    async def run():
        memory, service = analytics_setup(tmp_path)
        try:
            service.policy = CollectionPolicy(enabled=True)
            for _ in range(3):
                service.record("assistant.request")
            await service.flush()
            monkeypatch.setattr(analytics, "MAX_EVENTS", 2)
            with pytest.raises(ValueError, match="Сократи период"):
                await service.handle({"op": "query"})
            with pytest.raises(ValueError, match="Сократи период"):
                await service.handle({"op": "export"})
            assert memory.db.execute("SELECT count(*) FROM analytics_events").fetchone()[0] == 3
        finally:
            await service.close()
            memory.close()

    asyncio.run(run())


def test_analytics_app_sampling_recovers_error_without_changing_privacy(tmp_path, monkeypatch):
    async def run():
        memory, service = analytics_setup(tmp_path)
        service.sampler.cancel()
        await asyncio.gather(service.sampler, return_exceptions=True)
        service.policy = CollectionPolicy(enabled=True, categories=["mac"])
        service.tools.desktop.bridge.request.side_effect = [
            RuntimeError("native denied"),
            {"idle_s": 0},
        ]
        service.tools.desktop.apps = AsyncMock(return_value={"apps": []})
        iterations = 0

        async def tick(_):
            nonlocal iterations
            iterations += 1
            if iterations > 2:
                raise asyncio.CancelledError

        monkeypatch.setattr(analytics.asyncio, "sleep", tick)
        try:
            with pytest.raises(asyncio.CancelledError):
                await service.sample_apps()
            assert service.snapshot()["error"] is None
            assert service.policy.enabled and service.policy.categories == ["mac"]
        finally:
            await service.close()
            memory.close()

    asyncio.run(run())


def test_sync_offline_snapshot_and_remote_device_catalogue_use_actual_device_schema(tmp_path):
    async def run():
        memory, tools, service = sync_setup(tmp_path)
        try:
            _, jwk, device = identity()
            peer = trust(service, jwk, device, capabilities=["devices"], categories=["memory"])
            with memory.memory_transaction():
                memory.db.execute("UPDATE sync_peers SET last_seen=?", (time.time(),))
            snapshot = service.snapshot()
            assert not snapshot["running"] and not snapshot["peers"][0]["online"]
            assert snapshot["url"] == ""
            tools.devices = SimpleNamespace(
                snapshot=lambda: {
                    "items": [
                        {
                            "id": "local-led",
                            "name": "Лента",
                            "kind": "led_strip",
                            "usable": True,
                            "endpoint": "private-uuid",
                        },
                        {
                            "id": "remote-led",
                            "name": "Другой профиль",
                            "kind": "led_strip",
                            "usable": False,
                        },
                    ]
                }
            )
            assert service.remote_status(peer)["devices"] == [
                {"id": "local-led", "name": "Лента", "kind": "led_strip"}
            ]
            await service.stop()
            persisted = service.store.peer(device)
            assert persisted["categories"] == ["memory"]
            assert persisted["capabilities"] == ["devices"]
            assert persisted["last_seen"] is not None
        finally:
            await service.close()
            memory.close()

    asyncio.run(run())


def ephemeral_material(monkeypatch):
    names = [
        "kSecClass",
        "kSecClassGenericPassword",
        "kSecAttrService",
        "kSecAttrAccount",
        "kSecReturnData",
        "kSecValueData",
        "kSecAttrAccessible",
        "kSecAttrAccessibleWhenUnlockedThisDeviceOnly",
    ]
    monkeypatch.setitem(
        sys.modules,
        "Security",
        SimpleNamespace(
            **{name: name for name in names},
            errSecSuccess=0,
            errSecItemNotFound=-25300,
            SecItemCopyMatching=lambda *args: (-25300, None),
            SecItemAdd=lambda *args: (0, None),
        ),
    )
    return KeychainIdentity().read()


def test_sync_real_loopback_tls_start_stop_restart_preserves_pairing(tmp_path, monkeypatch):
    async def run():
        memory, _, service = sync_setup(tmp_path)
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", 0))
        except PermissionError:
            probe.close()
            await service.close()
            memory.close()
            pytest.skip("Loopback socket is sandboxed; run this case with socket access")
        port = probe.getsockname()[1]
        material = ephemeral_material(monkeypatch)
        service.identity = SimpleNamespace(read=lambda: material)
        key, jwk, device = identity()
        trust(service, jwk, device, capabilities=["chat"], categories=["memory"])
        context = ssl.create_default_context(cadata=certificates(material, "127.0.0.1")["ca"])
        try:
            try:
                with pytest.raises(ValueError, match="порт уже занят"):
                    await service.handle({"op": "start", "host": "127.0.0.1", "port": port})
                assert not service.snapshot()["running"] and service.url == ""
                assert service.store.peer(device)["capabilities"] == ["chat"]
            finally:
                probe.close()
            for _iteration in range(2):
                await service.start("127.0.0.1", port, advertise=False)
                assert service.snapshot()["running"]
                assert service.snapshot()["peers"][0]["online"] is False
                async with httpx.AsyncClient(base_url=service.url, verify=context) as client:
                    assert (await client.get("/")).status_code == 200
                    status = await signed(client, key, device, "reconnect", {}, origin=service.url)
                    assert status.status_code == 200
                assert service.snapshot()["peers"][0]["online"] is True
                await service.stop()
                assert not service.snapshot()["running"]
                assert service.snapshot()["url"] == ""
                assert service.snapshot()["peers"][0]["online"] is False
                assert service.store.peer(device)["capabilities"] == ["chat"]
            assert memory.preference("sync", "endpoint") == {"host": "127.0.0.1", "port": port}
        finally:
            await service.close()
            memory.close()

    asyncio.run(run())


def test_sync_crashed_task_does_not_prevent_cleanup_or_remove_pairings(tmp_path):
    async def run():
        memory, _, service = sync_setup(tmp_path)
        _, jwk, device = identity()
        trust(service, jwk, device, capabilities=["chat"], categories=[])

        async def crashed():
            raise RuntimeError("fixture server failure")

        service.server = SimpleNamespace(started=True, should_exit=False)
        service.server_task = asyncio.create_task(crashed())
        service.url = "https://127.0.0.1:8766"
        try:
            await service.stop()
            assert service.server_task is None and service.url == ""
            assert not service.snapshot()["running"]
            assert "ошибкой" in service.snapshot()["error"]
            assert service.store.peer(device)["capabilities"] == ["chat"]
        finally:
            await service.close()
            memory.close()

    asyncio.run(run())


def test_center_echoes_operation_ids_for_success_and_errors():
    class Example(Center):
        channel = "test"

        async def handle(self, value):
            if value.get("fail"):
                raise ValueError("fixture failure")
            return {"data": {"value": 1}}

    async def run():
        bus, events = HudBus(), []
        bus.publish = events.append
        service = Example()
        service.bind(bus)
        await service.run({"request_id": "qa-success"})
        assert events[-1]["operation_id"] == "qa-success" and events[-1]["busy"] is False
        await service.run({"request_id": "qa-error", "fail": True})
        assert events[-1]["operation_id"] == "qa-error" and events[-1]["error"] == "fixture failure"

    asyncio.run(run())


def test_center_rejects_second_client_request_without_releasing_first_operation():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class Example(Center):
            channel = "test"

            async def handle(self, value):
                entered.set()
                await release.wait()

        bus, events = HudBus(), []
        bus.publish = events.append
        service = Example()
        service.bind(bus)
        try:
            service.request({"request_id": "first"})
            await entered.wait()
            service.request({"request_id": "second"})
            assert events[-1]["operation_id"] == "second"
            assert events[-1]["busy"] is False and events[-1]["error"]
            assert not service.task.done()
            release.set()
            await service.task
            assert events[-1]["operation_id"] == "first" and not events[-1]["busy"]
        finally:
            await service.close()

    asyncio.run(run())
