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
        from backend.paths import native_helper

        if bundled := native_helper("Чушпан — утро.app"):
            return bundled
        source = ROOT_DIR / "backend/personal.swift"
        cache = self.s.data_path / "native"
        app = cache / "Чушпан — утро.app"
        contents = app / "Contents"
        binary = contents / "MacOS" / "chushpan-personal"
        stamp = cache / "personal-version"
        digest = hashlib.sha256(source.read_bytes() + Path(__file__).read_bytes()).hexdigest()
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
                "Чушпан показывает события выбранного дня и утреннюю сводку. События не изменяются."
            ),
            "NSLocationUsageDescription": (
                "Чушпан определяет город для погоды и автоматического выбора версии сценария."
            ),
            "NSLocationWhenInUseUsageDescription": (
                "Чушпан определяет город для погоды и автоматического выбора версии сценария."
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

    async def read(
        self,
        *,
        request_access=True,
        parts=("calendar", "location"),
        day=None,
        city_only=False,
        permission=None,
    ):
        result = await self._read(
            request_access=request_access,
            parts=parts,
            day=day,
            city_only=city_only,
            permission=permission,
        )
        if not request_access or "permissions" in parts:
            return result
        blocked = [
            key
            for key in parts
            if key in {"calendar", "location"}
            and result.get(key, {}).get("error") == key + "_permission"
        ]
        if not blocked:
            return result
        status = await self.permissions()
        for key in blocked:
            if status.get(key) != "denied":
                continue
            from backend.desktop.platform import open_privacy

            opened = await asyncio.to_thread(open_privacy, key)
            if not opened["opened"]:
                continue
            # The protected read did not start; resume just this part after a
            # confirmed manual grant. Never restart the morning scenario.
            for _ in range(60):
                fresh = await self.permissions()
                if fresh.get(key) == "granted":
                    result.update(
                        await self._read(
                            request_access=False, parts=(key,), day=day, city_only=city_only
                        )
                    )
                    break
                await asyncio.sleep(1)
        return result

    async def _read(
        self,
        *,
        request_access: bool = True,
        parts: tuple[str, ...] = ("calendar", "location"),
        day: str | None = None,
        city_only: bool = False,
        permission: str | None = None,
    ) -> dict:
        from datetime import date

        if not parts or set(parts) - {"calendar", "location", "signals", "permissions"}:
            raise ValueError("Неизвестный источник личных данных.")
        if permission not in {None, "calendar", "location"}:
            raise ValueError("Неизвестное разрешение личных данных.")
        if day is not None:
            day = date.fromisoformat(day).isoformat()
        async with self._lock:
            app = await self.helper()
            # Only the requested two-day window is returned. Temporary data is removed
            # after each invocation; calendars and coordinates aren't saved as facts.
            with tempfile.TemporaryDirectory(prefix="chushpan-morning-") as tmp:
                request, result = Path(tmp) / "request.json", Path(tmp) / "result.json"
                request.write_text(
                    json.dumps(
                        {
                            "request_access": request_access,
                            "parts": parts,
                            "date": day,
                            "city_only": city_only,
                            "permission": permission,
                        }
                    )
                )
                try:
                    # LaunchServices establishes the helper's own stable application
                    # identity for Core Location; executing its Mach-O directly can
                    # attribute access to the parent instead. Calendar uses this same app.
                    command = ["open", "-W", "-n", "-g", str(app), "--args"]
                    await process(
                        *command,
                        str(request),
                        str(result),
                        timeout=65,
                    )
                except TimeoutError as exc:
                    raise ToolError("macOS не ответила на запрос календаря и геопозиции.") from exc
                if not result.exists():
                    raise ToolError("Не удалось прочитать календарь и геопозицию Mac.")
                return json.loads(result.read_text())

    async def permissions(self, request: str | None = None) -> dict:
        # No events, coordinates, SSIDs, or geocoder calls in this path.
        return (
            await self.read(
                parts=("permissions",), request_access=request is not None, permission=request
            )
        ).get("permissions", {})
