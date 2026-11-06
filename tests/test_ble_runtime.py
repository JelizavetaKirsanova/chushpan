"""Read-only provenance; these tests never initialize a BLE scanner or microphone."""

import json
from pathlib import Path

from backend import ble_diagnostics
from scripts import build_macos


def test_runtime_uses_shipped_manifest_and_real_backend_type(tmp_path, monkeypatch):
    manifest = {"source_commit": "test-commit", "contains_ba705ed": True}
    (tmp_path / "build-info.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(ble_diagnostics, "RESOURCE_ROOT", tmp_path)
    report = ble_diagnostics.runtime_info()
    assert report["build"] == manifest
    assert "BleakScannerCoreBluetooth" in report["scanner_backend"]
    assert report["executable"] and report["python"] and report["bleak"]
    assert report["bluetooth_authorization"] in {
        "not_determined",
        "restricted",
        "denied",
        "granted",
        "unknown",
    }
    assert ble_diagnostics.device_key("ABC") == ble_diagnostics.device_key("abc")
    assert "ABC" not in ble_diagnostics.device_key("ABC")


def test_build_manifest_hashes_shipped_code_and_records_revision(tmp_path, monkeypatch):
    for name in [
        "backend/led.py",
        "backend/ble_diagnostics.py",
        "backend/devices/service.py",
        "frontend/src/devices.ts",
        "frontend/dist/index.html",
    ]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture source")
    monkeypatch.setattr(build_macos, "ROOT", tmp_path)
    monkeypatch.setattr(
        build_macos.subprocess,
        "check_output",
        lambda args, **kwargs: b"abcdef\n" if "rev-parse" in args else b"",
    )
    from types import SimpleNamespace

    monkeypatch.setattr(
        build_macos.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0)
    )
    result = json.loads(build_macos.build_manifest().read_text())
    assert result["source_commit"] == "abcdef"
    assert result["contains_ba705ed"] and not result["source_dirty"]
    assert len(result["sources"]["backend/led.py"]) == 64
    assert set(result["sources"]) == {
        "backend/led.py",
        "backend/ble_diagnostics.py",
        "backend/devices/service.py",
        "frontend/src/devices.ts",
        "frontend/dist/index.html",
    }
    spec = (Path(__file__).resolve().parents[1] / "packaging/macos/Chushpan.spec").read_text()
    assert "build-info.json" in spec


def test_diagnostic_launch_suppresses_autolistening_without_editing_preferences(
    tmp_path, monkeypatch
):
    from backend.desktop import app
    from backend.desktop.storage import load_preferences, save_preferences

    monkeypatch.setattr(app, "USER_ROOT", tmp_path)
    save_preferences(tmp_path, {"auto_listen": True})
    application = app.Application(no_auto_listen=True)
    assert application.prefs.auto_listen is True and application.no_auto_listen is True
    assert load_preferences(tmp_path).auto_listen is True
