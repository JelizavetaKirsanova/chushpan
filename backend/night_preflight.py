"""Fail-closed, read-only readiness checks. Never launch apps or request TCC grants."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from uuid import UUID

from backend import permissions
from backend.security import command_text
from backend.tools.registry import ToolError
from backend.tools.system import system_status

log = logging.getLogger(__name__)


def permissions_requested(text: str) -> bool:
    text = re.sub(r"^(?:эй[, .!]+)?чушпа[́]?н[, .!:]+", "", text.casefold().strip())
    return bool(re.fullmatch(r"проверь разрешения(?: ночного режима)?[.!? ]*", command_text(text)))


class NightPreflight:
    def __init__(self, settings, display, shortcuts):
        self.s, self.display, self.shortcuts = settings, display, shortcuts

    async def check(self) -> dict:
        checks = []

        def add(component, status, message):
            checks.append({"component": component, "status": status, "message": message})

        report = await permissions.diagnose(("com.apple.Safari",), settings=self.s)
        app = report.get("launch_application") or await permissions.launch_application()
        settings = "Системные настройки → Конфиденциальность и безопасность → "
        if "error" in report:
            add("macOS", "unknown", report["error"])
        if report.get("safari_executor", {}).get("status") == "unavailable":
            add(
                "Safari executor",
                "unavailable",
                "Не удалось проверить реальный Safari-помощник; права временного "
                "диагностического процесса не заменяют его права.",
            )
        ax = report.get("accessibility", "unknown")
        add(
            "Accessibility",
            ax,
            f"Универсальный доступ: {ax}. Приложение: {app}. " + settings + "Универсальный доступ.",
        )
        safari = report.get("automation", {}).get("com.apple.Safari", "unknown")
        add(
            "Safari Automation",
            safari,
            f"Автоматизация → Safari: {safari}. Приложение: {app}. "
            + settings
            + "Автоматизация. Если Safari не запущен, открой его вручную и повтори проверку.",
        )
        js = report.get("safari_javascript", "unknown")
        add(
            "Safari JavaScript",
            js,
            (
                f"Safari JavaScript from Apple Events: {js}. Safari → Разработка → "
                f"Разрешить JavaScript из событий Apple (либо Настройки → "
                f"Разработчик → Автоматизация). Для проверки нужна открытая обычная "
                f"вкладка; новые вкладки не открывались."
            ),
        )
        # A process cannot query another application's TCC grants through the public API.
        reviewed = self.s.night.shortcut_permissions_reviewed
        add(
            "Shortcuts permissions",
            "reviewed" if reviewed else "unknown",
            (
                "Права приложения Команды (Shortcuts) автоматически не проверены. "
                "Нужны Автоматизация → Safari и System Events; Команды → Настройки "
                "→ Дополнения → Разрешить выполнение скриптов, а также разрешения "
                "самой Youtube sleep. Если её действия управляют интерфейсом, "
                "нужен также Универсальный доступ для Команд (либо исполнителя, "
                "указанного в диалоге macOS) в Конфиденциальности и безопасности. "
            )
            + (
                (
                    "Ручная проверка отмечена в night.shortcut_permissions_reviewed; "
                    "это не результат TCC-проверки."
                )
                if reviewed
                else (
                    "После ручной проверки отметь night.shortcut_permissions_reviewed: "
                    "true в config.yaml. До этого запуск заблокирован."
                )
            ),
        )
        from backend.night import APP

        if not Path(APP).is_file():
            add(
                "BetterDisplay",
                "unavailable",
                "BetterDisplay не установлен по ожидаемому пути; яркость не менялась.",
            )
        elif ax != "granted":
            add(
                "BetterDisplay",
                "unknown",
                (
                    "Дисплеи не проверялись до разрешения Универсального доступа. "
                    "Отдельное право TCC на аппаратную яркость или громкость не "
                    "требуется этим кодом."
                ),
            )
        else:
            stage = "inventory"
            try:
                displays = await self.display.inventory()
                for builtin, uid, control in [
                    (True, None, "hardwareBrightness"),
                    (False, self.s.night.external_uuid, self.s.night.external_control),
                ]:
                    stage = "builtin_display" if builtin else "external_display"
                    selected = [
                        d
                        for d in displays
                        if d["builtin"] == builtin
                        and (builtin or (uid and UUID(d["uuid"]) == UUID(uid)))
                    ]
                    if len(selected) != 1:
                        raise ToolError(
                            "Встроенный или настроенный внешний экран не найден однозначно."
                        )
                    await self.display.read(selected[0]["uuid"], control)
                stage = "video_display_configuration"
                if self.s.youtube_video.night_autoplay and (
                    self.s.youtube_video.preferred_screen != "external"
                    or UUID(self.s.youtube_video.external_uuid or self.s.night.external_uuid)
                    != UUID(self.s.night.external_uuid)
                ):
                    raise ToolError("Внешний экран видео не совпадает с экраном ночного сценария.")
                add(
                    "BetterDisplay",
                    "granted",
                    (
                        "Дисплеи и текущая яркость прочитаны. Это не гарантирует "
                        "успешность последующей записи; она проверяется отдельно."
                    ),
                )
            except (ToolError, ValueError, KeyError, OSError, TimeoutError):
                add(
                    "BetterDisplay",
                    "unavailable",
                    (
                        "Не удалось прочитать нужные дисплеи/яркость. "
                        f"Проверка: {stage}. "
                        "Проверь BetterDisplay, подключение и UUID внешнего экрана. Не выдавай "
                        "лишние TCC-права наугад."
                    ),
                )
        try:
            sound = (await system_status("volume")).get("sound", {})
            if sound.get("volume_percent") is None:
                raise ToolError("volume unavailable")
            add(
                "System volume",
                "granted",
                (
                    "Громкость прочитана, не изменена. HDMI может не поддерживать "
                    "регулировку; запись будет проверена отдельно."
                ),
            )
        except (ToolError, OSError, TimeoutError):
            add(
                "System volume",
                "unavailable",
                (
                    "Текущая системная громкость не прочитана; проверь устройство "
                    "вывода. Это не доказательство отсутствия TCC-разрешения."
                ),
            )
        try:
            await self.shortcuts.verify_sleep()
            add(
                "Youtube sleep",
                "granted",
                (
                    "Имя и разрешённый UUID Youtube sleep найдены; команда не "
                    "запускалась. YouTube Sleep Timer отдельно не используется "
                    "реализацией: ожидание video.ended выполняет эта команда."
                ),
            )
        except (ToolError, OSError, TimeoutError):
            add(
                "Youtube sleep",
                "unavailable",
                "Не удалось проверить имя и разрешённый UUID Youtube sleep; "
                "проверь наличие команды и allowlist. Запуск не выполнялся.",
            )
        ok = all(c["status"] in {"granted", "reviewed"} for c in checks)
        blocking = [c for c in checks if c["status"] not in {"granted", "reviewed"}]
        chain = report.get("process_chain", {}).get("processes", [])
        identity = " → ".join(
            f"PID {p['pid']} (PPID {p['ppid']}), {p['executable']}" for p in chain
        )
        for check in checks:
            log.info(
                "NIGHT_PERMISSION_CHECK component=%s status=%s", check["component"], check["status"]
            )
        for check in blocking:
            log.warning(
                "NIGHT_PREFLIGHT_BLOCK component=%s status=%s", check["component"], check["status"]
            )
        return {
            "ok": ok,
            "checks": checks,
            "blocking_checks": blocking,
            "launch_application": app,
            "permission_probe": report.get("probe_identity", {}),
            "process_chain": report.get("process_chain", {}),
            "safari_executor": report.get("safari_executor", {}),
            "execution_roles": {
                "permission_probe": "permissions.swift: отдельный временный Swift-процесс",
                "safari_applescript": "NSAppleScript внутри youtube_safari.swift; "
                "PID и executable в safari_executor",
                "desktop_accessibility": "ytmusic_ax.swift: отдельный Swift-помощник",
                "system_volume": "osascript: Standard Additions, только чтение при диагностике",
                "shortcut": "/usr/bin/shortcuts run запускается только после всех проверок; "
                "сейчас не запускался",
                "shortcut_internal_permissions": "Собственные права Команд/исполнителя "
                "не наследуются из отчёта Python или Terminal",
            },
            "informational_permissions": {
                "microphone": {
                    "status": report.get("microphone", "unknown"),
                    "required": "для голосового ввода; не блокирует исполнение ночного сценария",
                },
                "screen_recording": {
                    "status": report.get("screen_recording", "unknown"),
                    "required": False,
                },
            },
            "message": (
                "Предварительная проверка пройдена; системные действия не выполнялись. "
                if ok
                else (
                    "Ночной сценарий заблокирован; яркость, громкость, видео и "
                    "Shortcut не менялись. "
                )
            )
            + (f"Цепочка запуска (не определяет владельца TCC): {identity}. " if identity else "")
            + (
                "Блокирующие проверки: "
                + ", ".join(f"{c['component']}={c['status']}" for c in blocking)
                + ". "
                if blocking
                else ""
            )
            + " ".join(c["message"] for c in checks),
        }
