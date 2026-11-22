"""Single API worker supervised by :mod:`chushpan_core`."""

from __future__ import annotations

import argparse
import ssl
import tempfile
from pathlib import Path

import uvicorn

from chushpan_core.app import app, configure_logging
from chushpan_core.identity import tls_paths, tls_private_key_pem


def minimum_tls_12(config, _create_default):
    """Load the DPAPI key through a short-lived file and retain only it in memory."""
    certificate, encrypted_key, _ = tls_paths()
    if not certificate.is_file() or not encrypted_key.is_file():
        raise RuntimeError("The pinned Core TLS identity has not been provisioned over SSH.")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".pem", prefix="core-tls-", dir=certificate.parent, delete=False
        ) as temporary_key:
            temporary_path = Path(temporary_key.name)
            temporary_key.write(tls_private_key_pem())
            temporary_key.flush()
        context.load_cert_chain(certfile=str(certificate), keyfile=str(temporary_path))
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Chushpan Core API worker")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    configure_logging()
    certificate, private_key, _ = tls_paths()
    if not certificate.is_file() or not private_key.is_file():
        raise RuntimeError("The pinned Core TLS identity has not been provisioned over SSH.")
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        ssl_certfile=str(certificate),
        ssl_keyfile=str(private_key),
        ssl_context_factory=minimum_tls_12,
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
