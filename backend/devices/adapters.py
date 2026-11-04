"""Bounded public macOS mechanisms and the documented WLED JSON API.

Discovery is an observation, never authority to write to an unregistered receiver.
"""

import asyncio
import hashlib
import ipaddress
import json
import sys
from pathlib import Path

import httpx

from backend.intents import LEDParameters
from backend.latency import measure
from backend.media.safari import process
from backend.tools.registry import ToolError


def local_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("Введи локальный IP-адрес без URL, порта и пароля.") from exc
    networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
    if not any(address in ipaddress.ip_network(network) for network in networks):
        raise ValueError("Разрешён только адрес устройства в частной локальной сети.")
    return str(address)


class MacDevicesAdapter:
    def __init__(self, settings, runner=process):
        self.s, self.runner = settings, runner
        self.lock = asyncio.Lock()

    async def helper(self):
        from backend.paths import native_helper

        if sys.platform != "darwin":
            raise ToolError("Аудиовывод и Bonjour доступны в macOS.")
        if bundled := native_helper("devices"):
            return bundled
        async with self.lock:
            source = Path(__file__).with_name("native.swift")
            info = source.with_suffix(".plist")
            digest = hashlib.sha256(source.read_bytes() + info.read_bytes()).hexdigest()[:16]
            cache = self.s.data_path / "native"
            cache.mkdir(parents=True, exist_ok=True)
            target = cache / f"devices-{digest}"
            if not target.exists():
                temporary = target.with_suffix(".building")
                try:
                    await self.runner(
                        "xcrun",
                        "swiftc",
                        "-module-cache-path",
                        str(cache / "swift-cache"),
                        str(source),
                        "-Xlinker",
                        "-sectcreate",
                        "-Xlinker",
                        "__TEXT",
                        "-Xlinker",
                        "__info_plist",
                        "-Xlinker",
                        str(info),
                        "-o",
                        str(temporary),
                        timeout=90,
                    )
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
            return target

    async def run(self, operation, *args):
        if operation not in {"outputs", "select", "browse", "sound", "displays"}:
            raise ValueError("Неизвестная операция macOS.")
        result = json.loads(
            await self.runner(str(await self.helper()), operation, *args, timeout=12)
        )
        if result.get("error"):
            raise ToolError(result["error"])
        return result


class WLEDAdapter:
    async def read(self, device):
        ip = local_ip(device.endpoint)
        host = f"[{ip}]" if ":" in ip else ip
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            response = await client.get(f"http://{host}/json")
            response.raise_for_status()
            if len(response.content) > 262144:
                raise ToolError("Слишком большой ответ устройства.")
            data = response.json()
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("info"), dict)
            or not isinstance(data.get("state"), dict)
            or "ver" not in data["info"]
            or "leds" not in data["info"]
        ):
            raise ToolError("Адрес не подтвердил совместимый WLED JSON API.")
        return data

    async def inspect(self, device):
        data = await self.read(device)
        state = data["state"]
        values = {}
        if type(state.get("on")) is bool:
            values["power"] = state["on"]
        if type(state.get("bri")) is int and 0 <= state["bri"] <= 255:
            values["brightness"] = round(state["bri"] * 100 / 255)
        # Read the main segment only. Never silently overwrite a whole multi-segment setup.
        segments = state.get("seg", [])
        main = next((s for s in segments if s.get("id") == state.get("mainseg", 0)), None)
        if main and main.get("col"):
            rgb = main["col"][0][:3]
            if len(rgb) == 3 and all(type(v) is int and 0 <= v <= 255 for v in rgb):
                values["color"] = "#" + "".join(f"{v:02X}" for v in rgb)
        return {
            "state": values,
            "device_state_confirmed": True,
            "message": "WLED сообщил текущее состояние.",
        }

    async def execute(self, device, action, parameters):
        if action != "led":
            raise ValueError("WLED поддерживает питание, яркость и RGB.")
        params = LEDParameters.model_validate(parameters)
        if params.brightness_delta is not None:
            raise ValueError("Для WLED укажи абсолютную яркость.")
        data = await self.read(device)  # Re-establish compatibility before every write.
        body = {}
        if params.power is not None:
            body["on"] = params.power
        if params.brightness is not None:
            body["bri"] = round(params.brightness * 255 / 100)
        if params.color is not None:
            body["seg"] = [
                {
                    "id": data["state"].get("mainseg", 0),
                    "col": [[int(params.color[i : i + 2], 16) for i in (1, 3, 5)]],
                    "fx": 0,
                }
            ]
        ip = local_ip(device.endpoint)
        host = f"[{ip}]" if ":" in ip else ip
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            with measure("device_send"):
                try:
                    response = await client.post(f"http://{host}/json/state", json=body)
                    response.raise_for_status()
                except httpx.HTTPError:
                    return {
                        "state": {},
                        "result_status": "unverified",
                        "message": "Ответ на отправку не получен. Команда могла выполниться; "
                        "состояние неизвестно, автоматического повтора нет.",
                    }
        try:
            with measure("device_confirmation"):
                result = await self.inspect(device)
        except (httpx.HTTPError, ValueError, ToolError):
            return {
                "state": {},
                "written": True,
                "result_status": "unverified",
                "message": "WLED принял команду, но новое состояние прочитать не удалось. "
                "Автоматического повтора нет.",
            }
        result["written"] = True
        expected = params.model_dump(exclude_none=True)
        actual = result["state"]
        matches = all(
            abs(actual.get(k, -1000) - v) <= 1 if k == "brightness" else actual.get(k) == v
            for k, v in expected.items()
        )
        result["device_state_confirmed"] = matches
        result["result_status"] = "success" if matches else "unverified"
        result["message"] = (
            "WLED подтвердил состояние."
            if matches
            else "Команда отправлена; WLED не подтвердил запрошенные значения."
        )
        return result
