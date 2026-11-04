import asyncio
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
from backend.sync.service import DeviceSync, lan_address


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
