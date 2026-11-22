"""P-256 public-key identities and replay-safe request signatures."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEVICE_TYPES = {"mac", "iphone", "ipad"}
SCOPES = {"llm.chat", "memory.read", "memory.write", "device.manage"}
DEFAULT_SCOPES = {
    "mac": ["device.manage", "llm.chat", "memory.read", "memory.write"],
    "iphone": ["llm.chat", "memory.read", "memory.write"],
    "ipad": ["llm.chat", "memory.read", "memory.write"],
}
MAX_CLOCK_SKEW_SECONDS = 120


class DeviceAuthError(ValueError):
    """A pairing, identity, signature, or replay check failed."""


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode(value: str, *, limit: int = 512) -> bytes:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError("Invalid base64url value.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid base64url value.")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, base64.binascii.Error):
        raise ValueError("Invalid base64url value.") from None


def public_key(jwk: dict) -> tuple[ec.EllipticCurvePublicKey, dict, str]:
    if (
        not isinstance(jwk, dict)
        or jwk.get("kty") != "EC"
        or jwk.get("crv") != "P-256"
        or "d" in jwk
    ):
        raise ValueError("A public P-256 key is required.")
    x, y = decode(jwk.get("x", "")), decode(jwk.get("y", ""))
    if len(x) != 32 or len(y) != 32:
        raise ValueError("Invalid P-256 public key.")
    key = ec.EllipticCurvePublicNumbers(
        int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()
    ).public_key()
    canonical = {"kty": "EC", "crv": "P-256", "x": encode(x), "y": encode(y)}
    identity = hashlib.sha256(
        key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).hexdigest()
    return key, canonical, identity


def request_payload(method: str, target: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"v1\n{timestamp}\n{nonce}\n{method.upper()}\n{target}\n{body_hash}".encode()


def verify_signature(jwk: dict, signature: str, payload: bytes) -> None:
    key, _, _ = public_key(jwk)
    raw = decode(signature)
    if len(raw) != 64:
        raise ValueError("Invalid ECDSA signature.")
    der = utils.encode_dss_signature(int.from_bytes(raw[:32]), int.from_bytes(raw[32:]))
    try:
        key.verify(der, payload, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise ValueError("Device signature was not verified.") from None


class DeviceRegistry:
    """Versioned SQLite registry for one-time invitations, device grants, and nonces."""

    schema_version = 2

    def __init__(self, path: str | Path) -> None:
        self.db = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > self.schema_version:
            raise RuntimeError("Core database schema is newer than this server release.")
        if version < 1:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for statement in (
                    "CREATE TABLE pairing_invites ("
                    "code_hash TEXT PRIMARY KEY, device_name TEXT NOT NULL, "
                    "device_type TEXT NOT NULL, scopes TEXT NOT NULL, created_at REAL NOT NULL, "
                    "expires_at REAL NOT NULL, used_at REAL)",
                    "CREATE TABLE devices ("
                    "device_id TEXT PRIMARY KEY, device_name TEXT NOT NULL, "
                    "device_type TEXT NOT NULL, public_jwk TEXT NOT NULL, scopes TEXT NOT NULL, "
                    "created_at REAL NOT NULL, last_seen REAL, revoked_at REAL)",
                    "CREATE TABLE request_nonces ("
                    "device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE, "
                    "nonce TEXT NOT NULL, created_at REAL NOT NULL, "
                    "PRIMARY KEY(device_id, nonce))",
                    "CREATE INDEX request_nonces_expiry ON request_nonces(created_at)",
                ):
                    self.db.execute(statement)
                self.db.execute("PRAGMA user_version=1")
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            version = 1
        if version < 2:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "CREATE TABLE request_ids ("
                    "device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE, "
                    "request_id TEXT NOT NULL, body_hash TEXT NOT NULL, created_at REAL NOT NULL, "
                    "status TEXT NOT NULL CHECK(status IN "
                    "('running','completed','interrupted','failed')), "
                    "PRIMARY KEY(device_id,request_id))"
                )
                self.db.execute("CREATE INDEX request_ids_expiry ON request_ids(created_at)")
                self.db.execute("PRAGMA user_version=2")
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _validated_device(device_name: str, device_type: str) -> tuple[str, str]:
        if device_type not in DEVICE_TYPES:
            raise ValueError("Device type must be mac, iphone, or ipad.")
        if not isinstance(device_name, str):
            raise ValueError("Device name is required.")
        name = " ".join(device_name.split())
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise ValueError("Device name must contain 1 to 80 printable characters.")
        return name, device_type

    @staticmethod
    def _validated_scopes(scopes: list[str] | tuple[str, ...]) -> list[str]:
        if not isinstance(scopes, (list, tuple)) or len(scopes) > len(SCOPES):
            raise ValueError("Invalid device scopes.")
        if any(not isinstance(scope, str) or scope not in SCOPES for scope in scopes):
            raise ValueError("Unknown device scope.")
        return sorted(set(scopes))

    def issue_invite(
        self,
        device_name: str,
        device_type: str,
        *,
        scopes: list[str] | None = None,
        ttl_seconds: int = 300,
        now: float | None = None,
    ) -> dict:
        name, kind = self._validated_device(device_name, device_type)
        grants = self._validated_scopes(scopes if scopes is not None else DEFAULT_SCOPES[kind])
        if type(ttl_seconds) is not int or not 30 <= ttl_seconds <= 600:
            raise ValueError("Invitation lifetime must be from 30 to 600 seconds.")
        issued = time.time() if now is None else now
        code = secrets.token_urlsafe(24)
        digest = hashlib.sha256(code.encode("ascii")).hexdigest()
        with self.db:
            self.db.execute(
                "INSERT INTO pairing_invites("
                "code_hash,device_name,device_type,scopes,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?)",
                (digest, name, kind, json.dumps(grants), issued, issued + ttl_seconds),
            )
        return {
            "code": code,
            "expires_at": issued + ttl_seconds,
            "device_name": name,
            "scopes": grants,
        }

    def pair(self, code: str, jwk: dict, *, now: float | None = None) -> dict:
        if (
            not isinstance(code, str)
            or not 32 <= len(code) <= 64
            or not re.fullmatch(r"[A-Za-z0-9_-]+", code)
        ):
            raise DeviceAuthError("Invitation is invalid or expired.")
        try:
            _, canonical, identity = public_key(jwk)
        except (TypeError, ValueError):
            raise DeviceAuthError("A valid public P-256 key is required.") from None
        at = time.time() if now is None else now
        digest = hashlib.sha256(code.encode("ascii", errors="strict")).hexdigest()
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                invite = self.db.execute(
                    "SELECT * FROM pairing_invites WHERE code_hash=? "
                    "AND used_at IS NULL AND expires_at>?",
                    (digest, at),
                ).fetchone()
                if invite is None:
                    raise DeviceAuthError("Invitation is invalid or expired.")
                self.db.execute(
                    "UPDATE pairing_invites SET used_at=? WHERE code_hash=? AND used_at IS NULL",
                    (at, digest),
                )
                self.db.execute(
                    "INSERT INTO devices("
                    "device_id,device_name,device_type,public_jwk,scopes,created_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET "
                    "device_name=excluded.device_name,device_type=excluded.device_type,"
                    "public_jwk=excluded.public_jwk,scopes=excluded.scopes,"
                    "created_at=excluded.created_at,"
                    "last_seen=NULL,revoked_at=NULL",
                    (
                        identity,
                        invite["device_name"],
                        invite["device_type"],
                        json.dumps(canonical, separators=(",", ":")),
                        invite["scopes"],
                        at,
                    ),
                )
        except sqlite3.IntegrityError:
            raise DeviceAuthError("Invitation has already been used.") from None
        return self.device(identity)

    def device(self, identity: str) -> dict:
        row = self.db.execute(
            "SELECT device_id,device_name,device_type,scopes,created_at,last_seen,revoked_at "
            "FROM devices WHERE device_id=?",
            (identity,),
        ).fetchone()
        if row is None:
            raise DeviceAuthError("Device is not paired.")
        value = dict(row)
        value["scopes"] = json.loads(value["scopes"])
        value["active"] = value["revoked_at"] is None
        return value

    def authenticate(
        self,
        *,
        device_id: str,
        timestamp: str,
        nonce: str,
        signature: str,
        method: str,
        target: str,
        body: bytes,
        now: float | None = None,
    ) -> dict:
        if not re.fullmatch(r"[a-f0-9]{64}", device_id or ""):
            raise DeviceAuthError("Device authentication required.")
        if not re.fullmatch(r"[0-9]{10,13}", timestamp or ""):
            raise DeviceAuthError("Device authentication required.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", nonce or ""):
            raise DeviceAuthError("Device authentication required.")
        at = time.time() if now is None else now
        stamped = int(timestamp)
        if abs(at - stamped) > MAX_CLOCK_SKEW_SECONDS:
            raise DeviceAuthError("Request timestamp is outside the allowed window.")
        row = self.db.execute(
            "SELECT public_jwk,scopes,device_name,device_type,revoked_at "
            "FROM devices WHERE device_id=?",
            (device_id,),
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise DeviceAuthError("Device is not active.")
        payload = request_payload(method, target, timestamp, nonce, body)
        try:
            verify_signature(json.loads(row["public_jwk"]), signature, payload)
        except (TypeError, ValueError):
            raise DeviceAuthError("Device signature was not verified.") from None
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                active = self.db.execute(
                    "SELECT revoked_at FROM devices WHERE device_id=?", (device_id,)
                ).fetchone()
                if active is None or active["revoked_at"] is not None:
                    raise DeviceAuthError("Device is not active.")
                self.db.execute(
                    "INSERT INTO request_nonces(device_id,nonce,created_at) VALUES(?,?,?)",
                    (device_id, nonce, at),
                )
                self.db.execute("DELETE FROM request_nonces WHERE created_at<?", (at - 600,))
                self.db.execute("UPDATE devices SET last_seen=? WHERE device_id=?", (at, device_id))
        except sqlite3.IntegrityError:
            raise DeviceAuthError("Request replay was rejected.") from None
        return {
            "device_id": device_id,
            "device_name": row["device_name"] if "device_name" in row.keys() else "",
            "device_type": row["device_type"] if "device_type" in row.keys() else "",
            "scopes": json.loads(row["scopes"]),
        }

    def list_devices(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT device_id,device_name,device_type,scopes,created_at,last_seen,revoked_at "
            "FROM devices ORDER BY created_at,device_name"
        ).fetchall()
        result = []
        for row in rows:
            device = dict(row)
            device["scopes"] = json.loads(device["scopes"])
            device["active"] = device["revoked_at"] is None
            result.append(device)
        return result

    def revoke(self, identity: str, *, now: float | None = None) -> bool:
        at = time.time() if now is None else now
        with self.db:
            cursor = self.db.execute(
                "UPDATE devices SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL",
                (at, identity),
            )
        return cursor.rowcount > 0

    def claim_request(
        self, device_id: str, request_id: str, body_hash: str, *, now: float | None = None
    ) -> bool:
        """Atomically record an LLM request ID so a retry cannot repeat a paid call."""
        at = time.time() if now is None else now
        with self._lock, self.db:
            self.db.execute("DELETE FROM request_ids WHERE created_at<?", (at - 7 * 86400,))
            row = self.db.execute(
                "SELECT body_hash,status FROM request_ids WHERE device_id=? AND request_id=?",
                (device_id, request_id),
            ).fetchone()
            if row is not None:
                if row["body_hash"] != body_hash:
                    raise DeviceAuthError("Request ID was already used for different content.")
                raise DeviceAuthError(
                    f"Request ID was already submitted ({row['status']}); use a new ID to retry."
                )
            self.db.execute(
                "INSERT INTO request_ids(device_id,request_id,body_hash,created_at,status) "
                "VALUES(?,?,?,?, 'running')",
                (device_id, request_id, body_hash, at),
            )
        return True

    def finish_request(self, device_id: str, request_id: str, status: str) -> None:
        if status not in {"completed", "interrupted", "failed"}:
            raise ValueError("Invalid LLM request status.")
        with self._lock, self.db:
            self.db.execute(
                "UPDATE request_ids SET status=? WHERE device_id=? AND request_id=? "
                "AND status='running'",
                (status, device_id, request_id),
            )


def tls_paths() -> tuple[Path, Path, Path]:
    root = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    directory = root / "Chushpan" / "CoreServer" / "tls"
    return (
        directory / "server-cert.pem",
        directory / "server-key.dpapi",
        directory / "server-cert.sha256",
    )


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    """Protect data with machine-scope DPAPI on Windows."""
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is available only on the Core Server host.")
    source = ctypes.create_string_buffer(data, len(data))
    input_blob = _DataBlob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if protect:
        function = crypt32.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DataBlob),
        ]
        function.restype = ctypes.c_int
        ok = function(
            ctypes.byref(input_blob),
            "Chushpan Core TLS key",
            None,
            None,
            None,
            0x4,
            ctypes.byref(output_blob),
        )
    else:
        description = ctypes.c_wchar_p()
        function = crypt32.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DataBlob),
        ]
        function.restype = ctypes.c_int
        ok = function(
            ctypes.byref(input_blob),
            ctypes.byref(description),
            None,
            None,
            None,
            0,
            ctypes.byref(output_blob),
        )
    if not ok:
        raise OSError(ctypes.get_last_error(), "Windows DPAPI operation failed")
    try:
        value = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        ctypes.memset(output_blob.pbData, 0, output_blob.cbData)
        return value
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(output_blob.pbData)


def _protect_tls_key(key_bytes: bytes) -> bytes:
    return _dpapi(key_bytes, protect=True)


def _unprotect_tls_key(encrypted: bytes) -> bytes:
    return _dpapi(encrypted, protect=False)


def tls_private_key_pem() -> bytes:
    """Decrypt the TLS key only for loading into the in-memory SSL context."""
    _, key_path, _ = tls_paths()
    return _unprotect_tls_key(key_path.read_bytes())


def ensure_tls_certificate(host: str) -> dict:
    """Create a pinned self-signed P-256 TLS certificate for a Tailscale IPv4 address."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("TLS host must be an IPv4 address in the Tailscale range.") from None
    if address.version != 4 or address not in ipaddress.ip_network("100.64.0.0/10"):
        raise ValueError("TLS host must be an IPv4 address in the Tailscale range.")
    cert_path, key_path, fingerprint_path = tls_paths()
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    present = (cert_path.exists(), key_path.exists())
    if any(present) and not all(present):
        raise RuntimeError("An incomplete TLS identity exists; rotate it explicitly over SSH.")
    if all(present):
        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
        private_key = serialization.load_pem_private_key(
            _unprotect_tls_key(key_path.read_bytes()), password=None
        )
        if private_key.public_key().public_numbers() != certificate.public_key().public_numbers():
            raise RuntimeError("TLS certificate and private key do not match.")
        try:
            san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            addresses = san.value.get_values_for_type(x509.IPAddress)
        except x509.ExtensionNotFound:
            addresses = []
        now = datetime.now(UTC)
        if address not in addresses or certificate.not_valid_after_utc <= now + timedelta(days=7):
            raise RuntimeError("TLS identity is expired or bound to another Tailscale address.")
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Chushpan Core Server")])
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(address)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256())
        )
        key_bytes = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        cert_bytes = certificate.public_bytes(serialization.Encoding.PEM)
        protected_key = _protect_tls_key(key_bytes)
        key_tmp, cert_tmp = key_path.with_suffix(".dpapi.tmp"), cert_path.with_suffix(".pem.tmp")
        key_tmp.write_bytes(protected_key)
        cert_tmp.write_bytes(cert_bytes)
        key_tmp.replace(key_path)
        cert_tmp.replace(cert_path)
    fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
    fingerprint_path.write_text(fingerprint + "\n", encoding="ascii")
    return {"certificate": str(cert_path), "private_key": str(key_path), "fingerprint": fingerprint}
