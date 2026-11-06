"""Local BLE provenance and structured traces. Never scan or send control packets here."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
import sys

from backend.desktop import VERSION
from backend.paths import RESOURCE_ROOT

log = logging.getLogger(__name__)


def device_key(address: str) -> str:
    """Correlate stages without putting raw peripheral UUIDs into routine logs."""
    return hashlib.sha256(address.casefold().encode()).hexdigest()[:12]


def trace(stage: str, scan_id: str, **values) -> None:
    log.info(
        "BLE_DISCOVERY %s",
        json.dumps(
            {"stage": stage, "scan_id": scan_id, **values}, ensure_ascii=False, sort_keys=True
        ),
    )


def runtime_info() -> dict:
    """Read-only process identity and native authorization; no permission prompt."""
    from bleak.backends.scanner import get_platform_scanner_backend_type

    backend = get_platform_scanner_backend_type()
    # Bleak 3 returns (type, enum); older supported releases returned only the type.
    scanner = backend[0] if isinstance(backend, tuple) else backend
    build = {}
    try:
        build = json.loads((RESOURCE_ROOT / "build-info.json").read_text())
    except (OSError, ValueError):
        pass
    info = {
        "version": VERSION,
        "executable": sys.executable,
        "python": sys.version.split()[0],
        "pid": os.getpid(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "bleak": importlib.metadata.version("bleak"),
        "scanner_backend": scanner.__module__ + "." + scanner.__name__,
        "resource_root": str(RESOURCE_ROOT),
        "build": build,
        "bluetooth_authorization": "unknown",
    }
    if sys.platform == "darwin":
        try:
            from CoreBluetooth import CBManager
            from Foundation import NSBundle

            bundle = NSBundle.mainBundle()
            info.update(
                bundle_id=bundle.bundleIdentifier() or "unbundled",
                bundle_path=str(bundle.bundlePath()),
                bluetooth_authorization={
                    0: "not_determined",
                    1: "restricted",
                    2: "denied",
                    3: "granted",
                }.get(int(CBManager.authorization()), "unknown"),
            )
        except (ImportError, AttributeError):
            pass
    return info
