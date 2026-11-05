"""Public macOS APIs; reading permission state never requests access."""

from __future__ import annotations

import ctypes
import threading
from pathlib import Path


def login_status() -> str:
    from ServiceManagement import SMAppService

    return {0: "off", 1: "enabled", 2: "approval_required", 3: "not_found"}.get(
        SMAppService.mainAppService().status(), "unknown"
    )


def set_login(enabled: bool) -> str:
    from ServiceManagement import SMAppService

    service = SMAppService.mainAppService()
    ok, error = (
        service.registerAndReturnError_(None)
        if enabled
        else service.unregisterAndReturnError_(None)
    )
    if not ok:
        raise RuntimeError(
            str(error.localizedDescription()) if error else "Ошибка автозапуска macOS"
        )
    return login_status()


def permissions() -> dict:
    from ApplicationServices import AXIsProcessTrusted
    from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
    from CoreLocation import CLLocationManager
    from Quartz import CGPreflightScreenCaptureAccess

    return {
        "microphone": {0: "not_determined", 1: "restricted", 2: "denied", 3: "granted"}.get(
            AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio), "unknown"
        ),
        "accessibility": "granted" if AXIsProcessTrusted() else "not_granted",
        "screen_recording": "granted" if CGPreflightScreenCaptureAccess() else "not_granted",
        "automation": "per_target_in_system_settings",
        "notifications": "check_system_settings",
        # Existing location/calendar integration has a separate, stable helper identity.
        "location": "helper_in_system_settings"
        if CLLocationManager.locationServicesEnabled()
        else "disabled",
        "bluetooth": "check_system_settings",
        "calendar": "helper_in_system_settings",
    }


def request_microphone() -> bool:
    from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

    status = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
    if status == 3:
        return True
    if status != 0:
        return False
    done = threading.Event()
    result = []

    def answer(granted):
        result.append(bool(granted))
        done.set()

    AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVMediaTypeAudio, answer)
    done.wait(60)
    return (
        bool(result and result[0])
        and AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio) == 3
    )


def request_notifications() -> bool:
    from Foundation import NSBundle
    from UserNotifications import (
        UNAuthorizationOptionAlert,
        UNAuthorizationOptionSound,
        UNUserNotificationCenter,
    )

    if not NSBundle.mainBundle().bundleIdentifier():
        return False
    done, result = threading.Event(), []

    def answer(granted, error):
        result.append(bool(granted) and error is None)
        done.set()

    UNUserNotificationCenter.currentNotificationCenter().requestAuthorizationWithOptions_completionHandler_(
        UNAuthorizationOptionAlert | UNAuthorizationOptionSound, answer
    )
    done.wait(60)
    return bool(result and result[0])


PRIVACY_PANES = {
    "microphone": ("Privacy_Microphone", "Микрофон"),
    "speech": ("Privacy_SpeechRecognition", "Распознавание речи"),
    "accessibility": ("Privacy_Accessibility", "Универсальный доступ"),
    "automation": ("Privacy_Automation", "Автоматизация"),
    "screen_recording": ("Privacy_ScreenCapture", "Запись экрана и системного аудио"),
    "location": ("Privacy_LocationServices", "Службы геолокации"),
    "bluetooth": ("Privacy_Bluetooth", "Bluetooth"),
    "calendar": ("Privacy_Calendars", "Календари"),
    "files": ("Privacy_FilesAndFolders", "Файлы и папки"),
    "input_monitoring": ("Privacy_ListenEvent", "Мониторинг ввода"),
    "full_disk": ("Privacy_AllFiles", "Полный доступ к диску"),
    "local_network": ("Privacy_LocalNetwork", "Локальная сеть"),
    "reminders": ("Privacy_Reminders", "Напоминания"),
    "contacts": ("Privacy_Contacts", "Контакты"),
    "photos": ("Privacy_Photos", "Фото"),
    "camera": ("Privacy_Camera", "Камера"),
}


def privacy_url(key: str) -> str:
    if key == "notifications":
        return "x-apple.systempreferences:com.apple.Notifications-Settings.extension"
    if key == "login":
        return "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"
    if key not in PRIVACY_PANES:
        raise ValueError("Неизвестное разрешение.")
    return "x-apple.systempreferences:com.apple.preference.security?" + PRIVACY_PANES[key][0]


def open_privacy(key: str) -> dict:
    """NSWorkspace, not navigation of WKWebView to an external URL scheme.

    Acceptance of a URL is not proof of the selected pane or a privacy grant.
    macOS may ignore a pane anchor; always include the exact manual route.
    """
    from AppKit import NSWorkspace
    from Foundation import NSURL

    url = privacy_url(key)
    route = (
        "Уведомления"
        if key == "notifications"
        else "Основные → Объекты входа"
        if key == "login"
        else "Конфиденциальность и безопасность → " + PRIVACY_PANES[key][1]
    )
    workspace = NSWorkspace.sharedWorkspace()
    opened = bool(workspace.openURL_(NSURL.URLWithString_(url)))
    fallback = not opened
    if not opened:
        opened = bool(
            workspace.openURL_(
                NSURL.URLWithString_("x-apple.systempreferences:com.apple.preference.security")
            )
        )
    if not opened:
        settings = workspace.URLForApplicationWithBundleIdentifier_("com.apple.systempreferences")
        opened = bool(settings and workspace.openURL_(settings))
    return {
        "opened": opened,
        "fallback": fallback,
        "url": url,
        "message": (
            "Если macOS открыла другой раздел: Системные настройки → "
            + route
            + ". Включи доступ для указанного исполнителя Чушпана и вернись в приложение."
        )
        if opened
        else "Не удалось открыть настройки macOS. Открой вручную: " + route,
    }


def permission_status() -> dict:
    import os
    import sys

    result = permissions()
    from Foundation import NSBundle

    result["identity"] = {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "executable": sys.executable,
        "bundle_id": NSBundle.mainBundle().bundleIdentifier() or "unbundled",
    }
    try:
        from CoreBluetooth import CBManager

        result["bluetooth"] = {0: "not_determined", 1: "restricted", 2: "denied", 3: "granted"}.get(
            CBManager.authorization(), "unknown"
        )
    except (ImportError, AttributeError):
        result["bluetooth"] = "unknown"
    # No delivery or requestAuthorization during a check.
    try:
        if not NSBundle.mainBundle().bundleIdentifier():
            raise RuntimeError("unbundled")
        from UserNotifications import UNUserNotificationCenter

        done, answer = threading.Event(), []

        def received(settings):
            answer.append(int(settings.authorizationStatus()))
            done.set()

        UNUserNotificationCenter.currentNotificationCenter().getNotificationSettingsWithCompletionHandler_(
            received
        )
        done.wait(3)
        result["notifications"] = {
            0: "not_determined",
            1: "denied",
            2: "granted",
            3: "granted",
            4: "granted",
        }.get(answer[0] if answer else -1, "unknown")
    except (ImportError, AttributeError, RuntimeError):
        result["notifications"] = "unknown"
    try:
        result["login"] = "granted" if login_status() == "enabled" else "check_needed"
    except (ImportError, RuntimeError):
        result["login"] = "unknown"
    return result


def is_login_launch() -> bool:
    # Loginwindow supplies this Apple event; ordinary Finder reopen is not a login.
    from Foundation import NSAppleEventManager

    event = NSAppleEventManager.sharedAppleEventManager().currentAppleEvent()
    if event is None:
        return False
    prop = event.paramDescriptorForKeyword_(int.from_bytes(b"prdt", "big"))
    return bool(prop and prop.enumCodeValue() == int.from_bytes(b"lgit", "big"))


def process_usage(pid: int) -> dict:
    """Resident memory and accumulated CPU time; no process enumeration or busy polling."""
    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {"pid": pid, "cpu_seconds": round(usage.ru_utime + usage.ru_stime, 2)}


def resident_mb() -> float:
    class Info(ctypes.Structure):
        _fields_ = [
            ("virtual_size", ctypes.c_uint64),
            ("resident_size", ctypes.c_uint64),
            ("resident_size_max", ctypes.c_uint64),
            ("user_time", ctypes.c_uint64),
            ("system_time", ctypes.c_uint64),
            ("policy", ctypes.c_int32),
            ("suspend_count", ctypes.c_int32),
        ]

    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    lib.mach_task_self.restype = ctypes.c_uint32
    info, count = Info(), ctypes.c_uint32(ctypes.sizeof(Info) // 4)
    if lib.task_info(lib.mach_task_self(), 20, ctypes.byref(info), ctypes.byref(count)) != 0:
        return 0.0
    return round(info.resident_size / 1024**2, 1)


def open_path(path: Path) -> None:
    from AppKit import NSWorkspace
    from Foundation import NSURL

    NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(str(path)))
