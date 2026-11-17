import asyncio
import ipaddress
import json
import secrets
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from backend.config import Settings
from backend.hud.bus import HudBus
from backend.memory.store import MemoryStore
from backend.security import Confirmations
from backend.sync.identity import encode, public_key
from backend.sync.service import DeviceSync, connection_type, lan_address, pairing_code
from backend.sync.store import SyncStore


def identity():
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": encode(numbers.x.to_bytes(32)),
        "y": encode(numbers.y.to_bytes(32)),
    }
    return key, jwk, public_key(jwk)[2]


def setup(tmp_path):
    memory = MemoryStore(tmp_path / "sync.sqlite")
    tools = SimpleNamespace(
        confirmations=Confirmations(),
        desktop=SimpleNamespace(bridge=SimpleNamespace(request=AsyncMock())),
        call=AsyncMock(
            return_value=json.dumps({"ok": True, "message": "Передано", "result_status": "sent"})
        ),
        automations=None,
        devices=None,
    )
    service = DeviceSync(Settings(), memory, tools, HudBus())
    service.url, service.fingerprint = "https://127.0.0.1:8766", "example-fingerprint"
    return memory, tools, service


async def signed(
    client,
    key,
    device,
    operation,
    value,
    *,
    nonce=None,
    timestamp=None,
    signature=None,
    origin="https://127.0.0.1:8766",
):
    body = json.dumps(value, separators=(",", ":"))
    nonce, timestamp = nonce or secrets.token_hex(16), timestamp or str(int(time.time()))
    raw = f"/api/{operation}\n{timestamp}\n{nonce}\n{body}".encode()
    r, s = utils.decode_dss_signature(key.sign(raw, ec.ECDSA(hashes.SHA256())))
    signature = signature or encode(r.to_bytes(32) + s.to_bytes(32))
    return await client.post(
        "/api/" + operation,
        content=body,
        headers={
            "Content-Type": "application/json",
            "Origin": origin,
            "X-Device": device,
            "X-Time": timestamp,
            "X-Nonce": nonce,
            "X-Signature": signature,
        },
    )


def trust(service, jwk, device, capabilities=(), categories=()):
    service.store.trust({"id": device, "name": "Test iPad", "kind": "ipad", "key": jwk})
    with service.memory.memory_transaction():
        service.memory.db.execute(
            "UPDATE sync_peers SET capabilities=?,categories=? WHERE id=?",
            (json.dumps(list(capabilities)), json.dumps(list(categories)), device),
        )
    return service.store.peer(device)


def test_pairing_requires_both_devices_expires_and_starts_with_no_grants(tmp_path):
    async def go():
        memory, tools, service = setup(tmp_path)
        key, jwk, device = identity()
        service.invite = {"nonce": secrets.token_urlsafe(24), "expires": time.time() + 120}
        invite = service.invite["nonce"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.application()), base_url=service.url
        ) as client:
            result = await signed(
                client,
                key,
                device,
                "pair",
                {"invite": invite, "key": jwk, "name": "iPad", "kind": "ipad"},
            )
            assert result.status_code == 200 and result.json()["status"] == "pending"
            assert service.invite is None
            assert (await signed(client, key, device, "status", {})).status_code == 403
            code = result.json()["code"]
            with pytest.raises(ValueError):
                await service.handle(
                    {"op": "approve", "id": device, "code": "000000", "confirmed": True}
                )
            await service.handle({"op": "approve", "id": device, "code": code, "confirmed": True})
            result = await signed(client, key, device, "status", {})
            assert result.status_code == 200 and result.json()["capabilities"] == []
            assert result.json()["categories"] == []
            assert (
                await signed(
                    client,
                    key,
                    device,
                    "pair",
                    {"invite": invite, "key": jwk, "name": "iPad", "kind": "ipad"},
                )
            ).status_code == 403
            tools.call.assert_not_awaited()
        await service.close()
        memory.close()

    asyncio.run(go())


def test_discovery_pairing_checks_certificate_bound_code_and_handles_two_devices(tmp_path):
    async def go():
        memory, _, service = setup(tmp_path)
        service.fingerprint = "ab" * 32
        first_key, first_jwk, first_id = identity()
        second_key, second_jwk, second_id = identity()
        first_challenge = secrets.token_urlsafe(24)
        second_challenge = secrets.token_urlsafe(24)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.application()), base_url=service.url
        ) as client:
            first = await signed(
                client,
                first_key,
                first_id,
                "pair",
                {
                    "challenge": first_challenge,
                    "key": first_jwk,
                    "name": "iPhone",
                    "kind": "iphone",
                },
            )
            second = await signed(
                client,
                second_key,
                second_id,
                "pair",
                {
                    "challenge": second_challenge,
                    "key": second_jwk,
                    "name": "iPad",
                    "kind": "ipad",
                },
            )
            assert first.status_code == second.status_code == 200
            assert first.json()["code"] == pairing_code(
                first_challenge, first_id, service.fingerprint
            )
            assert second.json()["code"] == pairing_code(
                second_challenge, second_id, service.fingerprint
            )
            assert len(service.pending) == 2
            assert (await signed(client, first_key, first_id, "status", {})).status_code == 403

            await service.handle(
                {
                    "op": "approve",
                    "id": first_id,
                    "code": first.json()["code"],
                    "confirmed": True,
                }
            )
            await service.handle({"op": "reject", "id": second_id})
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=service.application(), client=("100.94.1.2", 51234)
                ),
                base_url=service.url,
            ) as tailscale_client:
                trusted = await signed(tailscale_client, first_key, first_id, "status", {})
            rejected = await signed(client, second_key, second_id, "status", {})
            assert trusted.status_code == 200
            assert trusted.json()["capabilities"] == []
            assert trusted.json()["categories"] == []
            assert service.store.peer(first_id)["connection_type"] == "tailscale"
            assert rejected.status_code == 403
            peers = service.store.peers()
            assert len(peers) == 1 and peers[0]["kind"] == "iphone"
        await service.close()
        memory.close()

    asyncio.run(go())


def test_tailscale_bind_addresses_are_allowed_without_allowing_public_interfaces():
    assert lan_address("100.94.1.2") == "100.94.1.2"
    with pytest.raises(ValueError, match="Публичный интерфейс запрещён"):
        lan_address("203.0.113.20")
    assert connection_type("100.94.1.2") == "tailscale"
    assert connection_type("192.168.1.20") == "local"


def test_existing_trusted_devices_migrate_connection_type_without_losing_trust(tmp_path):
    memory = MemoryStore(tmp_path / "sync-migration.sqlite")
    memory.db.execute(
        "CREATE TABLE sync_peers (id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,"
        "public_key TEXT NOT NULL,approved_at REAL NOT NULL,revoked_at REAL,"
        "disconnected INTEGER NOT NULL DEFAULT 0,last_seen REAL,last_sync REAL,"
        "categories TEXT NOT NULL DEFAULT '[]',capabilities TEXT NOT NULL DEFAULT '[]')"
    )
    memory.db.execute(
        "INSERT INTO sync_peers(id,name,kind,public_key,approved_at) VALUES(?,?,?,?,?)",
        ("device", "Existing iPad", "ipad", "{}", time.time()),
    )
    store = SyncStore(memory)
    peer = store.peer("device")
    assert peer["name"] == "Existing iPad"
    assert peer["connection_type"] == "local"
    assert store.peers()[0]["connection_type"] == "local"
    memory.close()


def test_authentication_replay_origin_stale_request_disconnect_and_revoke(tmp_path):
    async def go():
        memory, _, service = setup(tmp_path)
        key, jwk, device = identity()
        trust(service, jwk, device)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.application()), base_url=service.url
        ) as client:
            nonce = secrets.token_hex(16)
            assert (await signed(client, key, device, "status", {}, nonce=nonce)).status_code == 200
            assert (await signed(client, key, device, "status", {}, nonce=nonce)).status_code == 403
            assert (
                await signed(client, key, device, "status", {}, signature=encode(b"0" * 64))
            ).status_code == 403
            assert (
                await signed(client, key, device, "status", {}, origin="https://attacker.invalid")
            ).status_code == 403
            assert (
                await signed(
                    client, key, device, "status", {}, timestamp=str(int(time.time() - 120))
                )
            ).status_code == 403
            await service.handle({"op": "disconnect", "id": device})
            assert (await signed(client, key, device, "status", {})).status_code == 403
            assert (await signed(client, key, device, "reconnect", {})).status_code == 200
            await service.handle({"op": "revoke", "id": device})
            assert (await signed(client, key, device, "reconnect", {})).status_code == 403
        await service.close()
        memory.close()

    asyncio.run(go())


def test_public_certificate_route_returns_only_the_pinning_certificate(tmp_path):
    async def go():
        memory, _, service = setup(tmp_path)
        service.ca = b"public test certificate"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.application()), base_url=service.url
        ) as client:
            result = await client.get("/api/certificate")
            assert result.status_code == 200
            assert result.content == service.ca
            assert result.headers["cache-control"] == "no-store"
            assert result.headers["content-type"].startswith("application/pkix-cert")
        service.ca = None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.application()), base_url=service.url
        ) as client:
            assert (await client.get("/api/certificate")).status_code == 503
        await service.close()
        memory.close()

    asyncio.run(go())


def test_commands_are_once_only_expiring_and_cannot_reuse_ids_for_different_args(tmp_path):
    async def go():
        memory, tools, service = setup(tmp_path)
        _, jwk, device = identity()
        peer = trust(service, jwk, device, capabilities=["open_app"])
        command = {
            "id": secrets.token_hex(16),
            "operation": "open_app",
            "arguments": {"name": "Safari"},
            "expires": time.time() + 60,
        }
        assert service.submit(peer, command)["status"] == "running"
        await asyncio.gather(*list(service.commands.values()))
        assert service.submit(peer, command)["status"] == "completed"
        assert tools.call.await_count == 1
        with pytest.raises(ValueError, match="другой командой"):
            service.submit(peer, {**command, "arguments": {"name": "Finder"}})
        with pytest.raises(ValueError, match="устарела"):
            service.submit(
                peer, {**command, "id": secrets.token_hex(16), "expires": time.time() - 1}
            )
        with pytest.raises(ValueError, match="не разрешена"):
            service.submit(peer, {**command, "operation": "shell"})
        await service.close()
        memory.close()

    asyncio.run(go())


def test_remote_scenario_passes_explicit_version_and_values_to_existing_runner(tmp_path):
    async def go():
        memory, tools, service = setup(tmp_path)
        _, jwk, device = identity()
        scenario_id = "1" * 32
        scenario = SimpleNamespace(id=scenario_id, revision=7, name="Тест")
        tools.automations = SimpleNamespace(store=SimpleNamespace(get=lambda identifier: scenario))
        trust(service, jwk, device, capabilities=["scenario"])
        command = {
            "id": secrets.token_hex(16),
            "operation": "scenario",
            "arguments": {
                "id": scenario_id,
                "revision": 7,
                "variant": "2" * 32,
                "values": {"room": "study"},
            },
            "expires": time.time() + 60,
        }
        service.submit(service.store.peer(device), command)
        await asyncio.gather(*list(service.commands.values()))
        assert tools.call.await_args.args == (
            "run_scenario",
            {
                "id": scenario_id,
                "revision": 7,
                "variant": "2" * 32,
                "values": '{"room": "study"}',
            },
        )
        assert memory.db.execute("SELECT status FROM sync_commands").fetchone()[0] == "completed"
        await service.close()
        memory.close()

    asyncio.run(go())


def test_remote_status_exposes_only_scenario_input_names_and_kinds(tmp_path):
    memory, _, service = setup(tmp_path)
    scenario = SimpleNamespace(
        id="1" * 32,
        name="Вечерний свет",
        revision=3,
        enabled=True,
        variants=[],
        variables=[SimpleNamespace(name="brightness", kind="number", default=45)],
    )
    service.tools.automations = SimpleNamespace(store=SimpleNamespace(list=lambda: [scenario]))
    status = service.remote_status(
        {
            "id": "d" * 64,
            "capabilities": ["scenario"],
            "categories": [],
            "name": "iPhone",
        }
    )
    assert status["scenarios"] == [
        {
            "id": scenario.id,
            "name": scenario.name,
            "revision": 3,
            "variants": [],
            "variables": [{"name": "brightness", "kind": "number"}],
        }
    ]
    assert "default" not in status["scenarios"][0]["variables"][0]
    asyncio.run(service.close())
    memory.close()


def test_remote_led_action_uses_existing_device_command_without_direct_ble(tmp_path):
    async def go():
        memory, tools, service = setup(tmp_path)
        _, jwk, device = identity()
        trust(service, jwk, device, capabilities=["devices"])
        command = {
            "id": secrets.token_hex(16),
            "operation": "device",
            "arguments": {
                "target": "configured-led",
                "action": "led",
                "color": "#8A28CC",
                "brightness": 55,
                "power": True,
            },
            "expires": time.time() + 60,
        }
        service.submit(service.store.peer(device), command)
        await asyncio.gather(*list(service.commands.values()))
        assert tools.call.await_args.args == (
            "device_command",
            {
                "target": "configured-led",
                "action": "led",
                "color": "#8A28CC",
                "brightness": 55,
                "power": True,
            },
        )
        assert memory.db.execute("SELECT status FROM sync_commands").fetchone()[0] == "completed"
        await service.close()
        memory.close()

    asyncio.run(go())


def test_permission_revocation_cancels_running_commands_without_retry(tmp_path):
    async def go():
        memory, tools, service = setup(tmp_path)
        _, jwk, device = identity()
        peer = trust(service, jwk, device, capabilities=["open_app"])
        entered = asyncio.Event()

        async def blocked(*args):
            entered.set()
            await asyncio.sleep(100)

        tools.call.side_effect = blocked
        command = {
            "id": secrets.token_hex(16),
            "operation": "open_app",
            "arguments": {"name": "Safari"},
            "expires": time.time() + 60,
        }
        service.submit(peer, command)
        await entered.wait()
        task = next(iter(service.commands.values()))
        await service.handle(
            {"op": "permissions", "id": device, "capabilities": [], "categories": []}
        )
        await task
        assert memory.db.execute("SELECT status FROM sync_commands").fetchone()[0] == "interrupted"
        assert tools.call.await_count == 1
        await service.close()
        memory.close()

    asyncio.run(go())


def test_selective_sync_excludes_secrets_sensitive_expired_project_and_other_dialogues(tmp_path):
    memory, _, service = setup(tmp_path)
    _, jwk, device = identity()
    peer = trust(service, jwk, device, categories=["memory", "preferences", "history"])
    memory.save_memory("Люблю чай", category="preference")
    memory.save_memory("Мой диагноз: пример", category="preference", sensitive=True)
    memory.db.execute(
        "INSERT INTO memory_projects(name,created_at,updated_at) VALUES('Private','now','now')"
    )
    memory.save_memory("Секрет проекта", project_id=1)
    expired, _ = memory.save_memory("Устаревший факт")
    with memory.memory_transaction():
        memory.db.execute(
            "UPDATE facts SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (expired,)
        )
    result = service.sync_data(peer)
    assert [row["content"] for row in result["data"]["memory"]] == ["Люблю чай"]
    assert [row["content"] for row in result["data"]["preferences"]] == ["Люблю чай"]
    assert result["data"]["history"] == []
    memory.close()


def test_remote_memory_changes_wait_for_mac_and_detect_revision_conflicts(tmp_path):
    async def go():
        memory, _, service = setup(tmp_path)
        _, jwk, device = identity()
        peer = trust(service, jwk, device, categories=["memory"])
        item, _ = memory.save_memory("Люблю чай")
        result = service.propose_memory(peer, {"id": item, "revision": 1, "content": "Люблю кофе"})
        assert result["status"] == "awaiting_mac_confirmation"
        assert memory.facts()[0][1] == "Люблю чай"
        conflict = service.snapshot()["conflicts"][0]
        await service.handle(
            {"op": "conflict", "id": conflict["id"], "accept": True, "revision": 1}
        )
        assert memory.facts()[0][1] == "Люблю кофе"
        result = service.propose_memory(peer, {"id": item, "revision": 1, "content": "Люблю воду"})
        assert result["status"] == "conflict"
        await service.close()
        memory.close()

    asyncio.run(go())


@pytest.mark.parametrize("host", ["0.0.0.0", "8.8.8.8", "::", "example.com", "169.254.1.2"])
def test_sync_refuses_public_wildcard_and_unverified_addresses(host):
    with pytest.raises(ValueError):
        lan_address(host)


def test_tls_identity_is_keychain_only_and_cert_has_ip_san(monkeypatch):
    import sys

    from backend.sync.identity import KeychainIdentity, certificates

    saved = []
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
    security = SimpleNamespace(
        **{name: name for name in names},
        errSecSuccess=0,
        errSecItemNotFound=-25300,
        SecItemCopyMatching=lambda *args: (-25300, None),
        SecItemAdd=lambda query, _: (saved.append(query) or 0, None),
    )
    monkeypatch.setitem(sys.modules, "Security", security)
    material = KeychainIdentity().read()
    result = certificates(material, "192.168.1.10")
    cert = x509.load_pem_x509_certificate(result["certificate"])
    assert (
        str(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value[0].value)
        == "192.168.1.10"
    )
    assert len(result["fingerprint"]) == 64
    assert saved[0]["kSecAttrAccessible"] == "kSecAttrAccessibleWhenUnlockedThisDeviceOnly"
    assert serialization.load_pem_private_key(material["server_key"].encode(), password=None)

    multi = certificates(
        material,
        ["192.168.1.10", "100.94.1.2"],
        service_hostname="chushpan-test.local",
    )
    names = (
        x509.load_pem_x509_certificate(multi["certificate"])
        .extensions.get_extension_for_class(x509.SubjectAlternativeName)
        .value
    )
    assert set(names.get_values_for_type(x509.IPAddress)) == {
        ipaddress.ip_address("192.168.1.10"),
        ipaddress.ip_address("100.94.1.2"),
    }
    assert names.get_values_for_type(x509.DNSName) == ["chushpan-test.local"]
