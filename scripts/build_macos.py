"""Reproducible, allowlisted standalone Apple Silicon app build. No user data."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from backend.desktop import VERSION
from scripts.macos_release import (
    BUILD_RECEIPT,
    new_build_directory,
    update_state,
    verify_bundle,
    write_receipt,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SOURCES = (
    "backend/cinema/service.py",
    "backend/cinema/sources.py",
    "backend/cinema/internet.py",
    "backend/cinema/browser_search.py",
    "backend/cinema/learned_player.py",
    "backend/cinema/site_adapters.py",
    "backend/cinema/keychain.py",
    "backend/cinema/tavily.py",
    "backend/media/ytmusic_ax.swift",
    "backend/cinema/site_browser.py",
    "backend/cinema/browser_diagnostics.py",
    "backend/cinema/evidence.py",
    "backend/cinema/archive_open.py",
    "backend/cinema/adapters.py",
    "backend/cinema/player.js",
    "backend/cinema/classification.py",
    "backend/cinema/models.py",
    "backend/cinema/public_search.py",
    "backend/cinema/playback.py",
    "backend/cinema/store.py",
    "backend/cinema/native_player.py",
    "frontend/src/cinema.ts",
    "frontend/src/cinema_viewer.ts",
    "frontend/src/cinema_browser_diagnostics.ts",
    "frontend/src/cinema_source_diagnostics.ts",
    "frontend/src/cinema_player.ts",
    "frontend/src/cinema.css",
    "backend/led.py",
    "backend/ble_diagnostics.py",
    "backend/ble_profiles.py",
    "backend/devices/center.py",
    "backend/devices/service.py",
    "backend/devices/colors.py",
    "backend/devices/lighting.py",
    "backend/devices/lighting_models.py",
    "backend/devices/lighting_schedule.py",
    "backend/devices/lighting_voice.py",
    "backend/memory/store.py",
    "backend/memory/intelligence.py",
    "backend/memory/library.py",
    "backend/settings_center.py",
    "backend/hud/server.py",
    "backend/sync/service.py",
    "frontend/src/devices.ts",
    "frontend/src/color_editor.ts",
    "frontend/src/devices.css",
    "frontend/src/lighting.ts",
    "frontend/src/lighting.css",
    "frontend/src/memory_library.ts",
    "frontend/src/module_ui.ts",
    "frontend/dist/index.html",
    "packaging/macos/Chushpan.spec",
)


def run(*args, env=None):
    subprocess.run(args, cwd=ROOT, check=True, env=env)


def build_manifest(build_root: Path | None = None) -> Path:
    """Stamp shipped sources, rather than trusting a manually incremented version."""

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT).decode().strip()

    path = (build_root or ROOT / "build/macos") / "metadata/build-info.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": VERSION,
                "source_commit": git("rev-parse", "HEAD"),
                "source_dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
                "built_at": datetime.now(UTC).isoformat(),
                "contains_ba705ed": subprocess.run(
                    ["git", "merge-base", "--is-ancestor", "ba705ed", "HEAD"], cwd=ROOT
                ).returncode
                == 0,
                "sources": {
                    name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                    for name in MANIFEST_SOURCES
                },
            },
            indent=2,
        )
        + "\n"
    )
    return path


def build() -> Path:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SystemExit("Сборка требует macOS на Apple Silicon и arm64 Python.")
    identity_file = update_state() / "signing-identity"
    signing_identity = os.environ.get("CHUSHPAN_CODESIGN_IDENTITY")
    if not signing_identity and identity_file.exists():
        signing_identity = identity_file.read_text().strip()
        if not signing_identity or signing_identity == "-":
            raise SystemExit("Некорректная сохранённая идентичность подписи Чушпана.")
    signing_identity = signing_identity or "-"
    if signing_identity != "-":
        identities = subprocess.check_output(
            ["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"], text=True
        )
        if signing_identity.casefold() not in identities.casefold():
            raise SystemExit("Сохранённый сертификат подписи Чушпана недоступен в Связке ключей.")
    for module in ("PyInstaller", "ServiceManagement", "AVFoundation", "CoreLocation"):
        if importlib.util.find_spec(module) is None:
            raise SystemExit(f"Нет {module}. Выполни uv sync --group macos-build --locked.")
    for executable in ("xcrun", "codesign", "iconutil", "npm"):
        if not shutil.which(executable):
            raise SystemExit(f"Не найден {executable}. Установи Command Line Tools / Node.js.")
    run(sys.executable, "-m", "scripts.prepare_macos_icon")
    run("npm", "--prefix", "frontend", "ci")
    run("npm", "--prefix", "frontend", "run", "build")
    build_root = new_build_directory()
    build_manifest(build_root)
    native = build_root / "native"
    native.mkdir(parents=True, exist_ok=True)
    for name, source in [
        ("permissions", "backend/permissions.swift"),
        ("devices", "backend/devices/native.swift"),
        ("ytmusic_ax", "backend/media/ytmusic_ax.swift"),
        ("youtube_safari", "backend/media/youtube_safari.swift"),
        ("music_safari", "backend/media/music_safari.swift"),
        ("cinema_player", "backend/cinema/avplayer.swift"),
    ]:
        args = [
            "xcrun",
            "swiftc",
            "-O",
            "-module-cache-path",
            str(ROOT / "build/macos/swift-cache"),
            str(ROOT / source),
            "-o",
            str(native / name),
        ]
        if name in {"music_safari", "devices"}:
            args += [
                "-Xlinker",
                "-sectcreate",
                "-Xlinker",
                "__TEXT",
                "-Xlinker",
                "__info_plist",
                "-Xlinker",
                str(
                    ROOT
                    / (
                        "backend/devices/native.plist"
                        if name == "devices"
                        else "backend/media/music_safari.plist"
                    )
                ),
            ]
        elif name in {"ytmusic_ax", "youtube_safari"}:
            from backend.native_identity import helper_info

            info = native / (name + ".plist")
            info.write_bytes(helper_info(name))
            args += [
                "-Xlinker",
                "-sectcreate",
                "-Xlinker",
                "__TEXT",
                "-Xlinker",
                "__info_plist",
                "-Xlinker",
                str(info),
            ]
        run(*args)
    helper = native / "Чушпан — утро.app/Contents"
    (helper / "MacOS").mkdir(parents=True, exist_ok=True)
    (helper / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "local.chushpan.personal",
                "CFBundleExecutable": "chushpan-personal",
                "CFBundleName": "Чушпан — утро",
                "CFBundlePackageType": "APPL",
                "CFBundleVersion": "1",
                "LSUIElement": True,
                "NSCalendarsFullAccessUsageDescription": "Чушпан читает события для сводки.",
                "NSLocationUsageDescription": "Чушпан определяет город для погоды и сценариев.",
                "NSLocationWhenInUseUsageDescription": (
                    "Чушпан определяет город для погоды и сценариев."
                ),
            }
        )
    )
    run(
        "xcrun",
        "swiftc",
        "-O",
        "-module-cache-path",
        str(ROOT / "build/macos/swift-cache"),
        str(ROOT / "backend/personal.swift"),
        "-o",
        str(helper / "MacOS/chushpan-personal"),
    )
    run("codesign", "--force", "--sign", signing_identity, str(helper.parent))
    run(
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--workpath",
        str(build_root / "pyinstaller"),
        "--distpath",
        str(build_root / "dist"),
        str(ROOT / "packaging/macos/Chushpan.spec"),
        env={
            **os.environ,
            "CHUSHPAN_BUILD_ROOT": str(build_root),
            "CHUSHPAN_CODESIGN_IDENTITY": signing_identity if signing_identity != "-" else "",
        },
    )
    app = build_root / "dist/Чушпан.app"
    helpers = app / "Contents/Helpers"
    helpers.mkdir(exist_ok=True)
    shutil.copytree(helper.parent, helpers / helper.parent.name, dirs_exist_ok=True)
    for name in ("music_safari", "youtube_safari", "ytmusic_ax"):
        binary = (app / "Contents/Resources/native" / name).resolve()
        options = (
            ["--entitlements", str(ROOT / "packaging/macos/automation.entitlements.plist")]
            if name != "ytmusic_ax"
            else []
        )
        run(
            "codesign",
            "--force",
            "--sign",
            signing_identity,
            "--identifier",
            "local.chushpan." + name.replace("_", "-"),
            *options,
            str(binary),
        )
    run(
        "codesign",
        "--force",
        "--sign",
        signing_identity,
        "--identifier",
        "ee.chushpan.desktop",
        "--entitlements",
        str(ROOT / "packaging/macos/automation.entitlements.plist"),
        str(app),
    )
    run("codesign", "--verify", "--deep", "--strict", str(app))
    receipt = verify_bundle(app)
    if receipt["version"] != VERSION:
        raise SystemExit("Версия пакета macOS отличается от версии исходников.")
    write_receipt(BUILD_RECEIPT, receipt)
    print(f"Сборка проверена: {app}. Для обновления: python -m scripts.install_macos")
    return app


def main():
    build()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Сборка остановлена: {exc.cmd[0]} вернул {exc.returncode}") from exc
