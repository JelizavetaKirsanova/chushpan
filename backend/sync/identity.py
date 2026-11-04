"""TLS identity in macOS Keychain; clients authenticate with their own P-256 keys."""

import base64
import hashlib
import ipaddress
import json
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.x509.oid import NameOID


def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def decode(value):
    if not isinstance(value, str) or len(value) > 10000:
        raise ValueError("Некорректная подпись устройства.")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def public_key(jwk):
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256" or "d" in jwk:
        raise ValueError("Нужен публичный ключ P-256.")
    x, y = decode(jwk.get("x")), decode(jwk.get("y"))
    if len(x) != 32 or len(y) != 32:
        raise ValueError("Некорректный ключ P-256.")
    key = ec.EllipticCurvePublicNumbers(int.from_bytes(x), int.from_bytes(y), ec.SECP256R1()).public_key()
    canonical = {"kty": "EC", "crv": "P-256", "x": encode(x), "y": encode(y)}
    identity = hashlib.sha256(key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()
    return key, canonical, identity


def verify(jwk, signature, payload):
    key, _, _ = public_key(jwk)
    raw = decode(signature)
    if len(raw) != 64:
        raise ValueError("Некорректная подпись устройства.")
    der = utils.encode_dss_signature(int.from_bytes(raw[:32]), int.from_bytes(raw[32:]))
    try:
        key.verify(der, payload.encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise ValueError("Подпись устройства не подтверждена.") from None


class KeychainIdentity:
    def read(self):
        import Security as s

        query = {s.kSecClass: s.kSecClassGenericPassword, s.kSecAttrService: "ee.chushpan.device-sync", s.kSecAttrAccount: "tls-identity"}
        code, data = s.SecItemCopyMatching({**query, s.kSecReturnData: True}, None)
        if code == s.errSecSuccess:
            return json.loads(bytes(data))
        if code != s.errSecItemNotFound:
            raise ValueError("Связка ключей недоступна. Сервер синхронизации не запущен.")
        ca_key = ec.generate_private_key(ec.SECP256R1())
        server_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Chushpan Local CA " + encode(__import__('secrets').token_bytes(8)))])
        now = datetime.now(UTC)
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5))
              .not_valid_after(now+timedelta(days=3650)).add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                          data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                          crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
              .sign(ca_key, hashes.SHA256()))
        pem = lambda key: key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        identity = {"ca_key": pem(ca_key), "server_key": pem(server_key), "ca": ca.public_bytes(serialization.Encoding.PEM).decode()}
        code, _ = s.SecItemAdd({**query, s.kSecValueData: json.dumps(identity).encode(),
                               s.kSecAttrAccessible: s.kSecAttrAccessibleWhenUnlockedThisDeviceOnly}, None)
        if code != s.errSecSuccess:
            raise ValueError("Не удалось сохранить TLS-ключ в Keychain. Незащищённый сервер не запускался.")
        return identity


def certificates(identity, host):
    ca = x509.load_pem_x509_certificate(identity["ca"].encode())
    ca_key = serialization.load_pem_private_key(identity["ca_key"].encode(), password=None)
    server_key = serialization.load_pem_private_key(identity["server_key"].encode(), password=None)
    now = datetime.now(UTC)
    certificate = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Chushpan Mac")]))
                   .issuer_name(ca.subject).public_key(server_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=365))
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(host))]), critical=False)
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                   .sign(ca_key, hashes.SHA256()))
    return {"certificate": certificate.public_bytes(serialization.Encoding.PEM),
            "key": identity["server_key"].encode(), "ca": ca.public_bytes(serialization.Encoding.DER),
            "fingerprint": ca.fingerprint(hashes.SHA256()).hex()}
