"""Reproducible, allowlisted standalone Apple Silicon app build. No user data."""

from __future__ import annotations

import importlib.util
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    subprocess.run(args, cwd=ROOT, check=True)


def main():
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SystemExit("Сборка требует macOS на Apple Silicon и arm64 Python.")
    for module in ("PyInstaller", "ServiceManagement", "AVFoundation", "CoreLocation"):
        if importlib.util.find_spec(module) is None:
            raise SystemExit(f"Нет {module}. Выполни uv sync --group macos-build --locked.")
    for executable in ("xcrun", "codesign", "iconutil", "npm"):
        if not shutil.which(executable):
            raise SystemExit(f"Не найден {executable}. Установи Command Line Tools / Node.js.")
    run("npm", "--prefix", "frontend", "ci")
    run("npm", "--prefix", "frontend", "run", "build")
    native = ROOT / "build/macos/native"
    native.mkdir(parents=True, exist_ok=True)
    for name, source in [
        ("permissions", "backend/permissions.swift"),
        ("ytmusic_ax", "backend/media/ytmusic_ax.swift"),
        ("youtube_safari", "backend/media/youtube_safari.swift"),
        ("music_safari", "backend/media/music_safari.swift"),
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
        if name == "music_safari":
            args += [
                "-Xlinker",
                "-sectcreate",
                "-Xlinker",
                "__TEXT",
                "-Xlinker",
                "__info_plist",
                "-Xlinker",
                str(ROOT / "backend/media/music_safari.plist"),
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
    run("codesign", "--force", "--sign", "-", str(helper.parent))
    from PIL import Image

    from backend.app_bundle import icon_pixels

    Image.fromarray(icon_pixels()).save(ROOT / "build/macos/icon.icns", format="ICNS")
    run(
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--workpath",
        str(ROOT / "build/macos/pyinstaller"),
        str(ROOT / "packaging/macos/Chushpan.spec"),
    )
    app = ROOT / "dist/Чушпан.app"
    helpers = app / "Contents/Helpers"
    helpers.mkdir(exist_ok=True)
    shutil.copytree(helper.parent, helpers / helper.parent.name, dirs_exist_ok=True)
    run("codesign", "--force", "--sign", "-", "--identifier", "ee.chushpan.desktop", str(app))
    run("codesign", "--verify", "--deep", "--strict", str(app))
    forbidden = {".env", "config.local.yaml", "jarvis.db", "application.json"}
    for path in app.rglob("*"):
        public_ca = path.name == "cacert.pem" and "certifi" in path.parts
        if path.name in forbidden or (
            path.suffix in {".db", ".sqlite", ".key", ".pem"} and not public_ca
        ):
            raise SystemExit(f"Проверка приватности не пройдена: {path.name}")
    print(f"Готово: {app}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Сборка остановлена: {exc.cmd[0]} вернул {exc.returncode}") from exc
