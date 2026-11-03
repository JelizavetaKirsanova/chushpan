"""Read-only application resources and writable user files, independent of cwd."""

import os
import sys
from pathlib import Path

RESOURCE_ROOT = Path(__file__).resolve().parent.parent
DESKTOP = bool(getattr(sys, "frozen", False) or os.environ.get("CHUSHPAN_DESKTOP"))
USER_ROOT = (
    Path(os.environ.get("CHUSHPAN_APP_DATA", Path.home() / "Library/Application Support/Chushpan"))
    if DESKTOP
    else RESOURCE_ROOT
)


def native_helper(name: str) -> Path | None:
    if getattr(sys, "frozen", False) and name.endswith(".app"):
        app = Path(sys.executable).parent.parent / "Helpers" / name
        return app if app.exists() else None
    path = RESOURCE_ROOT / "native" / name
    return path if DESKTOP and path.exists() else None
