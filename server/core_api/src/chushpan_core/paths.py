"""Fixed service paths; never derive Core storage from a request or working directory."""

from __future__ import annotations

import os
from pathlib import Path


def core_root() -> Path:
    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Chushpan" / "CoreServer"


def database_path() -> Path:
    return core_root() / "data" / "core.sqlite3"


def memory_path() -> Path:
    return core_root() / "data" / "memory.sqlite3"
