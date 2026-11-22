"""Pinned-TLS, per-device client for the independent Huawei Core API."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import re
import secrets
import ssl
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from backend.brain.llm import ChatChunk, LLMError, Message
from backend.context import request_context

TAILSCALE = ipaddress.ip_network("100.64.0.0/10")
KEYCHAIN_SERVICE = "ee.chushpan.core.client"
KEYCHAIN_ACCOUNT = "device-identity"


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _certificate(address: str, pem: str) -> tuple[ssl.SSLContext, str]:
    parsed = urlsplit(address)
    try:
        ip = ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        raise ValueError("Core API must use a Tailscale IPv4 address.") from None
    if (
        parsed.scheme != "https"
        or ip.version != 4
        or ip not in TAILSCALE
        or parsed.port != 8765
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Core API must be HTTPS on its fixed Tailscale endpoint.")
    if not isinstance(pem, str) or len(pem) > 4096 or "-----BEGIN CERTIFICATE-----" not in pem:
        raise ValueError("Paste the public TLS certificate obtained over SSH.")
    try:
        cert = x509.load_pem_x509_certificate(pem.encode("ascii"))
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        addresses = san.get_values_for_type(x509.IPAddress)
        constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    except (UnicodeEncodeError, ValueError, x509.ExtensionNotFound):
        raise ValueError("The TLS certificate is invalid.") from None
    if (
        ip not in addresses
        or cert.subject != cert.issuer
        or not constraints.ca
        or cert.not_valid_after_utc <= datetime.now(UTC)
        or not isinstance(cert.public_key(), ec.EllipticCurvePublicKey)
        or not isinstance(cert.public_key().curve, ec.SECP256R1)
    ):
        raise ValueError("The certificate does not match the configured Huawei Tailscale IP.")
    try:
        cert.verify_directly_issued_by(cert)
        context = ssl.create_default_context(cadata=pem)
    except (ValueError, ssl.SSLError):
        raise ValueError("The self-signed TLS certificate failed its signature check.") from None
    return context, cert.fingerprint(hashes.SHA256()).hex()


class CoreKeychainIdentity:
    """Store a P-256 device private key only in this Mac's Keychain."""

    def __init__(self, *, security=None) -> None:
        self._security = security

    def security(self):
        if self._security is None:
            try:
                import Security
            except ImportError:
                raise RuntimeError("macOS Keychain is required for Core device pairing.") from None
            self._security = Security
        return self._security

    def _query(self) -> dict:
        security = self.security()
        return {
            security.kSecClass: security.kSecClassGenericPassword,
            security.kSecAttrService: KEYCHAIN_SERVICE,
            security.kSecAttrAccount: KEYCHAIN_ACCOUNT,
        }

    def load(self) -> dict | None:
        security = self.security()
        code, value = security.SecItemCopyMatching(
            {**self._query(), security.kSecReturnData: True}, None
        )
        if code == security.errSecItemNotFound:
            return None
        if code != security.errSecSuccess:
            raise RuntimeError("Could not read the Mac Core identity from Keychain.")
        try:
            result = json.loads(bytes(value))
            if set(result) != {"device_id", "private_key"}:
                raise ValueError
            return result
        except (TypeError, ValueError, UnicodeError):
            raise RuntimeError("The Mac Core identity in Keychain is invalid.") from None

    def save(self, device_id: str, private_key_pem: bytes) -> None:
        security = self.security()
        query = self._query()
        value = {
            security.kSecValueData: json.dumps(
                {"device_id": device_id, "private_key": private_key_pem.decode("ascii")}
            ).encode("utf-8"),
            security.kSecAttrAccessible: security.kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
            security.kSecAttrLabel: "Чушпан · Huawei Core device identity",
        }
        code = security.SecItemUpdate(query, value)
        if code == security.errSecItemNotFound:
            code, _ = security.SecItemAdd({**query, **value}, None)
        if code != security.errSecSuccess:
            raise RuntimeError("Mac Keychain could not save the Core device identity.")

    def delete(self) -> None:
        security = self.security()
        code = security.SecItemDelete(self._query())
        if code not in {security.errSecSuccess, security.errSecItemNotFound}:
            raise RuntimeError("Mac Keychain could not remove the Core device identity.")


class CoreAPIClient:
    def __init__(
        self,
        address: str,
        certificate_pem: str,
        *,
        identity_store: CoreKeychainIdentity | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.address = address.rstrip("/")
        self.ssl_context, self.fingerprint = _certificate(address, certificate_pem)
        self.identity_store = identity_store or CoreKeychainIdentity()
        self.identity = self.identity_store.load()
        self._client = client or httpx.AsyncClient(
            base_url=self.address,
            verify=self.ssl_context,
            timeout=httpx.Timeout(35, connect=8),
            trust_env=False,
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _jwk(private_key: ec.EllipticCurvePrivateKey) -> tuple[dict, str]:
        public = private_key.public_key()
        numbers = public.public_numbers()
        jwk = {
            "kty": "EC",
            "crv": "P-256",
            "x": _encode(numbers.x.to_bytes(32, "big")),
            "y": _encode(numbers.y.to_bytes(32, "big")),
        }
        identity = hashlib.sha256(
            public.public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).hexdigest()
        return jwk, identity

    @staticmethod
    def _body(value: dict) -> bytes:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @classmethod
    def _signature(
        cls,
        private_key: ec.EllipticCurvePrivateKey,
        method: str,
        target: str,
        timestamp: str,
        nonce: str,
        body: bytes,
    ) -> str:
        body_hash = hashlib.sha256(body).hexdigest()
        payload = f"v1\n{timestamp}\n{nonce}\n{method.upper()}\n{target}\n{body_hash}".encode()
        der = private_key.sign(payload, ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der)
        return _encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    @staticmethod
    def _load_private(identity: dict) -> ec.EllipticCurvePrivateKey:
        try:
            signing_identity = serialization.load_pem_private_key(
                identity["private_key"].encode("ascii"), password=None
            )
        except (TypeError, ValueError, KeyError):
            raise RuntimeError("Mac Core private identity is invalid.") from None
        if not isinstance(signing_identity, ec.EllipticCurvePrivateKey) or not isinstance(
            signing_identity.curve, ec.SECP256R1
        ):
            raise RuntimeError("Mac Core identity must use P-256.")
        return signing_identity

    async def pair(self, code: str) -> dict:
        if self.identity is not None or self.identity_store.load() is not None:
            raise RuntimeError(
                "A Core device identity already exists; unpair it before pairing again."
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", code or ""):
            raise ValueError("The one-time pairing code is invalid.")
        private = ec.generate_private_key(ec.SECP256R1())
        jwk, identity = self._jwk(private)
        body = self._body({"code": code, "public_jwk": jwk})
        try:
            response = await self._client.post(
                "/v1/pair", content=body, headers={"Content-Type": "application/json"}
            )
            if response.status_code != 201:
                raise RuntimeError("Huawei rejected the one-time invitation.")
            paired = response.json()
            if paired.get("device_id") != identity:
                raise RuntimeError("Huawei returned an unexpected device identity.")
            private_pem = private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            self.identity_store.save(identity, private_pem)
            self.identity = {"device_id": identity, "private_key": private_pem.decode("ascii")}
            return {
                "device_id": identity,
                "device_name": paired.get("device_name", "MacBook"),
                "fingerprint": self.fingerprint,
                "paired": True,
            }
        except httpx.HTTPError:
            raise RuntimeError("Huawei pairing endpoint could not be reached.") from None

    def _headers(self, method: str, target: str, body: bytes) -> dict[str, str]:
        if not self.identity:
            raise RuntimeError("Mac is not paired with Huawei Core.")
        private = self._load_private(self.identity)
        timestamp = str(int(time.time()))
        nonce = _encode(secrets.token_bytes(24))
        return {
            "X-Chushpan-Device": self.identity["device_id"],
            "X-Chushpan-Timestamp": timestamp,
            "X-Chushpan-Nonce": nonce,
            "X-Chushpan-Signature": self._signature(
                private, method, target, timestamp, nonce, body
            ),
        }

    async def _signed_json(self, method: str, path: str, value: dict | None = None) -> dict:
        body = self._body(value) if value is not None else b""
        headers = self._headers(method, path, body)
        if value is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = await self._client.request(method, path, content=body, headers=headers)
        except httpx.HTTPError:
            raise RuntimeError("Huawei Core request could not be reached.") from None
        if response.status_code >= 400:
            raise RuntimeError(f"Huawei Core returned HTTP {response.status_code}.")
        return response.json()

    async def health(self) -> dict:
        try:
            response = await self._client.get("/health")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError:
            raise RuntimeError("Huawei Core is unreachable over Tailscale.") from None

    async def status(self) -> dict:
        started = time.perf_counter()
        value = await self._signed_json("GET", "/v1/core/status")
        value["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return value

    async def devices(self) -> list[dict]:
        return (await self._signed_json("GET", "/v1/devices")).get("devices", [])

    async def revoke(self, device_id: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{64}", device_id):
            raise ValueError("Invalid Core device ID.")
        return await self._signed_json("DELETE", f"/v1/devices/{device_id}")

    async def list_memory(self, *, limit: int = 50) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Memory limit must be between 1 and 100.")
        return (await self._signed_json("GET", f"/v1/memory?limit={limit}")).get("entries", [])

    async def search_memory(self, query: str, *, limit: int = 20) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("Memory query must contain 1 to 200 characters.")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Memory limit must be between 1 and 50.")
        from urllib.parse import quote

        path = f"/v1/memory/search?q={quote(query, safe='')}&limit={limit}"
        return (await self._signed_json("GET", path)).get("entries", [])

    async def import_memory(self, migration_id: str, entries: list[dict]) -> dict:
        if not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            migration_id,
            re.IGNORECASE,
        ):
            raise ValueError("Invalid memory migration ID.")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 50:
            raise ValueError("Select between 1 and 50 memory entries.")
        return await self._signed_json(
            "POST",
            "/v1/memory/import",
            {"migration_id": migration_id, "entries": entries},
        )

    async def rollback_memory_import(self, migration_id: str) -> dict:
        if not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            migration_id,
            re.IGNORECASE,
        ):
            raise ValueError("Invalid memory migration ID.")
        return await self._signed_json("DELETE", f"/v1/memory/import/{migration_id}")

    @staticmethod
    def _messages(messages: list[Message]) -> list[dict]:
        result = []
        for item in messages:
            if item.get("images"):
                raise LLMError("Huawei Core text API does not yet support image attachments.")
            role = item.get("role")
            content = item.get("content")
            if role == "tool":
                result.append(
                    {
                        "role": "tool",
                        "content": str(content or ""),
                        "name": str(item.get("tool_name") or "tool"),
                        "tool_call_id": str(item.get("tool_call_id") or "missing-call-id"),
                    }
                )
            elif role == "assistant" and item.get("tool_calls"):
                calls = []
                for call in item["tool_calls"]:
                    function = call.get("function") or {}
                    args = function.get("arguments", {})
                    if not isinstance(args, str):
                        args = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
                    calls.append(
                        {
                            "id": call.get("id") or "legacy-call",
                            "type": "function",
                            "function": {"name": function.get("name", ""), "arguments": args},
                        }
                    )
                result.append({"role": "assistant", "content": content, "tool_calls": calls})
            elif role in {"system", "user", "assistant"}:
                result.append({"role": role, "content": str(content or " ")})
        return result

    async def chat_stream(
        self, messages: list[Message], tools: list | None = None
    ) -> AsyncIterator[ChatChunk]:
        converted = self._messages(messages)
        declarations = tools or []
        context = request_context.get()
        if context is None:
            request_id = str(uuid4())
        else:
            stable_body = self._body({"messages": converted, "tools": declarations})
            digest = hashlib.sha256(stable_body).hexdigest()
            request_id = str(uuid5(NAMESPACE_URL, f"{context.request_id}:{digest}"))
        body = self._body({"request_id": request_id, "messages": converted, "tools": declarations})
        path = "/v1/chat"
        headers = self._headers("POST", path, body)
        headers["Content-Type"] = "application/json"
        try:
            async with self._client.stream("POST", path, content=body, headers=headers) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise LLMError(f"Huawei Core returned HTTP {response.status_code}.")
                event_type = "message"
                completed = False
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        try:
                            data = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            raise LLMError(
                                "Huawei Core returned an invalid stream event."
                            ) from None
                        if event_type == "token":
                            yield ChatChunk(content=data.get("text", ""))
                        elif event_type == "tool_calls":
                            yield ChatChunk(tool_calls=data.get("tool_calls") or [])
                        elif event_type == "done":
                            completed = True
                            yield ChatChunk(done=True, stats={"provider": "huawei_core"})
                        elif event_type == "error":
                            raise LLMError(data.get("detail") or "Huawei Core request failed.")
                        event_type = "message"
                if not completed:
                    raise LLMError("Huawei Core ended the stream before completion.")
        except httpx.HTTPError:
            raise LLMError("Huawei Core could not be reached over Tailscale.") from None


class RemoteCoreClient:
    """LLMClient adapter; all system/audio/tools remain in the existing Mac Agent."""

    cloud_context = True
    provider = "huawei_core"

    def __init__(self, api: CoreAPIClient) -> None:
        self.api = api

    @property
    def available(self) -> bool:
        return bool(self.api.identity)

    async def chat_stream(self, messages: list[Message], tools=None) -> AsyncIterator[ChatChunk]:
        if not self.available:
            raise LLMError("Сопряги Mac Agent с Huawei в настройках Чушпана.")
        async for chunk in self.api.chat_stream(messages, tools):
            yield chunk

    async def aclose(self) -> None:
        await self.api.aclose()

    async def prime(self, system_prompt: str, tools=None, history=None) -> None:
        return

    async def warmup(self) -> None:
        return

    async def unload(self) -> None:
        return

    async def wait_ready(self, timeout_s: float, interval_s: float = 1.0) -> bool:
        return self.available


class CoreAutoClient:
    """Use Huawei when it returns no partial answer; otherwise stay on local Ollama."""

    cloud_context = True
    provider = "huawei_core_auto"

    def __init__(self, remote: RemoteCoreClient, local) -> None:
        self.remote, self.local = remote, local
        self.last_provider = None
        self.on_status = None

    async def chat_stream(self, messages: list[Message], tools=None) -> AsyncIterator[ChatChunk]:
        if not self.remote.available:
            self.last_provider = "ollama"
            async for chunk in self.local.chat_stream(messages, tools):
                yield chunk
            return
        emitted = False
        try:
            async for chunk in self.remote.chat_stream(messages, tools):
                emitted |= bool(chunk.content or chunk.tool_calls or chunk.done)
                self.last_provider = "huawei_core"
                yield chunk
        except Exception:
            if emitted:
                raise
            self.last_provider = "ollama"
            async for chunk in self.local.chat_stream(messages, tools):
                yield chunk

    async def aclose(self) -> None:
        await asyncio.gather(self.remote.aclose(), self.local.aclose(), return_exceptions=True)

    async def prime(self, system_prompt: str, tools=None, history=None) -> None:
        return

    async def warmup(self) -> None:
        if not self.remote.available:
            await self.local.warmup()

    async def unload(self) -> None:
        await self.local.unload()

    async def wait_ready(self, timeout_s: float, interval_s: float = 1.0) -> bool:
        del timeout_s, interval_s
        return True
