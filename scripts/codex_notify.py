#!/usr/bin/python3
"""Standalone Codex notifier; installed outside the checkout and never logs prompts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def dispatch(payload: dict, settings: dict) -> int:
    original = settings.get("original_notify", [])
    if original:
        try:
            subprocess.run([*original, json.dumps(payload)], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass  # An existing desktop notifier must not block saving completed work.
    root = Path(settings["root"])
    cwd = Path(payload.get("cwd", "/")).resolve()
    python = Path(settings["python"])
    script = root / "scripts/git_sync.py"
    if (
        payload.get("type") != "agent-turn-complete"
        or not (cwd == root or root in cwd.parents)
        or not python.is_file()
        or not script.is_file()
    ):
        return 0
    with open(os.devnull, "wb") as out:
        subprocess.Popen(
            [str(python), str(script), "complete"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,
        )
    return 0


if __name__ == "__main__":
    config = json.loads(Path(__file__).with_name("notify-settings.json").read_text())
    raise SystemExit(dispatch(json.loads(sys.argv[1]), config))
