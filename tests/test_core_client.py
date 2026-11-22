import asyncio
import json
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from backend.brain.llm import ChatChunk, LLMError
from backend.core_client import CoreAPIClient, CoreAutoClient
from backend.sync.identity import verify


class MemoryIdentity:
    def __init__(self):
        self.value = None

    def load(self):
        return self.value

    def save(self, device_id, private_key_pem):
        self.value = {
            "device_id": device_id,
            "private_key": private_key_pem.decode("ascii"),
        }


def certificate_pem():
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Core")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Core")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ip_address("100.117.129.107"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def test_core_pairing_pins_tls_and_signs_streamed_tool_requests():
    async def run():
        identity_store = MemoryIdentity()
        server_keys = {}
        observed = []

        async def handler(request):
            if request.url.path == "/v1/pair":
                payload = json.loads(request.content)
                from chushpan_core.identity import public_key

                _, _, identity = public_key(payload["public_jwk"])
                server_keys["jwk"] = payload["public_jwk"]
                return httpx.Response(201, json={"device_id": identity})
            if request.url.path == "/v1/chat":
                signed = "\n".join(
                    [
                        "v1",
                        request.headers["x-chushpan-timestamp"],
                        request.headers["x-chushpan-nonce"],
                        "POST",
                        request.url.path,
                        __import__("hashlib").sha256(request.content).hexdigest(),
                    ]
                )
                verify(server_keys["jwk"], request.headers["x-chushpan-signature"], signed)
                observed.append(json.loads(request.content))
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    text=(
                        'event: token\ndata:{"text":"Mac keeps tools"}\n\n'
                        'event: tool_calls\ndata:{"tool_calls":[{"id":"call-1",'
                        '"function":{"name":"open_app","arguments":{"app":"Safari"}}}]}\n\n'
                        'event: done\ndata:{"request_id":"test"}\n\n'
                    ),
                )
            if request.url.path == "/v1/core/status":
                return httpx.Response(200, json={"status": "ok"})
            if request.url.path == "/v1/memory":
                return httpx.Response(200, json={"entries": [{"id": "one"}]})
            if request.url.path == "/v1/memory/search":
                return httpx.Response(200, json={"entries": [{"id": "match"}]})
            if request.url.path == "/v1/memory/import":
                return httpx.Response(201, json={"created": [{"id": "new"}], "duplicates": 0})
            if request.url.path.startswith("/v1/memory/import/"):
                return httpx.Response(200, json={"deleted": 1, "preserved_modified": 0})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(
            base_url="https://100.117.129.107:8765",
            transport=transport,
        )
        api = CoreAPIClient(
            "https://100.117.129.107:8765",
            certificate_pem(),
            identity_store=identity_store,
            client=http,
        )
        paired = await api.pair("A" * 32)
        assert paired["paired"] is True
        assert identity_store.value["device_id"] == paired["device_id"]

        result = [
            chunk
            async for chunk in api.chat_stream(
                [{"role": "user", "content": "run Safari"}],
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "open_app",
                            "description": "Open an app",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )
        ]
        assert result[0].content == "Mac keeps tools"
        assert result[1].tool_calls[0]["function"]["name"] == "open_app"
        assert result[-1].done is True
        assert observed[0]["tools"][0]["function"]["name"] == "open_app"
        assert "A" * 32 not in identity_store.value["private_key"]
        assert await api.list_memory(limit=5) == [{"id": "one"}]
        assert await api.search_memory("selected memory") == [{"id": "match"}]
        migration_id = "5f3a808e-25db-4f10-9cf2-90cd892b89cb"
        imported = await api.import_memory(
            migration_id,
            [{"content": "Chosen", "category": "preference", "source": "explicit_user"}],
        )
        assert imported["created"][0]["id"] == "new"
        rollback = await api.rollback_memory_import(migration_id)
        assert rollback["deleted"] == 1
        await http.aclose()

    asyncio.run(run())


def test_optional_ollama_provider_is_loopback_only_and_streams_mac_tool_calls():
    from chushpan_core.chat import OllamaChatProvider

    async def run():
        observed = []

        async def respond(request):
            assert str(request.url) == "http://127.0.0.1:11434/api/chat"
            payload = json.loads(request.content)
            observed.append(payload)
            assert payload["model"] == "fixture-small-model"
            assert payload["messages"][0]["tool_calls"][0]["function"]["arguments"] == {
                "app": "Safari"
            }
            lines = [
                {"message": {"content": "Mac keeps control"}, "done": False},
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "open_app",
                                    "arguments": {"app": "Safari"},
                                }
                            }
                        ]
                    },
                    "done": True,
                },
            ]
            return httpx.Response(
                200,
                content="\n".join(json.dumps(item) for item in lines).encode(),
            )

        http = httpx.AsyncClient(
            base_url="http://127.0.0.1:11434",
            transport=httpx.MockTransport(respond),
        )
        provider = OllamaChatProvider(
            client=http,
            model="fixture-small-model",
            endpoint="http://127.0.0.1:11434",
        )
        tools = [
            {
                "type": "function",
                "function": {"name": "open_app", "parameters": {"type": "object"}},
            }
        ]
        chunks = [
            item
            async for item in provider.stream(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "open_app",
                                    "arguments": '{"app":"Safari"}',
                                },
                            }
                        ],
                    }
                ],
                "request-test",
                tools,
            )
        ]
        assert chunks[0]["text"] == "Mac keeps control"
        assert chunks[1]["tool_calls"][0]["function"]["name"] == "open_app"
        assert observed
        await provider.aclose()
        await http.aclose()

    asyncio.run(run())
    with pytest.raises(ValueError, match="loopback"):
        OllamaChatProvider(endpoint="http://100.117.129.107:11434")


def test_automatic_remote_mode_falls_back_only_before_any_remote_chunk():
    class Remote:
        available = True

        def __init__(self, chunks):
            self.chunks = chunks

        async def chat_stream(self, messages, tools=None):
            for chunk in self.chunks:
                if isinstance(chunk, BaseException):
                    raise chunk
                yield chunk

        async def aclose(self):
            pass

    class Local:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools=None):
            self.calls += 1
            yield ChatChunk(content="local fallback")

        async def aclose(self):
            pass

    async def run():
        local = Local()
        auto = CoreAutoClient(Remote([LLMError("offline")]), local)
        chunks = [chunk async for chunk in auto.chat_stream([{"role": "user", "content": "hi"}])]
        assert [chunk.content for chunk in chunks] == ["local fallback"]
        assert local.calls == 1

        local = Local()
        remote = Remote([ChatChunk(content="partial"), LLMError("connection dropped")])
        auto = CoreAutoClient(remote, local)
        chunks = []
        try:
            async for chunk in auto.chat_stream([{"role": "user", "content": "act"}]):
                chunks.append(chunk)
        except LLMError:
            pass
        else:
            raise AssertionError("A partial remote answer must not be retried locally.")
        assert [chunk.content for chunk in chunks] == ["partial"]
        assert local.calls == 0

    asyncio.run(run())


def test_mac_client_and_core_api_interoperate_with_signed_search_and_memory_transfer():
    from chushpan_core.app import create_app
    from chushpan_core.chat import ChatService
    from chushpan_core.identity import DeviceRegistry
    from chushpan_core.memory import MemoryStore

    async def run():
        registry = DeviceRegistry(":memory:")
        memory = MemoryStore(":memory:")
        invitation = registry.issue_invite("MacBook", "mac")
        chat = ChatService()
        app = create_app(
            lambda: "bootstrap",
            registry,
            memory,
            chat,
        )
        transport = httpx.ASGITransport(app=app)
        http = httpx.AsyncClient(
            base_url="https://100.117.129.107:8765",
            transport=transport,
        )
        identity_store = MemoryIdentity()
        api = CoreAPIClient(
            "https://100.117.129.107:8765",
            certificate_pem(),
            identity_store=identity_store,
            client=http,
        )
        paired = await api.pair(invitation["code"])
        assert paired["device_id"] == identity_store.value["device_id"]
        imported = await api.import_memory(
            "5f3a808e-25db-4f10-9cf2-90cd892b89cb",
            [
                {
                    "content": "A selected fact from the Mac client",
                    "category": "preference",
                    "source": "explicit_user",
                }
            ],
        )
        assert len(imported["created"]) == 1
        matches = await api.search_memory("selected fact")
        assert matches[0]["content"] == "A selected fact from the Mac client"
        assert matches[0]["source"] == "mac_import:explicit_user"
        rollback = await api.rollback_memory_import(imported["migration_id"])
        assert rollback["deleted"] == 1
        assert await api.list_memory() == []
        await http.aclose()
        await chat.provider.aclose()
        memory.close()
        registry.close()

    asyncio.run(run())
