"""Environment checks: `uv run python -m backend doctor`."""

from __future__ import annotations

import asyncio
import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from rich.console import Console
from rich.table import Table

from backend.brain.prompts import build_system_prompt
from backend.config import Settings, get_settings
from backend.tools.geocode import resolve_location

console = Console()


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def check_platform() -> Check:
    is_mac_arm = sys.platform == "darwin" and platform.machine() == "arm64"
    detail = f"{platform.system()} {platform.release()} ({platform.machine()})"
    return Check("macOS Apple Silicon", is_mac_arm, detail, required=False)


def check_python() -> Check:
    ok = (3, 12) <= sys.version_info[:2] < (3, 14)
    return Check("Python 3.12–3.13", ok, platform.python_version())


def check_audio(s: Settings) -> list[Check]:
    try:
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001
        return [Check("Аудиоустройства", False, f"sounddevice недоступен: {exc}", required=False)]
    checks = []
    for kind, label, device in (
        ("input", "Микрофон", s.audio.input_device),
        ("output", "Динамики", s.audio.output_device),
    ):
        try:
            info = sd.query_devices(device, kind=kind)
            checks.append(Check(label, True, info["name"], required=False))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check(label, False, str(exc), required=False))
    return checks


def check_speech_models(s: Settings) -> list[Check]:
    checks = []
    try:
        import pysilero_vad  # noqa: F401

        checks.append(Check("Обнаружение речи", True, "Silero VAD", required=False))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("Обнаружение речи", False, str(exc), required=False))
    try:
        import mlx_whisper  # noqa: F401
        from huggingface_hub import try_to_load_from_cache

        cached = isinstance(try_to_load_from_cache(s.stt.model, "config.json"), str)
        detail = s.stt.model if cached else "не загружено: запустите ./scripts/setup.sh"
        checks.append(Check("Распознавание речи", cached, detail, required=False))
    except Exception as exc:  # noqa: BLE001
        checks.append(
            Check("Распознавание речи", False, f"mlx-whisper недоступен: {exc}", required=False)
        )
    checks.append(check_wakeword(s))
    return checks


def check_wakeword(s: Settings) -> Check:
    if not s.wakeword.enabled:
        return Check("Фраза активации", True, "выключена: слушаю постоянно", required=False)
    if s.wakeword.phrase:
        return Check("Фраза активации", True, f"«{s.wakeword.phrase}» · Whisper", required=False)
    files = [s.wakeword_path, s.wakeword_dir / "melspectrogram.onnx"]
    files.append(s.wakeword_dir / "embedding_model.onnx")
    missing = [f.name for f in files if not f.exists()]
    if missing:
        return Check(
            "Фраза активации",
            False,
            f"нет файлов {', '.join(missing)}: запустите ./scripts/setup.sh",
            required=False,
        )
    try:
        import openwakeword  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return Check("Фраза активации", False, f"openwakeword недоступен: {exc}", required=False)
    detail = f"{s.wakeword_path.name} · порог {s.wakeword.threshold}"
    return Check("Фраза активации", True, detail, required=False)


def check_ollama(s: Settings) -> list[Check]:
    checks: list[Check] = []
    try:
        version = httpx.get(f"{s.llm.host}/api/version", timeout=3).json()["version"]
        checks.append(Check("Сервер Ollama", True, f"v{version} · {s.llm.host}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("Сервер Ollama", False, f"недоступен: {exc}"))
        return checks

    tags = httpx.get(f"{s.llm.host}/api/tags", timeout=5).json().get("models", [])
    names = {m["name"] for m in tags} | {m.get("model", "") for m in tags}
    has_model = s.llm.model in names or f"{s.llm.model}:latest" in names
    checks.append(
        Check(
            f"Модель {s.llm.model}",
            has_model,
            "установлена" if has_model else f"запустите: ollama pull {s.llm.model}",
        )
    )
    if not has_model:
        return checks

    try:
        prompt = build_system_prompt(s)
    except (OSError, ValueError) as exc:
        checks.append(Check("Личность", False, str(exc)))
        return checks
    payload = {
        "model": s.llm.model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Вычисли 2 + 2. Напиши только результат."},
        ],
        "stream": False,
        "think": False,  # a bounded health probe; normal conversations keep their thinking mode
        "keep_alive": s.llm.keep_alive,
        "options": {
            "temperature": s.llm.temperature,
            "num_ctx": s.llm.num_ctx,
            "num_predict": 32,
        },
    }
    start = time.perf_counter()
    try:
        resp = httpx.post(f"{s.llm.host}/api/chat", json=payload, timeout=180)
        resp.raise_for_status()
        text = resp.json()["message"]["content"].strip()
        elapsed = time.perf_counter() - start
        checks.append(Check("Ответ модели", bool(text), f'"{text[:60]}" ({elapsed:.1f}s)'))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("Ответ модели", False, str(exc)))
    return checks


def check_llm(s: Settings) -> list[Check]:
    if s.llm.provider == "ollama":
        return check_ollama(s)
    from backend.brain.providers import SETUP, api_key

    ready = bool(api_key() and s.gemini.free_tier_confirmed)
    checks = [
        Check(
            "Gemini (локальная проверка настройки)",
            ready,
            f"{s.gemini.model}; доступность API и квота не проверены" if ready else SETUP,
            required=not s.llm.fallback,
        )
    ]
    if s.llm.fallback:
        local = check_ollama(s)
        for check in local:
            check.required = not ready
        checks.extend(local)
    return checks


def check_language(s: Settings) -> Check:
    loc = s.locale
    detail = f"{loc.code} · {loc.language_name}"
    if not loc.native_prompt:
        detail += " (инструкция на английском, ответы на выбранном языке)"
    return Check("Язык", True, detail, required=False)


def check_voice(s: Settings) -> Check:
    path = s.voice_path
    if s.tts.engine == "say":
        return Check("Голос macOS", True, s.tts.say_voice or "системный", required=False)
    silero = s.tts.engine == "silero"
    ok = path.exists() and (silero or Path(f"{path}.json").exists())
    detail = str(path.relative_to(path.parents[2])) if ok else "запустите: ./scripts/setup.sh"
    engine = "Silero" if silero else "Piper"
    return Check(f"Голос {engine} {s.voice_name}", ok, detail, required=False)


def check_owner(s: Settings) -> Check:
    u = s.user
    if not u.name and u.kind == "n":
        return Check("Пользователь", False, "задайте JARVIS_OWNER_NAME в .env", required=False)
    detail = f"{u.name or '-'} · обращение: {s.owner_full_address or 'нейтральное'}"
    return Check("Пользователь", True, detail, required=False)


def check_location(s: Settings) -> Check:
    loc = asyncio.run(resolve_location(s))
    if not loc.query and not loc.resolved:
        return Check("Местоположение", False, "задайте JARVIS_CITY в .env", required=False)
    if not loc.resolved:
        return Check("Местоположение", False, f'не удалось найти "{loc.query}"', required=False)
    detail = f"{loc.display_name} ({loc.latitude:.4f}, {loc.longitude:.4f}) · {loc.tz_name}"
    return Check("Местоположение", True, detail, required=False)


def check_hud(s: Settings) -> Check:
    from backend.hud.server import DIST_DIR

    if not s.hud.enabled:
        return Check("HUD", True, "выключено", required=False)
    if not (DIST_DIR / "index.html").exists():
        return Check("HUD", False, "не собран: запустите ./scripts/setup.sh", required=False)
    return Check("HUD", True, f"http://{s.server.host}:{s.server.port}", required=False)


def check_autostart() -> Check:
    from backend import autostart

    st = autostart.status()
    if not st.installed:
        return Check("Автозапуск", True, "выключен · `autostart install`", required=False)
    from backend.app_bundle import executable

    if not executable(get_settings()).exists():
        return Check("Автозапуск", False, "нет приложения: `autostart install`", required=False)
    if st.pid:
        return Check("Автозапуск", True, f"включён · работает (pid {st.pid})", required=False)
    detail = "включён · не работает" + (f" (код завершения {st.last_exit})" if st.last_exit else "")
    return Check("Автозапуск", True, detail, required=False)


def check_spotify(s: Settings) -> Check:
    if s.media.provider == "youtube_music":
        from backend.media.safari import SafariMusic
        from backend.tools.registry import ToolError

        if "media" not in s.tools.enabled or not s.media.youtube_music.enabled:
            return Check("YouTube Music", True, "выключено", required=False)
        try:
            SafariMusic(s).discover()
        except ToolError:
            return Check("YouTube Music", False, "Safari Web App не найден", required=False)
        return Check(
            "YouTube Music",
            True,
            "web app найден; нужен Универсальный доступ, живое управление не проверено",
            required=False,
        )
    if "media" not in s.tools.enabled:
        return Check("Spotify", True, "выключено", required=False)
    found = any(
        (base / "Spotify.app").exists()
        for base in (Path("/Applications"), Path.home() / "Applications")
    )
    if not found:
        return Check("Spotify", False, "не установлен: музыка недоступна", required=False)
    detail = (
        "установлен · для музыки нужны разрешения на автоматизацию, "
        "универсальный доступ и запись экрана"
    )
    return Check("Spotify", True, detail, required=False)


def check_permissions(s: Settings) -> list[Check]:
    from backend.permissions import PERMISSION_NAMES, diagnose, instruction

    targets = ("com.spotify.client",) if s.media.provider == "spotify" else ()
    report = asyncio.run(diagnose(targets))
    if "error" in report:
        return [Check("Разрешения macOS", False, report["error"], required=False)]
    checks = []
    for permission, label in PERMISSION_NAMES.items():
        states = report[permission]
        if isinstance(states, dict):
            for target, state in states.items():
                checks.append(
                    Check(
                        label,
                        state == "granted",
                        f"{target}: {state}. "
                        + (instruction(permission) if state != "granted" else ""),
                        required=False,
                    )
                )
        else:
            checks.append(
                Check(
                    label,
                    states == "granted",
                    states if states == "granted" else instruction(permission),
                    required=False,
                )
            )
    return checks


def run() -> int:
    s = get_settings()
    checks = [
        check_platform(),
        check_python(),
        check_language(s),
        check_owner(s),
        check_location(s),
        *check_llm(s),
        check_voice(s),
        *check_speech_models(s),
        *check_audio(s),
        check_hud(s),
        check_spotify(s),
        *check_permissions(s),
        check_autostart(),
    ]

    table = Table(title=f"{s.assistant_name} · диагностика", show_lines=False)
    table.add_column("", width=2)
    table.add_column("Проверка")
    table.add_column("Подробности", overflow="fold")
    for c in checks:
        icon = "[green]✔[/]" if c.ok else ("[red]✘[/]" if c.required else "[yellow]![/]")
        table.add_row(icon, c.name, c.detail)
    console.print(table)

    failed = [c for c in checks if c.required and not c.ok]
    if failed:
        console.print(f"[red]{len(failed)} обязательных проверок не пройдено.[/]")
        return 1
    if any(not c.ok for c in checks):
        console.print(
            "[yellow]Базовые проверки пройдены. Отдельные функции требуют настройки — "
            "см. строки с ! выше.[/]"
        )
    else:
        console.print("[green]Все выполненные проверки пройдены.[/]")
    return 0
