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
    return bool(result and result[0])


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
