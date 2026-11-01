"""Read-only access to this Mac's location and Apple Calendar, through public APIs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import plistlib
import tempfile
from pathlib import Path

from backend.config import ROOT_DIR, Settings
from backend.media.safari import process
from backend.tools.registry import ToolError


class PersonalContext:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self._lock = asyncio.Lock()

    async def helper(self) -> Path:
        source = ROOT_DIR / "backend/personal.swift"
        cache = self.s.data_path / "native"
        app = cache / "Чушпан — утро.app"
        contents = app / "Contents"
        binary = contents / "MacOS" / "chushpan-personal"
        stamp = cache / "personal-version"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if binary.exists() and stamp.exists() and stamp.read_text() == digest:
            return app
        binary.parent.mkdir(parents=True, exist_ok=True)
        info = {
            "CFBundleIdentifier": "local.chushpan.personal",
            "CFBundleName": "Чушпан — утро",
            "CFBundleDisplayName": "Чушпан — утро",
            "CFBundleExecutable": binary.name,
            "CFBundlePackageType": "APPL",
            "CFBundleVersion": "1",
            "LSUIElement": True,
            "NSCalendarsFullAccessUsageDescription": (
                "Чушпан читает события на сегодня и завтра для утренней сводки. "
                "События не изменяются."
            ),
            "NSLocationUsageDescription": (
                "Чушпан узнаёт погоду там, где сейчас находится твой Mac."
            ),
            "NSLocationWhenInUseUsageDescription": (
                "Чушпан узнаёт погоду там, где сейчас находится твой Mac."
            ),
        }
        (contents / "Info.plist").write_bytes(plistlib.dumps(info))
        await process(
            "xcrun",
            "swiftc",
            "-module-cache-path",
            str(cache / "swift-cache"),
            str(source),
            "-o",
            str(binary),
            timeout=90,
        )
        await process("codesign", "--force", "--sign", "-", str(app), timeout=20)
        stamp.write_text(digest)
        return app

    async def read(self, *, request_access: bool = True) -> dict:
        async with self._lock:
            app = await self.helper()
            # Only the requested two-day window is returned. Temporary data is removed
            # after each invocation; calendars and coordinates aren't saved as facts.
            with tempfile.TemporaryDirectory(prefix="chushpan-morning-") as tmp:
                request, result = Path(tmp) / "request.json", Path(tmp) / "result.json"
                request.write_text(json.dumps({"request_access": request_access}))
                try:
                    await process(
                        "open",
                        "-W",
                        "-n",
                        "-g",
                        str(app),
                        "--args",
                        str(request),
                        str(result),
                        timeout=65,
                    )
                except TimeoutError as exc:
                    raise ToolError("macOS не ответила на запрос календаря и геопозиции.") from exc
                if not result.exists():
                    raise ToolError("Не удалось прочитать календарь и геопозицию Mac.")
                return json.loads(result.read_text())
