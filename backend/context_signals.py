"""Read only currently associated Wi-Fi and connected Bluetooth devices, on opt-in."""

import hashlib
import json

from backend.personal import PersonalContext
from backend.tools.registry import ToolError
from backend.tools.system import run


def signal(kind, identifier, name):
    return {
        "kind": kind,
        "key": hashlib.sha256((kind + ":" + identifier).encode()).hexdigest(),
        "name": name[:120],
    }


def bluetooth_signals(value):
    result = []
    if isinstance(value, dict):
        for name, item in value.items():
            if isinstance(item, dict) and item.get("device_connected") in {
                "attrib_Yes",
                "Yes",
                True,
            }:
                address = item.get("device_address")
                if isinstance(address, str) and address:
                    result.append(signal("device", address.casefold(), name))
            result.extend(bluetooth_signals(item))
    elif isinstance(value, list):
        for item in value:
            result.extend(bluetooth_signals(item))
    return result


async def read_signals(settings):
    result, errors = [], []
    try:
        wifi = (await PersonalContext(settings).read(request_access=False, parts=("signals",))).get(
            "signals", {}
        )
        if wifi.get("ssid") and wifi.get("bssid"):
            result.append(
                signal("wifi", wifi["ssid"] + ":" + wifi["bssid"].casefold(), wifi["ssid"])
            )
        else:
            errors.append("Wi-Fi скрыт macOS или соединение отсутствует.")
    except (OSError, ValueError, TimeoutError, RuntimeError, ToolError):
        errors.append("Wi-Fi недоступен. Разрешения не запрашивались.")
    code, out = await run("system_profiler", "SPBluetoothDataType", "-json", "-detailLevel", "mini")
    if code == 0:
        try:
            result.extend(bluetooth_signals(json.loads(out)))
        except ValueError:
            errors.append("Список Bluetooth-устройств не прочитан.")
    else:
        errors.append("Список подключённых Bluetooth-устройств недоступен.")
    return {"signals": result, "error": " ".join(errors) or None}
