"""Public code identity only; never inspect TCC.db or system privacy settings."""

import asyncio
import plistlib
import sys
from functools import lru_cache
from pathlib import Path


def helper_info(name):
    return plistlib.dumps(
        {
            "CFBundleIdentifier": "local.chushpan." + name.replace("_", "-"),
            "CFBundleName": "Чушпан — " + name,
            "NSAppleEventsUsageDescription": (
                "Чушпан управляет выбранным приложением только по вашей команде."
            ),
        }
    )


@lru_cache(maxsize=24)
def _signature(path, modified):
    import subprocess

    try:
        result = subprocess.run(
            ["/usr/bin/codesign", "-d", "--verbose=4", path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        fields = {}
        for line in result.stderr.splitlines():
            key, _, value = line.partition("=")
            if key in {
                "Identifier",
                "TeamIdentifier",
                "CDHash",
                "Signature",
                "CodeDirectory",
                "Authority",
            }:
                fields[key] = value
        verified = (
            subprocess.run(
                ["/usr/bin/codesign", "--verify", "--strict", path], capture_output=True, timeout=5
            ).returncode
            == 0
        )
        return {
            "verified": verified,
            **fields,
            "note": "ad hoc подпись меняет идентичность TCC при обновлении; потребуется "
            "обновить разрешение установленного Чушпана."
            if fields.get("Signature") == "adhoc"
            else "Разрешение проверяется для текущей подписанной копии.",
        }
    except (OSError, subprocess.TimeoutExpired):
        return {"verified": False}


async def signing_identity(path):
    try:
        file = Path(path)
        return await asyncio.to_thread(_signature, str(file), file.stat().st_mtime_ns)
    except (OSError, TypeError):
        return {"verified": False}


def installed_app_identity():
    """Only our canonical frozen bundle can offer a reset of its own AX permission."""
    app = Path("/Applications/Чушпан.app")
    try:
        if (
            not getattr(sys, "frozen", False)
            or Path(sys.executable).resolve() != (app / "Contents/MacOS/Chushpan").resolve()
        ):
            return None
        if (
            plistlib.loads((app / "Contents/Info.plist").read_bytes()).get("CFBundleIdentifier")
            != "ee.chushpan.desktop"
        ):
            return None
        return app
    except (OSError, ValueError):
        return None


async def renew_accessibility_permission():
    """Explicit recovery action using Apple's tool; never edit or inspect TCC.db."""
    app = installed_app_identity()
    if app is None or not (await signing_identity(str(app))).get("verified"):
        raise ValueError("Обновление разрешения доступно только в установленном Чушпане.")
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/tccutil",
        "reset",
        "Accessibility",
        "ee.chushpan.desktop",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(process.communicate(), 10)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(
            "macOS не смогла обновить запись. Удалите только Чушпан из "
            "списка «Универсальный доступ» и добавьте текущую копию заново."
        )
