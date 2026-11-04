"""Public code identity only; never inspect TCC.db or system privacy settings."""

import asyncio
import plistlib
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
            if key in {"Identifier", "TeamIdentifier", "CDHash", "Signature", "CodeDirectory"}:
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
            "note": "ad hoc подпись не гарантирует сохранение TCC после обновления",
        }
    except (OSError, subprocess.TimeoutExpired):
        return {"verified": False}


async def signing_identity(path):
    try:
        file = Path(path)
        return await asyncio.to_thread(_signature, str(file), file.stat().st_mtime_ns)
    except (OSError, TypeError):
        return {"verified": False}
