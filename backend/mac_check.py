"""Read-only device checks, kept separate from mocked tests and destructive actions."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx

from backend.config import Settings
from backend.context import DeviceContext, RequestContext, request_scope
from backend.mac import MacDesktop
from backend.media.safari import SafariMusic, player_state
from backend.memory.store import MemoryStore
from backend.permissions import PERMISSION_NAMES, diagnose, instruction
from backend.security import risk_for
from backend.tools import build_registry
from backend.tools.registry import ToolError
from backend.tools.system import system_status


async def inspect(s: Settings) -> dict:
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "read_only_current_launch_identity",
        "write_operations_tested": False,
        "permissions": await diagnose(("com.apple.systemevents", "com.spotify.client")),
        "components": {},
        "real_checks": {},
    }
    permissions = report["permissions"]
    report["permission_instructions"] = {
        key: instruction(key)
        for key, value in permissions.items()
        if key in PERMISSION_NAMES
        and (
            any(v != "granted" for v in value.values())
            if isinstance(value, dict)
            else value != "granted"
        )
    }
    store = MemoryStore(":memory:")
    registry = build_registry(s, store=store)
    report["tools"] = [
        {
            "name": t.name,
            "capability": t.required_capability,
            "risk": str(t.risk or risk_for(t.name, {})),
            "conditional_risk": t.name in {"manage_app", "music_playlist"},
        }
        for t in registry._tools.values()
    ]
    store.close()
    report["components"]["memory"] = "configured; real database not opened"
    report["components"]["speech"] = {
        "stt_model": s.stt.model,
        "tts_engine": s.tts.engine,
        "voice": s.voice_name,
        "voice_file_present": s.voice_path.is_file(),
        "wake_phrase": s.wakeword.phrase,
        "live_audio_tested": False,
    }
    if s.llm.host in {"http://localhost:11434", "http://127.0.0.1:11434"}:
        try:
            async with httpx.AsyncClient(base_url=s.llm.host, timeout=5) as client:
                version = await client.get("/api/version")
                version.raise_for_status()
                report["components"]["ollama"] = {
                    "reachable": True,
                    "version": version.json().get("version"),
                    "model": s.llm.model,
                }
        except (httpx.HTTPError, ValueError):
            report["components"]["ollama"] = {"reachable": False}
    else:
        report["components"]["ollama"] = "not checked: host is not default loopback"

    desktop = MacDesktop(s)
    with request_scope(RequestContext(DeviceContext.local_mac(), "Диагностика Mac")):
        for name, check in (
            ("apps", lambda: desktop.apps()),
            ("active_app", lambda: desktop.apps("active")),
            ("windows", lambda: desktop.list_windows()),
            ("battery", lambda: system_status("battery")),
            ("sound", lambda: system_status("volume")),
            ("brightness", lambda: desktop.brightness()),
        ):
            try:
                result = await check()
                if name == "windows":
                    result = {"windows_count": len(result["windows"]), "titles_omitted": True}
                report["real_checks"][name] = {"ok": True, "result": result}
            except (ToolError, OSError, ValueError) as exc:
                report["real_checks"][name] = {"ok": False, "error": str(exc)}
    try:
        music = SafariMusic(s)
        status = await music.request("status")
        report["components"]["youtube_music"] = {
            "installed": True,
            "running": status["running"],
            "accessibility": status["trusted"],
        }
        if status["running"] and status["trusted"]:
            # Unlike SafariMusic.snapshot(), raw request never launches or activates it.
            snapshot = await music.request("snapshot")
            state = player_state(snapshot.get("nodes", []))
            report["real_checks"]["music_status"] = {
                "ok": state["state"] != "unknown" and bool(state.get("track")),
                "read_succeeded": True,
                "state": state["state"],
                "track_present": bool(state.get("track")),
                "playback_actions_tested": False,
            }
    except (ToolError, OSError, ValueError) as exc:
        report["components"]["youtube_music"] = {"error": str(exc)}
    return report


async def run(s: Settings, *, json_output: bool = False) -> int:
    from rich.console import Console

    report = await inspect(s)
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        console = Console(highlight=False)
        console.print(
            "Диагностика Mac: только чтение. Реальные preflight/read проверки, без действий."
        )
        console.print("Инструменты: " + ", ".join(t["name"] for t in report["tools"]))
        console.print("Разрешения:", report["permissions"])
        for message in report["permission_instructions"].values():
            console.print(message)
        console.print("Компоненты:", report["components"])
        console.print("Реальные проверки чтения:", report["real_checks"])
        console.print("Закрытие, питание, окна, громкость, музыка и снимки не изменялись.")
    return int("error" in report["permissions"])
