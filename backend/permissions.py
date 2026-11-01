"""On-demand TCC diagnostics for the current launch identity, without permission prompts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path

from backend.config import ROOT_DIR

log = logging.getLogger(__name__)

PERMISSION_NAMES = {
    "accessibility": "Универсальный доступ (Accessibility)",
    "automation": "Автоматизация (Automation)",
    "screen_recording": "Запись экрана (Screen Recording)",
    "microphone": "Микрофон (Microphone)",
}


def instruction(permission: str) -> str:
    return (
        "Открой Системные настройки → Конфиденциальность и безопасность → "
        f"{PERMISSION_NAMES[permission]}. Разреши доступ именно приложению запуска "
        "Чушпана (Terminal, Codex или Чушпан.app), затем перезапусти его. "
        "Разрешение другого приложения сюда не переносится."
    )


async def _run(*args: str, timeout: float = 30) -> str:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    if args[0] != "/bin/ps":
        log.info(
            "PERMISSION_PROCESS pid=%d ppid=%d executable=%s", process.pid, os.getpid(), args[0]
        )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError("Не удалось выполнить диагностику разрешений macOS.")
    return stdout.decode()


async def launch_application() -> str:
    """Best-effort ancestry, not an authoritative lookup of another app's TCC identity."""
    pid = os.getpid()
    candidates = []
    try:
        for _ in range(16):
            row = (await _run("/bin/ps", "-p", str(pid), "-o", "ppid=,comm=")).strip()
            parent, executable = row.split(maxsplit=1)
            if match := re.search(r"([^/]+\.app)/Contents/", executable):
                candidates.append(match[1])
                if match[1] != "Python.app":
                    return match[1] + " (по цепочке запуска; сверяй с диалогом macOS)"
            pid = int(parent)
            if pid <= 1:
                break
    except (OSError, ValueError, RuntimeError, TimeoutError):
        pass
    if candidates:
        return (
            candidates[0]
            + " (только исполняемый процесс; ответственное приложение TCC не определено)"
        )
    return "не определено; приложение, из которого запущен Чушпан: Terminal, Codex или Чушпан.app"


async def process_chain(pid: int | None = None) -> dict:
    current = os.getpid() if pid is None else pid
    root = current
    if current <= 0:
        return {"processes": [], "status": "invalid_pid"}
    rows, seen = [], set()
    try:
        while current > 0 and current not in seen and len(rows) < 20:
            seen.add(current)
            value = (await _run("/bin/ps", "-p", str(current), "-o", "pid=,ppid=,comm=")).strip()
            process, parent, executable = value.split(maxsplit=2)
            rows.append({"pid": int(process), "ppid": int(parent), "executable": executable})
            current = int(parent)
        children = []
        for line in (await _run("/bin/ps", "-axo", "pid=,ppid=,comm=")).splitlines():
            child, parent, executable = line.strip().split(maxsplit=2)
            if int(parent) == root:
                children.append({"pid": int(child), "ppid": int(parent), "executable": executable})
        return {
            "processes": rows,
            "children": children,
            "status": "observed",
            "tcc_responsible_app": "not_inferred",
        }
    except (OSError, ValueError, RuntimeError, TimeoutError):
        return {"processes": rows, "status": "unavailable_or_process_exited"}


async def diagnose(automation_targets: tuple[str, ...] = (), *, settings=None) -> dict:
    if sys.platform != "darwin":
        return {"error": "Диагностика разрешений доступна только на macOS."}
    if any(
        t not in {"com.spotify.client", "com.apple.systemevents", "com.apple.Safari"}
        for t in automation_targets
    ):
        return {"error": "Неизвестная цель Automation."}
    try:
        with tempfile.TemporaryDirectory(prefix="chushpan-permissions-") as directory:
            binary = Path(directory) / "permissions"
            await _run(
                "xcrun",
                "swiftc",
                "-module-cache-path",
                str(Path(directory) / "modules"),
                str(ROOT_DIR / "backend/permissions.swift"),
                "-o",
                str(binary),
                timeout=90,
            )
            result = json.loads(await _run(str(binary), *automation_targets, timeout=10))
            result["launch_application"] = await launch_application()
            result["process_chain"] = await process_chain()
            result["probe_identity"] = {
                "helper_process": result.pop("helper_process", {}),
                "helper_executable": str(binary),
                "parent_executable": sys.executable,
                "scope": "current_process_chain_not_other_app_tcc",
                "note": "Статусы относятся к запущенному помощнику в этой цепочке. "
                "Права другой копии Чушпан.app или Команд не проверены.",
            }
            if settings is not None and "com.apple.Safari" in automation_targets:
                from backend.media.youtube_playback import SafariPlayback
                from backend.tools.registry import ToolError

                try:
                    executor = await SafariPlayback(settings).request("youtube_permissions")
                    result["safari_executor"] = executor
                    result["accessibility"] = executor.get("accessibility", "unknown")
                    result["automation"]["com.apple.Safari"] = executor.get("automation", "unknown")
                    result["safari_javascript"] = executor.get("safari_javascript", "unknown")
                except (ToolError, OSError, TimeoutError):
                    result["safari_executor"] = {"status": "unavailable"}
            return result
    except (OSError, RuntimeError, ValueError, TimeoutError):
        return {"error": "Не удалось проверить разрешения. Нужны macOS и Command Line Tools."}


async def run(automation_targets: tuple[str, ...] = ()) -> int:
    from rich.console import Console

    console = Console(highlight=False)
    result = await diagnose(automation_targets)
    if "error" in result:
        console.print(result["error"])
        return 1
    console.print("Проверка текущего приложения запуска; разрешения не запрашиваются.")
    console.print(result.get("launch_application", "Приложение запуска не определено."))
    console.print(result.get("probe_identity", {}))
    console.print(result.get("process_chain", {}))
    for permission, name in PERMISSION_NAMES.items():
        states = result[permission]
        if isinstance(states, dict):
            if not states:
                console.print(f"{name}: не проверялась — укажи цель через --automation.")
            for target, state in states.items():
                console.print(f"{name}, {target}: {state}")
                if state != "granted":
                    console.print(instruction(permission))
        else:
            console.print(f"{name}: {states}")
            if states != "granted":
                console.print(instruction(permission))
    if "safari_javascript" in result and "com.apple.Safari" in automation_targets:
        console.print("Safari JavaScript from Apple Events: " + result["safari_javascript"])
    return 0  # Diagnostic completed, not a claim that every permission is granted.
