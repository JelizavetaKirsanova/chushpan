# Build from the locked Apple Silicon development environment.
# PyInstaller supplies SPECPATH, Analysis, PYZ, EXE, COLLECT and BUNDLE.
# ruff: noqa: F821
import json
import os
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

root = Path(SPECPATH).parents[1]
build_root = Path(os.environ["CHUSHPAN_BUILD_ROOT"])
if not any(part.endswith(".noindex") for part in build_root.parts):
    raise SystemExit("Build staging must be inside a .noindex directory")
build_info = json.loads((build_root / "metadata/build-info.json").read_text())
native = build_root / "native"
datas = [
    (str(root / "config.yaml"), "."),
    (str(root / "personality.md"), "."),
    (str(root / "frontend/dist"), "frontend/dist"),
    (str(build_root / "metadata/build-info.json"), "."),
]
datas += collect_data_files("backend", include_py_files=True)
binaries = [
    (str(native / name), "native")
    for name in (
        "permissions",
        "devices",
        "music_safari",
        "ytmusic_ax",
        "youtube_safari",
        "cinema_player",
    )
]
hiddenimports = collect_submodules("backend") + [
    "webview.platforms.cocoa",
    "webview.js",
    "ServiceManagement",
    "Security",
    "AVFoundation",
    "ApplicationServices",
    "CoreLocation",
    "Quartz",
    "UserNotifications",
    "uvicorn.logging",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.off",
]
for package in (
    "mlx",
    "mlx_whisper",
    "piper",
    "pysilero_vad",
    "openwakeword",
    "livekit",
    "bleak",
    "google.genai",
):
    data, binary, imports = collect_all(package)
    datas += data
    binaries += binary
    hiddenimports += imports
for package in ("mlx", "mlx-whisper", "piper-tts", "pysilero-vad", "pywebview", "google-genai"):
    datas += copy_metadata(package)
a = Analysis(
    [str(root / "packaging/macos/entrypoint.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=[
        "pytest",
        "tkinter",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "gi",
        "matplotlib",
        "IPython",
        "notebook",
        "torch.utils.tensorboard",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Chushpan",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch="arm64",
    codesign_identity=os.environ.get("CHUSHPAN_CODESIGN_IDENTITY") or None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Chushpan")
app = BUNDLE(
    coll,
    name="Чушпан.app",
    icon=str(root / "packaging/macos/resources/icon.icns"),
    bundle_identifier="ee.chushpan.desktop",
    info_plist={
        "CFBundleDisplayName": "Чушпан",
        "CFBundleName": "Чушпан",
        "CFBundleIconFile": "icon.icns",
        "CFBundleShortVersionString": build_info["version"],
        "CFBundleVersion": "34",
        "LSMinimumSystemVersion": "26.2",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": (
            "Чушпан распознаёт ваши голосовые команды локально на этом Mac."
        ),
        "NSBluetoothAlwaysUsageDescription": "Чушпан управляет LED-лентой по вашей команде.",
        "NSAppleEventsUsageDescription": (
            "Чушпан управляет приложениями и Командами по вашей просьбе."
        ),
        "NSLocationUsageDescription": "Чушпан определяет город только после вашего согласия.",
        "NSCalendarsFullAccessUsageDescription": "Чушпан читает события для утренней сводки.",
        "NSLocalNetworkUsageDescription": (
            "Чушпан подключает доверенные iPhone и iPad только после вашего подтверждения."
        ),
        "NSBonjourServices": ["_chushpan._tcp"],
    },
)
