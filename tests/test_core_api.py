import asyncio
import json
import ssl
import time
from ipaddress import ip_address

import pytest
from chushpan_core import identity, server
from chushpan_core.app import create_app
from chushpan_core.chat import ChatService, OpenAIChatProvider
from chushpan_core.identity import DeviceAuthError, DeviceRegistry, encode, request_payload
from chushpan_core.memory import MemoryStore
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from fastapi.testclient import TestClient


def key_jwk(private_key):
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": encode(numbers.x.to_bytes(32, "big")),
        "y": encode(numbers.y.to_bytes(32, "big")),
    }


def protect_test_bytes(value):
    return bytes(byte ^ 0xAA for byte in value)


def signed_headers(private_key, device_id, method, path, body=b"", *, nonce="A" * 22):
    timestamp = str(int(time.time()))
    payload = request_payload(method, path, timestamp, nonce, body)
    der = private_key.sign(payload, ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return {
        "X-Chushpan-Device": device_id,
        "X-Chushpan-Timestamp": timestamp,
        "X-Chushpan-Nonce": nonce,
        "X-Chushpan-Signature": encode(raw),
    }


def signed_request(client, key, device_id, method, path, body=None, *, nonce):
    raw = (
        json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if body is not None
        else b""
    )
    headers = signed_headers(key, device_id, method, path, raw, nonce=nonce)
    if body is not None:
        headers["Content-Type"] = "application/json"
    return client.request(method, path, content=raw, headers=headers)


def test_health_is_minimal_and_https_only():
    response = TestClient(create_app(lambda: "test-token"), base_url="https://testserver").get(
        "/health"
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "chushpan-core",
        "version": "0.1.0",
    }

    insecure = TestClient(create_app(lambda: "test-token")).get("/health")
    assert insecure.status_code == 426

    oversized = TestClient(create_app(lambda: "test-token"), base_url="https://testserver").post(
        "/v1/pair", content=b"x" * (1_048_576 + 1)
    )
    assert oversized.status_code == 413


def test_bootstrap_status_remains_available_only_over_https():
    client = TestClient(create_app(lambda: "test-token"), base_url="https://testserver")

    assert client.get("/v1/status").status_code == 401
    assert (
        client.get("/v1/status", headers={"Authorization": "Basic test-token"}).status_code == 401
    )
    assert client.get("/v1/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.get("/v1/status", headers={"Authorization": "Bearer test-token"})

    assert response.status_code == 200
    assert response.json()["service"] == "chushpan-core"


def test_status_returns_unavailable_when_secret_cannot_be_loaded():
    def fail_to_load():
        raise RuntimeError("fixture failure")

    response = TestClient(create_app(fail_to_load), base_url="https://testserver").get(
        "/v1/status", headers={"Authorization": "Bearer test-token"}
    )

    assert response.status_code == 503
    assert "fixture failure" not in response.text


def test_pairing_signatures_scopes_replay_protection_and_revocation():
    registry = DeviceRegistry(":memory:")
    mac_invite = registry.issue_invite("MacBook", "mac")
    phone_invite = registry.issue_invite("Phone", "iphone")
    mac_key = ec.generate_private_key(ec.SECP256R1())
    phone_key = ec.generate_private_key(ec.SECP256R1())
    client = TestClient(create_app(lambda: "bootstrap", registry), base_url="https://testserver")

    mac_pair = client.post(
        "/v1/pair", json={"code": mac_invite["code"], "public_jwk": key_jwk(mac_key)}
    )
    phone_pair = client.post(
        "/v1/pair", json={"code": phone_invite["code"], "public_jwk": key_jwk(phone_key)}
    )
    assert mac_pair.status_code == phone_pair.status_code == 201
    mac_id, phone_id = mac_pair.json()["device_id"], phone_pair.json()["device_id"]

    listing = client.get(
        "/v1/devices",
        headers=signed_headers(mac_key, mac_id, "GET", "/v1/devices", nonce="B" * 22),
    )
    assert listing.status_code == 200
    assert {d["device_name"] for d in listing.json()["devices"]} == {"MacBook", "Phone"}

    limited = client.get(
        "/v1/devices",
        headers=signed_headers(phone_key, phone_id, "GET", "/v1/devices", nonce="C" * 22),
    )
    assert limited.status_code == 403

    replay_headers = signed_headers(mac_key, mac_id, "GET", "/v1/devices/me", nonce="D" * 22)
    assert client.get("/v1/devices/me", headers=replay_headers).status_code == 200
    assert client.get("/v1/devices/me", headers=replay_headers).status_code == 401

    revoke_path = f"/v1/devices/{phone_id}"
    revoked = client.delete(
        revoke_path,
        headers=signed_headers(mac_key, mac_id, "DELETE", revoke_path, nonce="E" * 22),
    )
    assert revoked.status_code == 200
    after_revoke = client.get(
        "/v1/devices/me",
        headers=signed_headers(phone_key, phone_id, "GET", "/v1/devices/me", nonce="F" * 22),
    )
    assert after_revoke.status_code == 401

    reused = client.post(
        "/v1/pair", json={"code": phone_invite["code"], "public_jwk": key_jwk(phone_key)}
    )
    assert reused.status_code == 401
    registry.close()


def test_pairing_invitation_expires():
    registry = DeviceRegistry(":memory:")
    invite = registry.issue_invite("Tablet", "ipad", now=100, ttl_seconds=30)
    key = ec.generate_private_key(ec.SECP256R1())

    with pytest.raises(DeviceAuthError, match="expired"):
        registry.pair(invite["code"], key_jwk(key), now=131)
    registry.close()


def test_memory_api_requires_signed_scoped_device_and_honors_private_mode():
    registry = DeviceRegistry(":memory:")
    memory = MemoryStore(":memory:")
    key = ec.generate_private_key(ec.SECP256R1())
    invite = registry.issue_invite("MacBook", "mac")
    client = TestClient(
        create_app(lambda: "bootstrap", registry, memory), base_url="https://testserver"
    )
    paired = client.post("/v1/pair", json={"code": invite["code"], "public_jwk": key_jwk(key)})
    device_id = paired.json()["device_id"]

    created = signed_request(
        client,
        key,
        device_id,
        "POST",
        "/v1/memory",
        {"content": "Core memory stays on Huawei", "category": "project"},
        nonce="G" * 22,
    )
    assert created.status_code == 201
    identity = created.json()["entry"]["id"]

    private = signed_request(
        client,
        key,
        device_id,
        "POST",
        "/v1/memory",
        {"content": "Do not store", "private_mode": True},
        nonce="H" * 22,
    )
    assert private.json() == {"stored": False, "private_mode": True}

    search = signed_request(
        client,
        key,
        device_id,
        "GET",
        "/v1/memory/search?q=huawei",
        nonce="I" * 22,
    )
    assert search.status_code == 200
    assert [entry["id"] for entry in search.json()["entries"]] == [identity]

    updated = signed_request(
        client,
        key,
        device_id,
        "PATCH",
        f"/v1/memory/{identity}",
        {"content": "Core memory remains on Huawei", "revision": 1},
        nonce="J" * 22,
    )
    assert updated.status_code == 200
    history = signed_request(
        client,
        key,
        device_id,
        "GET",
        f"/v1/memory/{identity}/history",
        nonce="K" * 22,
    )
    assert [item["action"] for item in history.json()["history"]] == ["create", "update"]

    forgotten = signed_request(
        client,
        key,
        device_id,
        "DELETE",
        f"/v1/memory/{identity}",
        nonce="L" * 22,
    )
    assert forgotten.json() == {"forgotten": True, "id": identity}
    assert memory.list() == []
    memory.close()
    registry.close()


def test_memory_import_api_requires_device_scope_and_supports_exact_rollback():
    from uuid import uuid4

    registry = DeviceRegistry(":memory:")
    memory = MemoryStore(":memory:")
    key = ec.generate_private_key(ec.SECP256R1())
    invite = registry.issue_invite("MacBook", "mac")
    client = TestClient(
        create_app(lambda: "bootstrap", registry, memory), base_url="https://testserver"
    )
    paired = client.post("/v1/pair", json={"code": invite["code"], "public_jwk": key_jwk(key)})
    device_id = paired.json()["device_id"]
    migration_id = str(uuid4())
    path = "/v1/memory/import"
    payload = {
        "migration_id": migration_id,
        "entries": [
            {
                "content": "Explicitly selected from Mac",
                "category": "project",
                "source": "ui_confirmed",
            },
            {
                "content": "A second selected fact",
                "category": "preference",
                "source": "explicit_user",
            },
        ],
    }
    imported = signed_request(client, key, device_id, "POST", path, payload, nonce="M" * 22)
    assert imported.status_code == 201
    assert len(imported.json()["created"]) == 2
    assert memory.count() == 2

    repeated = signed_request(client, key, device_id, "POST", path, payload, nonce="N" * 22)
    assert repeated.status_code == 201
    assert repeated.json()["already_imported"] is True
    assert memory.count() == 2

    rollback_path = f"/v1/memory/import/{migration_id}"
    rollback = signed_request(client, key, device_id, "DELETE", rollback_path, nonce="O" * 22)
    assert rollback.status_code == 200
    assert rollback.json()["deleted"] == 2
    assert memory.count() == 0
    assert (
        signed_request(client, key, device_id, "DELETE", rollback_path, nonce="P" * 22).json()[
            "already_rolled_back"
        ]
        is True
    )
    memory.close()
    registry.close()


def test_chat_api_streams_and_rejects_duplicate_paid_request_id():
    class FakeProvider:
        available = True

        def __init__(self):
            self.calls = 0

        async def stream(self, messages, request_id, tools=None):
            self.calls += 1
            assert messages[-1]["content"] == "test prompt"
            assert messages[-2]["role"] == "system"
            assert "central memory" in messages[-2]["content"]
            assert request_id == "9f9074ee-78df-4cd6-a935-35f9585b6878"
            yield "Hello "
            yield "from Core"

    registry = DeviceRegistry(":memory:")
    provider = FakeProvider()
    memory = MemoryStore(":memory:")
    memory.create("A central memory fact for the test prompt", category="note")
    key = ec.generate_private_key(ec.SECP256R1())
    invite = registry.issue_invite("MacBook", "mac")
    client = TestClient(
        create_app(
            lambda: "bootstrap",
            registry,
            memory,
            ChatService(provider),
        ),
        base_url="https://testserver",
    )
    device_id = client.post(
        "/v1/pair", json={"code": invite["code"], "public_jwk": key_jwk(key)}
    ).json()["device_id"]
    payload = {
        "request_id": "9f9074ee-78df-4cd6-a935-35f9585b6878",
        "messages": [{"role": "user", "content": "test prompt"}],
    }

    response = signed_request(client, key, device_id, "POST", "/v1/chat", payload, nonce="M" * 22)
    assert response.status_code == 200
    assert "event: token" in response.text
    assert '"text":"Hello "' in response.text
    assert '"text":"from Core"' in response.text
    assert "event: done" in response.text

    duplicate = signed_request(client, key, device_id, "POST", "/v1/chat", payload, nonce="N" * 22)
    assert duplicate.status_code == 409
    assert provider.calls == 1
    memory.close()
    registry.close()


def test_chat_queue_is_fifo_bounded_and_releases_after_cancellation():
    from chushpan_core.chat import ChatBusy

    async def run():
        service = ChatService()
        first_started = asyncio.Event()
        release = asyncio.Event()
        order = []

        async def worker(name):
            reservation = await service.reserve()
            async with reservation:
                order.append(name)
                if name == "first":
                    first_started.set()
                    await release.wait()

        first = asyncio.create_task(worker("first"))
        await first_started.wait()
        second = asyncio.create_task(worker("second"))
        third = asyncio.create_task(worker("third"))
        await asyncio.sleep(0)
        assert service.pending == 3
        release.set()
        await asyncio.gather(first, second, third)
        assert order == ["first", "second", "third"]
        assert service.pending == 0

        blocked = asyncio.Event()
        entered = asyncio.Event()

        async def cancellable():
            reservation = await service.reserve()
            async with reservation:
                entered.set()
                await blocked.wait()

        task = asyncio.create_task(cancellable())
        await entered.wait()
        waiting = asyncio.create_task(worker("after-cancel"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(waiting, timeout=1)
        assert order[-1] == "after-cancel"
        assert service.pending == 0

        reservations = [await service.reserve() for _ in range(8)]
        with pytest.raises(ChatBusy):
            await service.reserve()
        await asyncio.gather(*(reservation.close() for reservation in reservations))

    asyncio.run(run())


def test_openai_stream_parser_uses_test_transport_without_live_calls():
    async def run():
        async def respond(request):
            assert request.headers["authorization"] == "Bearer test-key-only"
            return httpx.Response(
                200,
                text=(
                    'data: {"choices":[{"delta":{"content":"safe"},"finish_reason":null}]}\n\n'
                    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1",'
                    '"function":{"name":"open_app","arguments":"{\\"app\\":\\"Safari\\"}"}}]},'
                    '"finish_reason":null}]}\n\n'
                    "data: [DONE]\n\n"
                ),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        provider = OpenAIChatProvider(key_loader=lambda: "test-key-only", client=client)
        chunks = [
            text
            async for text in provider.stream(
                [{"role": "user", "content": "local test"}],
                "request-test",
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "open_app",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )
        ]
        await client.aclose()
        assert chunks == [
            {"type": "token", "text": "safe"},
            {
                "type": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "open_app", "arguments": {"app": "Safari"}},
                    }
                ],
            },
        ]

    import httpx

    asyncio.run(run())


def test_tls_identity_is_pinned_and_reused(tmp_path, monkeypatch):
    paths = (
        tmp_path / "server-cert.pem",
        tmp_path / "server-key.dpapi",
        tmp_path / "server-cert.sha256",
    )
    monkeypatch.setattr(identity, "tls_paths", lambda: paths)
    monkeypatch.setattr(
        identity,
        "_unprotect_tls_key",
        protect_test_bytes,
    )
    monkeypatch.setattr(identity, "_protect_tls_key", protect_test_bytes)

    first = identity.ensure_tls_certificate("100.117.129.107")
    certificate = x509.load_pem_x509_certificate(paths[0].read_bytes())
    san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)

    assert san.value.get_values_for_type(x509.IPAddress) == [ip_address("100.117.129.107")]
    assert first["fingerprint"] == paths[2].read_text().strip()
    assert b"BEGIN PRIVATE KEY" not in paths[1].read_bytes()
    assert identity.ensure_tls_certificate("100.117.129.107")["fingerprint"] == first["fingerprint"]

    with pytest.raises(ValueError, match="Tailscale"):
        identity.ensure_tls_certificate("8.8.8.8")


def test_server_requires_tls_12_or_newer(tmp_path, monkeypatch):
    paths = (
        tmp_path / "server-cert.pem",
        tmp_path / "server-key.dpapi",
        tmp_path / "server-cert.sha256",
    )
    monkeypatch.setattr(identity, "tls_paths", lambda: paths)
    monkeypatch.setattr(
        identity,
        "_unprotect_tls_key",
        protect_test_bytes,
    )
    monkeypatch.setattr(identity, "_protect_tls_key", protect_test_bytes)
    identity.ensure_tls_certificate("100.117.129.107")
    monkeypatch.setattr(server, "tls_paths", lambda: paths)
    monkeypatch.setattr(
        server,
        "tls_private_key_pem",
        lambda: identity._unprotect_tls_key(paths[1].read_bytes()),
    )

    context = server.minimum_tls_12(
        None, lambda: pytest.fail("default loader must not read DPAPI key")
    )

    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert not list(tmp_path.glob("core-tls-*.pem"))
