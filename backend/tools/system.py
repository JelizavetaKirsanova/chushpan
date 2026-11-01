"""The Mac itself: battery, memory, disk, uptime, volume and opening apps."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time

from backend.config import Settings
from backend.permissions import instruction
from backend.tools.registry import Tool, ToolError, user_text

TIMEOUT_S = 5

_BATTERY = re.compile(r"(\d+)%;\s*([^;]+);\s*(?:(\d+:\d+) remaining)?")
_MEM_FREE = re.compile(r"free percentage:\s*(\d+)%")
_BOOT = re.compile(r"sec = (\d+)")
_VOLUME = re.compile(r"output volume:(\d+|missing value).*output muted:(true|false)")


async def run(*cmd: str) -> tuple[int, str]:
    """Run a command without a shell; returns (exit code, stdout + stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except FileNotFoundError as exc:
        raise ToolError(f"{cmd[0]} not available") from exc
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
    except TimeoutError as exc:
        proc.kill()
        raise ToolError(f"{cmd[0]} timed out") from exc
    text = out.decode(errors="replace").strip()
    if proc.returncode and cmd[0] == "osascript":
        if "-1743" in text or "Not authorized" in text:
            raise ToolError(instruction("automation"))
        if "assistive access" in text or "-1719" in text:
            raise ToolError(instruction("accessibility"))
    return proc.returncode or 0, text


def parse_battery(text: str) -> dict | None:
    m = _BATTERY.search(text)
    if not m:
        return None  # desktop Mac
    percent, status, remaining = m.groups()
    status = status.strip()  # charging | discharging | charged | finishing charge | AC attached
    out: dict = {
        "percent": int(percent),
        "status": status,
        "charging": status in ("charging", "finishing charge"),
        "on_power_adapter": "AC Power" in text,
    }
    if remaining and remaining != "0:00":
        h, mins = remaining.split(":")
        # pmset says "remaining" for both; while charging it is the time to a full charge.
        # Under one name, qwen3 read 36 minutes to full as 36 minutes of battery left.
        key = "time_until_full" if out["charging"] else "battery_time_left"
        out[key] = f"{int(h)}h{mins}"
    return out


def parse_volume(text: str) -> dict | None:
    m = _VOLUME.search(text)
    if not m:
        return None
    level, muted = m.groups()
    return {
        "volume_percent": None if level == "missing value" else int(level),
        "muted": muted == "true",
    }


def _gb(n: int) -> float:
    return round(n / 1024**3, 1)


TOPICS = ("battery", "cpu", "memory", "disk", "uptime", "volume", "all")


async def system_status(topic: str = "all") -> dict:
    topic = topic if topic in TOPICS else "all"

    def wants(name: str) -> bool:
        return topic in (name, "all")

    out: dict = {}
    if wants("battery"):
        _, batt = await run("pmset", "-g", "batt")
        out["battery"] = parse_battery(batt) or "no battery (desktop Mac)"

    if wants("cpu"):
        load1, _, _ = os.getloadavg()
        cores = os.cpu_count() or 1
        out["cpu_load_percent"] = min(100, round(100 * load1 / cores))

    if wants("memory"):
        _, mem = await run("memory_pressure", "-Q")
        _, total = await run("sysctl", "-n", "hw.memsize")
        if (m := _MEM_FREE.search(mem)) and total.isdigit():
            out["memory"] = {"total_gb": _gb(int(total)), "free_percent": int(m.group(1))}

    if wants("disk"):
        disk = shutil.disk_usage("/")
        out["disk"] = {"free_gb": _gb(disk.free), "total_gb": _gb(disk.total)}

    if wants("uptime"):
        _, boot = await run("sysctl", "-n", "kern.boottime")
        if m := _BOOT.search(boot):
            mins = int((time.time() - int(m.group(1))) // 60)
            out["uptime"] = f"{mins // 1440}d {mins // 60 % 24}h {mins % 60}min"

    if wants("volume"):
        _, vol = await run("osascript", "-e", "get volume settings")
        if volume := parse_volume(vol):
            out["sound"] = volume
    return out


# Level to bring back on unmute when the sound was "muted" by setting the volume to 0.
_restore = {"level": None}


async def _volume_settings() -> dict:
    _, vol = await run("osascript", "-e", "get volume settings")
    return parse_volume(vol) or {}


async def set_volume(level: int | None = None, mute: bool | None = None) -> dict:
    said = (user_text.get() or "").casefold()
    if re.search(r"музык|песн|трек|youtube|spotify", said) and not re.search(
        r"\bmac\b|маке|мака|системн|компьютер", said
    ):
        direction = "down" if re.search(r"тише|убав|уменьш|сниз", said) else "up"
        raise ToolError(
            "Это громкость Mac. Для музыки вызови media_volume, "
            f"direction={direction!r}; level передавай только при явном проценте."
        )
    if level is None and mute is None:
        raise ToolError("give a level (0-100) or mute true/false")
    before = await _volume_settings()
    current = before.get("volume_percent")
    if level is not None:
        level = max(0, min(100, int(level)))
    if (mute or level == 0) and current and not before.get("muted"):
        _restore["level"] = current
    if mute is False and level is None and not current and _restore["level"]:
        level = _restore["level"]  # unmuting from 0 would stay silent: bring the old level
    script: list[str] = []
    if level is not None:
        script.append(f"set volume output volume {level}")
    if mute is not None:
        script.append(f"set volume output muted {'true' if mute else 'false'}")
    elif level:
        script.append("set volume output muted false")  # raising the volume implies unmute
    code, out = await run("osascript", *(arg for line in script for arg in ("-e", line)))
    if code:
        raise ToolError(out or "could not change the volume")
    after = await _volume_settings()
    if (
        not after
        or (level is not None and after.get("volume_percent") != level)
        or (mute is not None and after.get("muted") != mute)
    ):
        raise ToolError("macOS не подтвердила запрошенную громкость. Проверь текущий звук.")
    return {"ok": True, **after}


async def open_app(name: str) -> dict:
    name = (name or "").strip()
    if not name or name.startswith("-"):
        raise ToolError("give an application name")
    code, out = await run("open", "-a", name)
    if code:
        raise ToolError(f"application not found: {name}")
    return {"ok": True, "opened": name}


def system_tools(s: Settings) -> list[Tool]:
    async def set_requested_volume(level: int | None = None, mute: bool | None = None) -> dict:
        said = (user_text.get() or "").casefold()
        if (
            s.media.provider == "youtube_music"
            and "media" in s.tools.enabled
            and s.media.youtube_music.enabled
            and re.search(r"музык|песн|трек|youtube", said)
            and not re.search(r"\bmac\b|маке|мака|системн|компьютер", said)
        ):
            from backend.tools.media import MediaService

            direction = ""
            if re.search(r"тише|убав|уменьш|сниз", said):
                direction = "down"
            elif re.search(r"громче|прибав|увелич|повысь", said):
                direction = "up"
            number = re.search(r"\b(\d{1,3})\s*(?:%|процент)", said)
            if number:
                level, direction = int(number[1]), ""
            if re.search(r"выключи.{0,15}(?:звук|музык)|заглуши", said):
                level, direction = 0, ""
            return await MediaService(s).media_volume(direction=direction, level=level)
        return await set_volume(level, mute)

    async def open_requested(name: str) -> dict:
        if name.strip().casefold() in ("youtube music", "ютуб музыка", "ютуб мьюзик"):
            if not s.media.youtube_music.enabled:
                raise ToolError("YouTube Music отключён в настройках.")
            from backend.media.safari import SafariMusic

            app = SafariMusic(s)
            await app.launch()
            await app.snapshot()
            return {"ok": True, "opened": "YouTube Music"}
        return await open_app(name)

    cap = {
        "ru": "проверять состояние компьютера, менять громкость и открывать приложения",
        "pt": "ver bateria, memória, disco e volume do computador, ajustar o volume e abrir apps",
        "en": "check the computer's battery, memory, disk and volume, set the volume and open apps",
    }

    def status_reply(result: dict) -> str:
        pieces = []
        if battery := result.get("battery"):
            if isinstance(battery, dict):
                suffix = " Заряжается." if battery.get("charging") else ""
                pieces.append(f"Заряд {battery['percent']} процентов." + suffix)
            else:
                pieces.append("Батарея не обнаружена.")
        if "cpu_load_percent" in result:
            pieces.append(f"Оценка нагрузки процессора {result['cpu_load_percent']} процентов.")
        if memory := result.get("memory"):
            pieces.append(f"Свободно памяти {memory['free_percent']} процентов.")
        if disk := result.get("disk"):
            pieces.append(f"На диске свободно {disk['free_gb']} гигабайт.")
        if uptime := result.get("uptime"):
            match = re.fullmatch(r"(\d+)d (\d+)h (\d+)min", uptime)
            if match:
                days, hours, minutes = map(int, match.groups())
                pieces.append(
                    "Время работы: "
                    + s.locale.format_duration((days * 24 * 60 + hours * 60 + minutes) * 60)
                    + "."
                )
        if sound := result.get("sound"):
            pieces.append(volume_reply(sound))
        return " ".join(pieces) or "macOS не вернула запрошенные сведения."

    def volume_reply(result: dict) -> str:
        if "volume" in result:  # Existing music-routing handler.
            return f"Громкость музыки {result['volume']} процентов."
        if result.get("muted"):
            return "Звук выключен."
        if result.get("volume_percent") is not None:
            return f"Громкость Mac {result['volume_percent']} процентов."
        return "Текущий уровень звука не прочитан."

    return [
        Tool(
            name="get_system_status",
            description=(
                "Громкость всего Mac. Если названа музыка, песня или трек — используй "
                "media_volume. «Сделай музыку потише» → media_volume(direction=down). "
                "Status of this Mac: battery, CPU load, memory, free disk space, uptime and "
                "sound volume. Use for questions about the computer itself."
            ),
            handler=system_status,
            # Without a slot for "battery", qwen3 often wrote a call that Ollama 0.35
            # dropped (blank reply, no tool call); with it, 10 of 10 calls came through.
            parameters={
                "topic": {
                    "type": "string",
                    "enum": list(TOPICS),
                    "description": "What the user asked about; 'all' for a general check",
                }
            },
            required=["topic"],
            capability=cap,
            reply=status_reply if s.locale.lang == "ru" else None,
        ),
        Tool(
            name="set_volume",
            # qwen3 unmuted with level=50 ("volte o som" after "tire o som" at 20): say
            # explicitly that unmuting alone restores the previous level.
            description=(
                "Set the Mac's output volume and/or mute or unmute it. To silence the sound, "
                "pass mute=true (not level 0). To bring the sound back, pass only "
                "mute=false, with no level: the previous volume comes back by itself. Pass a "
                "level only when the user says a number or asks for louder/quieter. For the "
                "music alone ('abaixa a música'), use media_volume."
            ),
            handler=set_requested_volume,
            parameters={
                "level": {"type": "integer", "description": "Volume from 0 to 100"},
                "mute": {"type": "boolean", "description": "true to mute, false to unmute"},
            },
            capability=cap,
            reply=volume_reply if s.locale.lang == "ru" else None,
        ),
        Tool(
            name="open_app",
            description=(
                "Open (or bring to front) any installed Mac application by name, e.g. "
                "'Safari' or 'Spotify'. Opening an app works regardless of the selected "
                "music provider; it does not start music playback."
            ),
            handler=open_requested,
            parameters={"name": {"type": "string", "description": "Application name"}},
            required=["name"],
            capability=cap,
            reply=(lambda r: f"Запрос на открытие {r['opened']} передан macOS.")
            if s.locale.lang == "ru"
            else None,
        ),
    ]
