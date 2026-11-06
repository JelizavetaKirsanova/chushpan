"""Frozen entrypoint and development command: python -m backend.desktop."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--desktop-worker", action="store_true")
    parser.add_argument("--endpoint", type=Path)
    parser.add_argument("--parent", type=int, default=0)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument(
        "--no-auto-listen",
        action="store_true",
        help="suppress microphone auto-start for this launch; keep preferences",
    )
    parser.add_argument("--self-test", type=Path)
    parser.add_argument("--permission-status", type=Path)
    parser.add_argument("--ble-runtime", type=Path, help="read-only BLE runtime identity, no scan")
    args = parser.parse_args()
    os.environ["CHUSHPAN_DESKTOP"] = "1"
    os.umask(0o077)
    if args.ble_runtime:
        from backend.ble_diagnostics import runtime_info

        args.ble_runtime.write_text(json.dumps(runtime_info(), ensure_ascii=False, indent=2))
        return 0
    if args.permission_status:
        # Installed bundle identity + read-only native preflights. No UI, audio,
        # models, event scripts, location fixes, network probes or permission requests.
        from backend.config import get_settings
        from backend.permission_center import PermissionCenter

        report = asyncio.run(PermissionCenter(get_settings()).report())
        args.permission_status.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.self_test:
        # Import shipped dynamic packages without recording, playback, BLE or models.
        import importlib

        modules = [
            "backend.voice",
            "webview",
            "sounddevice",
            "pysilero_vad",
            "mlx_whisper",
            "piper",
            "torch",
            "bleak",
            "livekit",
            "ServiceManagement",
            "UserNotifications",
            "backend.personal_modules",
            "backend.sync.service",
            "backend.cinema.playback",
            "backend.cinema.native_player",
            "cryptography.x509",
        ]
        result = {}
        for module in modules:
            try:
                importlib.import_module(module)
                result[module] = "ok"
            except Exception as exc:
                result[module] = type(exc).__name__ + ": " + str(exc)
        args.self_test.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return int(any(v != "ok" for v in result.values()))
    if args.desktop_worker:
        from backend.desktop.worker import run_worker
        from backend.logging_setup import setup_logging

        setup_logging()
        import logging
        from logging.handlers import RotatingFileHandler

        from backend.paths import USER_ROOT

        folder = USER_ROOT / "data/logs"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        handler = RotatingFileHandler(folder / "engine.log", maxBytes=5_000_000, backupCount=1)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.getLogger().addHandler(handler)
        return (
            asyncio.run(
                run_worker(
                    os.environ["CHUSHPAN_CONTROL_TOKEN"],
                    args.endpoint,
                    smoke=args.smoke,
                    parent=args.parent,
                )
            )
            or 0
        )
    from backend.desktop.app import Application

    return Application(smoke=args.smoke, no_auto_listen=args.no_auto_listen).run()


if __name__ == "__main__":
    sys.exit(main())
