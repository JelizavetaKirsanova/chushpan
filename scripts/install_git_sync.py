#!/usr/bin/env python3
"""Install the reviewed sync command, Git hooks, Codex notifier and LaunchAgent."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from scripts.git_sync import ORIGIN, ROOT, Blocked, Sync, write_json

LABEL = "com.chushpan.git-sync"


def replace_notify(text: str, command: list[str]) -> str:
    """Replace only the top-level notify assignment; retain every other setting."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.match(r"^notify\s*=", line)), None)
    replacement = "notify = " + json.dumps(command, ensure_ascii=False) + "\n"
    if start is None:
        return replacement + text
    end = start + 1
    while True:
        try:
            tomllib.loads("".join(lines[start:end]))
            break
        except tomllib.TOMLDecodeError:
            if end >= len(lines):
                raise Blocked("Cannot safely replace the existing notify configuration") from None
            end += 1
    return "".join(lines[:start]) + replacement + "".join(lines[end:])


def install(scanner: Path, *, load_service: bool = True) -> dict:
    sync = Sync()
    if sync.git("remote", "get-url", "origin").decode().strip() != ORIGIN:
        raise Blocked("Review origin before installing synchronization")
    hooks = sync.git("config", "--get", "core.hooksPath", check=False).decode().strip()
    if hooks and Path(hooks) != ROOT / ".githooks":
        raise Blocked("An existing hooksPath must be integrated manually")
    existing = [
        p.name for p in (sync.git_dir / "hooks").glob("*") if not p.name.endswith(".sample")
    ]
    if existing and not hooks:
        raise Blocked("Existing Git hooks must be integrated manually: " + ", ".join(existing))
    if not scanner.is_file():
        raise Blocked("Supply a checksum-verified Gitleaks executable")
    binary = sync.state / "bin/gitleaks"
    binary.parent.mkdir(exist_ok=True, mode=0o700)
    if scanner.resolve() != binary.resolve():
        shutil.copyfile(scanner, binary)
    binary.chmod(0o700)
    codex_config = Path.home() / ".codex/config.toml"
    text = codex_config.read_text() if codex_config.exists() else ""
    settings = tomllib.loads(text)
    support = Path.home() / "Library/Application Support/Chushpan/AutoSync"
    support.mkdir(parents=True, exist_ok=True, mode=0o700)
    support.chmod(0o700)
    notifier = support / "codex_notify.py"
    command = ["/usr/bin/python3", str(notifier)]
    original = settings.get("notify", [])
    legacy_command = [sys.executable, str(ROOT / "scripts/git_sync.py"), "notify"]
    if original in (command, legacy_command):
        original = sync.cfg.get("original_notify", [])
    cfg = {
        **sync.cfg,
        "origin": ORIGIN,
        "branch": "main",
        "gitleaks": str(binary),
        "backups": str(Path.home() / "Library/Application Support/Chushpan/Backups"),
        "original_notify": original,
    }
    # Include configured external data/persona/auth paths as private backup inputs.
    from backend.config import get_settings

    app = get_settings()
    extras = {"runtime-data": str(app.data_path)} if app.data_path != ROOT / "data" else {}
    for label, value in (
        ("personality.md", app.persona.file),
        ("music-auth.json", app.media.youtube_music.auth_file),
    ):
        if value:
            path = Path(value).expanduser()
            path = path if path.is_absolute() else ROOT / path
            if not path.is_relative_to(ROOT):
                extras[label] = str(path)
    cfg["extra_backups"] = extras
    write_json(sync.state / "settings.json", cfg)
    sync.cfg = cfg
    sync.audit_history()
    backup = sync.backup()
    shutil.copyfile(ROOT / "scripts/codex_notify.py", notifier)
    notifier.chmod(0o700)
    write_json(
        support / "notify-settings.json",
        {
            "root": str(ROOT),
            "python": sys.executable,
            "original_notify": original,
        },
    )
    if not (sync.state / "codex-config.before.toml").exists():
        (sync.state / "codex-config.before.toml").write_text(text)
        (sync.state / "codex-config.before.toml").chmod(0o600)
    changed = replace_notify(text, command)
    assert {k: v for k, v in tomllib.loads(changed).items() if k != "notify"} == {
        k: v for k, v in settings.items() if k != "notify"
    }
    codex_config.write_text(changed)
    codex_config.chmod(0o600)
    for hook in (ROOT / ".githooks").iterdir():
        hook.chmod(0o755)
    sync.git("config", "core.hooksPath", str(ROOT / ".githooks"))
    sync.git("config", "push.default", "simple")
    sync.git("config", "push.followTags", "false")
    agent_path = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    launch = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(ROOT / "scripts/git_sync.py"), "periodic"],
        "WorkingDirectory": str(ROOT),
        "StartInterval": 300,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        "StandardOutPath": str(sync.state / "launchd.log"),
        "StandardErrorPath": str(sync.state / "launchd.error.log"),
    }
    if agent_path.exists():
        previous = plistlib.loads(agent_path.read_bytes())
        if previous.get("ProgramArguments", [])[:2] != launch["ProgramArguments"][:2]:
            raise Blocked("An unrelated LaunchAgent already uses this label")
    agent_path.write_bytes(plistlib.dumps(launch))
    agent_path.chmod(0o600)
    if load_service:
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(agent_path)], check=True)
    return {"backups": backup, "launch_agent": str(agent_path), "interval_seconds": 300}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gitleaks", required=True, type=Path)
    parser.add_argument("--no-load", action="store_true")
    args = parser.parse_args()
    print(json.dumps(install(args.gitleaks, load_service=not args.no_load), ensure_ascii=False))
