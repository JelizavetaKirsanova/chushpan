"""A device catalogue and HUD endpoint over the existing integration handlers."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from collections import OrderedDict
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx

from backend.ble_diagnostics import device_key, runtime_info, trace
from backend.ble_profiles import DDM_PROFILE, BLEProfile, legacy_profile
from backend.context import (
    RequestContext,
    device_selection,
    request_context,
    request_scope,
    scenario_operation,
    voice_addressed,
)
from backend.devices.center import DeviceCenter
from backend.devices.colors import ColorLibrary
from backend.devices.models import Device
from backend.devices.store import DeviceStore
from backend.intents import LEDAction, LEDParameters
from backend.latency import measure
from backend.led import LEDStrip
from backend.security import ActionDenied
from backend.tools.registry import ToolError

KINDS = {
    "led_strip": ("LED-лента", "ble"),
    "mac": ("MacBook", "local"),
    "airplay": ("Apple AirPlay", "bonjour"),
    "audio_output": ("Вывод звука macOS", "coreaudio"),
    "wled": ("Wi-Fi / HTTP · WLED", "http"),
    "external_display": ("Внешний дисплей", "betterdisplay"),
}
TOOLS = {
    "led_control": "led_strip",
    "get_brightness": "mac",
    "set_brightness": "mac",
    "set_volume": "mac",
    "get_system_status": "mac",
    "mac_power": "mac",
    "media_control": "mac",
    "music_command": "mac",
    "play_music": "mac",
    "now_playing": "mac",
    "media_volume": "mac",
    "media_seek": "mac",
    "set_external_brightness": "external_display",
}


def stamp():
    return datetime.now(UTC).isoformat()


class Devices(DeviceCenter):
    def __init__(self, tools, settings, memory):
        self.tools, self.s, self.memory = tools, settings, memory
        self.store = DeviceStore(memory)
        self.colors = ColorLibrary(memory)
        from backend.automation.locations import Locations

        self.locations = (
            tools.automations.locations if tools.automations else Locations(memory, settings)
        )
        self.bus = self.conv = None
        self.jobs = set()
        self.operations = {}
        self.connections = {}
        self.inflight = set()
        self.completed = OrderedDict()
        self.scans = {}
        self.discovery_lock = asyncio.Lock()
        self.scan_reports = OrderedDict()
        self.probes = {}
        self.strips = {}
        self.preferred = None
        self.primary = getattr(tools._tools.get("led_control"), "_handler", None)
        self.primary = getattr(self.primary, "__self__", None)
        self.seed()
        self.ensure_led_tool()
        self.init_center()
        if self.primary:
            primary = next(
                (
                    d
                    for d in self.store.list()
                    if d.kind == "led_strip" and d.endpoint == str(self.s.led.address)
                ),
                None,
            )
            if primary:
                self.strip(primary)

    def seed(self):
        if not self.memory.memory_available:
            return
        seeded = set(self.memory.preference("devices", "seeded") or [])
        candidates = []
        if any(n in self.tools for n, kind in TOOLS.items() if kind == "mac"):
            candidates.append(
                Device(
                    id="mac-local",
                    name="MacBook",
                    kind="mac",
                    protocol="local",
                    endpoint="local-mac",
                )
            )
        if self.s.led.enabled and "led_control" in self.tools:
            candidates.append(
                Device(
                    id="led-configured",
                    name="LED-лента",
                    kind="led_strip",
                    protocol="ble",
                    endpoint=str(self.s.led.address),
                )
            )
        if self.s.night.external_uuid and "set_external_brightness" in self.tools:
            candidates.append(
                Device(
                    id="display-configured",
                    name="Внешний дисплей",
                    kind="external_display",
                    protocol="betterdisplay",
                    endpoint=str(self.s.night.external_uuid),
                )
            )
        for device in candidates:
            if device.id not in seeded:
                if not any(
                    (d.kind, d.endpoint) == (device.kind, device.endpoint)
                    for d in self.store.list()
                ):
                    self.store.save(device)
                seeded.add(device.id)
        self.memory.set_preference("devices", "seeded", sorted(seeded))

    def ensure_led_tool(self):
        if "system" not in self.s.tools.enabled:
            return
        devices = [d for d in self.store.list() if d.kind == "led_strip"]
        if not devices:
            return
        tool = self.tools._tools.get("led_control")
        if tool is None:
            tool = self.strip(devices[0]).tool()
            tool.agent = self.tools.mac_agent
            tool.settings_store = self.memory
            self.tools.add(tool)
        tool.handler = self.led_command
        from backend.mac_agent import LocalMacAgent

        if isinstance(tool.agent, LocalMacAgent):
            tool.agent.register(tool.name, tool._handler)
        from backend.automation.capabilities import CapabilityRegistry

        if self.tools.automations:
            self.tools.automations.capabilities.items = CapabilityRegistry(self.tools, self.s).items

    def strip(self, device):
        if device.id not in self.strips:
            if self.primary and device.endpoint == str(self.s.led.address):
                strip = self.primary
            else:
                settings = self.s.model_copy(deep=True)
                settings.led.enabled, settings.led.address = True, UUID(device.endpoint)
                settings.led.name, settings.led.firmware = device.ble_name or "unknown", "unknown"
                strip = LEDStrip(settings)
                strip.path = self.s.data_path / "devices" / device.id / "led.json"
            strip.before_write = lambda: self.check_location(self.store.get(device.id))
            strip.state_listener = lambda state: self.led_state(device.id, state)
            self.strips[device.id] = strip
        return self.strips[device.id]

    def check_location(self, device):
        if device.protocol == "local" or device.place_id == "general":
            return
        current = self.locations.current()
        if current.get("status") != "ready" or current.get("place_id") != device.place_id:
            raise ToolError(
                "Устройство назначено другому или неизвестному месту. "
                "Выбери текущее место вручную; удалённый канал не настроен."
            )

    def resolve(self, name):
        kind = TOOLS.get(name)
        if not kind:
            return None
        selected = device_selection.get()
        if selected:
            device = self.store.get(selected)
            if device.kind != kind:
                raise ActionDenied("Действие не относится к выбранному устройству.")
            return device
        candidates = [d for d in self.store.list() if d.kind == kind]
        context = request_context.get()
        controls = getattr(self.conv, "controls", None)
        if (
            self.preferred
            and context
            and scenario_operation.get() is None
            and context.binding == self.preferred[1]
            and controls
            and controls.device
            and controls.device
            in (
                {"led_strip"}
                if kind == "led_strip"
                else {"external_display"}
                if kind == "external_display"
                else {"mac_display", "mac_sound"}
            )
            and controls.clock() < controls.expires
        ):
            preferred = next((d for d in candidates if d.id == self.preferred[0]), None)
            if preferred:
                return preferred
        local = [
            d
            for d in candidates
            if d.place_id == "general"
            or d.protocol == "local"
            or self.locations.current().get("place_id") == d.place_id
        ]
        if len(local) == 1:
            return local[0]
        if len(candidates) == 1:
            return candidates[0]  # The shared location guard will reject a distant device.
        raise ToolError(
            "Нет однозначно выбранного добавленного устройства. "
            "Выбери его в разделе «Устройства» или уточни местоположение."
        )

    def guard(self, name):
        device = self.resolve(name)
        if device:
            self.check_location(device)
        return device

    async def led_command(self, command):
        device = self.guard("led_control")
        return await self.strip(device).command(command)

    def led_state(self, identifier, state):
        """The integration's persisted write journal is shared by all entry points."""
        device = self.store.get(identifier)
        last = state.last_write
        if (
            not last
            or last.get("status") != "written"
            or device.sent.get("written_at") == last["time"]
        ):
            return
        requested = last["requested"]
        values = {}
        action = requested["action"]
        if action in {"on", "off"}:
            values["power"] = action == "on"
        elif action == "brightness":
            values["brightness"] = requested["level"]
        elif action == "color":
            values["color"] = "#" + "".join(f"{v:02X}" for v in requested["rgb"])
        device.sent.update(values, written_at=last["time"])
        for key in values:
            device.state.pop(key, None)
        device.last_contact = last["time"]
        device.result_status = "unverified"
        self.store.observe(device)
        strip = self.strips.get(identifier)
        self.connections[identifier] = (
            "disconnected" if strip and strip.connection_closed else "unknown"
        )
        self.publish()

    def observe(self, name, args, result):
        if name not in TOOLS:
            return
        try:
            device = self.resolve(name)
        except (ValueError, ToolError):
            return
        outcome = result.get("result_status", "unverified")
        if outcome == "requires_confirmation":
            return
        if outcome == "failed":
            self.connections[device.id] = "error"
        elif name != "led_control":
            self.connections[device.id] = "observed"
        values = {}
        if outcome == "success":
            if (
                name in {"set_brightness", "get_brightness", "set_external_brightness"}
                and type(result.get("level")) is int
            ):
                values["brightness"] = result["level"]
            if name == "set_volume":
                values.update({k: result[k] for k in ("volume_percent", "muted") if k in result})
            if name == "get_system_status":
                values.update(result.get("sound", {}))
                if isinstance(result.get("battery"), dict):
                    values["battery"] = result["battery"]
        if name == "now_playing" and not result.get("error"):
            values["playback"] = {k: result[k] for k in ("state", "track", "artist") if k in result}
        if values:
            device.state.update(values)
            device.state_at = stamp()
        if name != "led_control":
            if outcome != "failed":
                device.last_contact = stamp()
            device.result_status = outcome
            self.store.observe(device)
        elif outcome in {"failed", "partial_success"}:
            device.result_status = outcome
            self.store.observe(device)
        if device_selection.get() and self.conv:
            controls = self.conv.controls
            controls.clear()
            if outcome in {"success", "unverified"}:
                self.preferred = (
                    device.id,
                    (self.conv.device.device_id, self.conv.device.session_id),
                )
                controls.device = (
                    "led_strip"
                    if device.kind == "led_strip"
                    else "external_display"
                    if device.kind == "external_display" and "brightness" in values
                    else "mac_display"
                    if "brightness" in values
                    else "mac_sound"
                    if name == "set_volume"
                    else None
                )
                if controls.device:
                    controls.binding = (
                        (self.conv.device.device_id, self.conv.device.session_id),
                        self.conv.id,
                    )
                    controls.expires = controls.clock() + controls.ttl_s
                    controls.sent_state[controls.device] = {
                        k: v
                        for k, v in self.store.get(device.id).sent.items()
                        if k in {"power", "color", "brightness"}
                    }
                    controls.confirmed_state[controls.device] = {
                        k: v
                        for k, v in device.state.items()
                        if k in {"power", "color", "brightness"}
                    }
        self.publish()

    def features(self, device):
        enabled = self.tools.enabled
        if device.kind == "led_strip":
            try:
                strip = self.strip(device)
                confirmed = strip._load().confirmed
                driver = strip.driver()
                color_reason = strip.rgb_block_reason()
            except ToolError:
                confirmed = set()
                driver = None
                color_reason = "Профиль LED недоступен. Проверь локальный журнал и настройки."
            return {
                "power_on": enabled("led_control") and "on" in confirmed,
                "power_off": enabled("led_control") and "off" in confirmed,
                "brightness": enabled("led_control") and "brightness" in confirmed,
                "color": enabled("led_control") and "rgb" in confirmed,
                "colors": sorted(confirmed),
                "color_block_reason": color_reason
                if enabled("led_control")
                else "Управление LED отключено в настройках доступа.",
                "probe": enabled("led_control") and driver is not None,
                "warm_white": "rgb_approximation",
                "physical_feedback": False,
            }
        if device.kind == "mac":
            return {
                "volume": enabled("set_volume"),
                "mute": enabled("set_volume"),
                "brightness": enabled("set_brightness"),
                "playback": enabled("media_control"),
                "battery": enabled("get_system_status"),
                "lock": enabled("mac_power"),
                "power_on": False,
                "power_off": False,
            }
        if device.kind == "airplay":
            return {
                "airplay_audio": enabled("device_command"),
                "airplay_video": enabled("device_command") and "_airplay._tcp" in device.endpoint,
                "playback": False,
                "volume": False,
                "physical_feedback": False,
            }
        if device.kind == "audio_output":
            return {"audio_output": enabled("device_command")}
        if device.kind == "wled":
            confirmed = bool(device.last_contact) and enabled("device_command")
            return {
                "power_on": confirmed,
                "power_off": confirmed,
                "brightness": confirmed,
                "color": confirmed,
                "physical_feedback": True,
            }
        return {
            "brightness": enabled("set_external_brightness"),
            "volume": False,
            "inputs": False,
            "power_on": False,
            "power_off": False,
        }

    def snapshot(self, **extra):
        location = self.locations.current()
        records = []
        for d in self.store.list():
            try:
                self.check_location(d)
                usable, reason = True, ""
            except ToolError as exc:
                usable, reason = False, str(exc)
            records.append(
                {
                    **d.model_dump(),
                    "features": self.features(d),
                    **({"ble": self.ble_status(d)} if d.kind == "led_strip" else {}),
                    **(
                        {"preview_color": self.colors.preview(d.id)}
                        if d.kind in {"led_strip", "wled"}
                        else {}
                    ),
                    "connection": self.connections.get(d.id, "unknown"),
                    "usable": usable,
                    "reason": reason,
                    "busy": d.id in self.inflight
                    or bool(self.command_locks.get(d.id) and self.command_locks[d.id].locked()),
                }
            )
        records.sort(
            key=lambda d: (
                d["place_id"] != location.get("place_id"),
                d["place_id"] != "general",
                d["name"].casefold(),
            )
        )
        return {
            "type": "devices",
            "items": records,
            "location": location,
            "places": [{"id": "general", "name": "Общие устройства"}]
            + [{"id": p.id, "name": p.name} for p in self.locations.list()],
            "kinds": [{"id": k, "name": v[0], "protocol": v[1]} for k, v in KINDS.items()],
            **self.center_snapshot(),
            **extra,
        }

    def publish(self, **extra):
        if self.bus:
            if "request_id" in extra:
                extra["operation_id"] = extra.pop("request_id")
            self.bus.publish(self.snapshot(**extra))

    def ble_status(self, device):
        try:
            return self.strip(device).driver_status()
        except ToolError:
            return {"driver": "Профиль недоступен", "compatibility": "unsupported"}

    def bind(self, bus, conv):
        self.bus, self.conv = bus, conv
        bus.handle("devices", self.request)
        self.publish()

    def request(self, value):
        task = asyncio.create_task(self.handle(value))
        self.jobs.add(task)
        if isinstance(value, dict) and isinstance(value.get("request_id"), str):
            self.operations[value["request_id"]] = task
            task.add_done_callback(lambda done: self.operations.pop(value["request_id"], None))
        task.add_done_callback(self.jobs.discard)

    def valid_place(self, identifier):
        if identifier != "general":
            self.locations.get(identifier)

    def require_adapter(self):
        from backend.mac_agent import LocalMacAgent

        if (
            not isinstance(self.tools.mac_agent, LocalMacAgent)
            or "mac.system" not in self.conv.device.capabilities
        ):
            raise ActionDenied("Для локального оборудования нужен разрешённый Mac Agent.")

    async def discover(self, kind, *, scan_id=None, show_all_ble=False):
        if kind == "auto":
            # Each protocol is bounded; unavailable adapters do not fabricate results.
            results = await asyncio.gather(
                *(
                    self.discover(k, scan_id=scan_id, show_all_ble=show_all_ble)
                    for k in ("led_strip", "airplay", "audio_output")
                ),
                return_exceptions=True,
            )
            for kind_name, result in zip(("LED", "AirPlay", "Аудиовыходы"), results, strict=True):
                if isinstance(result, Exception):
                    self.publish(message=f"{kind_name}: {result}")
            return [item for result in results if isinstance(result, list) for item in result]
        if kind not in KINDS:
            raise ValueError("Для этого типа нет подключённого протокола управления.")
        if kind == "led_strip":
            strip = LEDStrip(self.s.model_copy(deep=True))
            strip.cfg = strip.cfg.model_copy(update={"enabled": True})
            async with self.discovery_lock:
                result = await strip.discover(scan_id=scan_id, show_all_ble=show_all_ble)
            raw = result["candidates"]
            items = []
            excluded = 0
            for v in raw:
                try:
                    endpoint = str(UUID(v["address"]))
                except (ValueError, TypeError, KeyError, AttributeError):
                    excluded += 1
                    trace(
                        "backend_device",
                        scan_id or "direct",
                        included=False,
                        device=device_key(str(v.get("address", ""))),
                        reason="invalid_macos_uuid",
                    )
                    continue
                items.append(
                    {
                        **{
                            k: v[k]
                            for k in ("rssi", "compatibility", "compatibility_reason", "connection")
                            if k in v
                        },
                        "name": v.get("name") or "Без имени",
                        "endpoint": endpoint,
                    }
                )
                trace(
                    "backend_device",
                    scan_id or "direct",
                    device=device_key(endpoint),
                    included=True,
                    reason="no_location_category_or_connection_filter",
                )
            if scan_id in self.scan_reports:
                self.scan_reports[scan_id].update(
                    scanner_count=result.get("scanner_count", len(raw)),
                    ble_candidate_count=len(items),
                    excluded_count=excluded,
                )
            trace(
                "backend_candidates",
                scan_id or "direct",
                received=len(raw),
                count=len(items),
                excluded_count=excluded,
            )
        elif kind == "airplay":
            result = await self.native.run("browse")
            items = [
                v for v in result["services"] if v["service"] in {"_airplay._tcp", "_raop._tcp"}
            ]
            if result.get("warning"):
                self.publish(message=result["warning"])
        elif kind == "audio_output":
            self.outputs = (await self.native.run("outputs"))["outputs"]
            items = [{"name": v["name"], "endpoint": v["uid"]} for v in self.outputs]
        elif kind == "wled":
            raise ValueError(
                "Введи локальный IP WLED. Bonjour-объявление не подтверждает HTTP API."
            )
        elif kind == "mac":
            items = [{"name": "Этот MacBook", "endpoint": "local-mac"}]
        else:
            from backend.mac import MacDesktop
            from backend.night import BetterDisplay

            items = [
                {"name": d.get("name") or "Внешний дисплей", "endpoint": str(UUID(d["uuid"]))}
                for d in await BetterDisplay(MacDesktop(self.s)).inventory()
                if not d.get("builtin")
            ]
        candidates = []
        self.scans = {k: v for k, v in self.scans.items() if v["expires"] > time.monotonic()}
        for item in items if kind == "led_strip" else items[:30]:
            token = uuid4().hex
            self.scans[token] = {**item, "kind": kind, "expires": time.monotonic() + 300}
            if kind == "led_strip":
                trace(
                    "backend_token",
                    scan_id or "direct",
                    device=device_key(item["endpoint"]),
                    token=token[:8],
                    included=True,
                )
            candidates.append(
                {
                    "token": token,
                    "name": item["name"],
                    "kind": kind,
                    "protocol": KINDS[kind][1],
                    "status": "discovered",
                    **{
                        k: item[k]
                        for k in ("rssi", "compatibility", "compatibility_reason", "connection")
                        if k in item
                    },
                }
            )
        return candidates

    def candidate(self, value):
        if value.get("token"):
            found = self.scans.get(value["token"])
            if not found or found["expires"] < time.monotonic():
                raise ValueError("Результат поиска устарел. Повтори поиск.")
            return Device(
                name=found["name"],
                kind=found["kind"],
                protocol=KINDS[found["kind"]][1],
                endpoint=found["endpoint"],
                ble_name=found["name"] if found["kind"] == "led_strip" else None,
            )
        kind = value.get("kind")
        if kind not in KINDS:
            raise ValueError("Выбери поддерживаемый тип устройства.")
        return Device(
            name=KINDS[kind][0],
            kind=kind,
            protocol=KINDS[kind][1],
            endpoint=value.get("endpoint", "local-mac" if kind == "mac" else ""),
        )

    def operation(self, device, action, parameters):
        if not isinstance(parameters, dict):
            raise ValueError("Параметры должны быть объектом.")
        if device.kind in {"wled", "airplay", "audio_output"}:
            return (
                "device_command",
                {"target": device.id, "action": action, **parameters},
                "Управляй выбранным устройством",
            )
        if device.kind == "led_strip" and action == "led":
            params = LEDParameters.model_validate(parameters)
            name, args = LEDAction(parameters=params).call()
            return name, args, args["command"]
        if action == "brightness" and device.kind in {"mac", "external_display"}:
            if (
                set(parameters) != {"level"}
                or type(parameters["level"]) is not int
                or not 0 <= parameters["level"] <= 100
            ):
                raise ValueError("Яркость должна быть целым числом 0–100.")
            return (
                ("set_brightness" if device.kind == "mac" else "set_external_brightness"),
                parameters,
                "Установи яркость экрана Mac"
                if device.kind == "mac"
                else "Установи яркость внешнего дисплея",
            )
        if device.kind == "mac":
            if action == "volume" and set(parameters) in ({"level"}, {"mute"}):
                return "set_volume", parameters, "Установи системную громкость Mac"
            if (
                action == "playback"
                and set(parameters) == {"action"}
                and parameters["action"] in {"pause", "resume", "next", "previous", "stop"}
            ):
                commands = {
                    "pause": "Поставь музыку на паузу",
                    "resume": "Продолжи музыку",
                    "next": "Включи следующий трек",
                    "previous": "Включи предыдущий трек",
                    "stop": "Останови музыку",
                }
                return (
                    "media_control",
                    {**parameters, "target": "music"},
                    commands[parameters["action"]],
                )
            if action == "lock" and not parameters:
                return "mac_power", {"action": "lock"}, "Заблокируй экран Mac"
        raise ValueError("Это действие не поддерживается выбранным устройством.")

    async def call(self, device, name, args, text):
        if not self.tools.enabled(name):
            raise ValueError("Функция отключена в настройках доступа или не подключена.")
        self.check_location(device)
        token = device_selection.set(device.id)
        address = voice_addressed.set(True)
        try:
            with request_scope(RequestContext(self.conv.device, text)):
                async with asyncio.timeout(45):
                    return json.loads(await self.tools.call(name, args))
        finally:
            voice_addressed.reset(address)
            device_selection.reset(token)

    async def refresh(self, device):
        if device.kind == "wled":
            result = await self.network.inspect(device)
            self.accept_network_state(device, result)
            return {**result, "result_status": "success"}
        if device.kind in {"airplay", "audio_output"}:
            self.check_location(device)
            if device.kind == "audio_output":
                self.outputs = (await self.native.run("outputs"))["outputs"]
                found = next((o for o in self.outputs if o["uid"] == device.endpoint), None)
                if not found:
                    raise ToolError("Выход больше не доступен macOS.")
                device.state = {"active_output": found["active"]}
            else:
                found = next(
                    (
                        v
                        for v in (await self.native.run("browse"))["services"]
                        if v["endpoint"] == device.endpoint
                    ),
                    None,
                )
                if not found:
                    raise ToolError("Bonjour сейчас не видит этот приёмник.")
                device.state = {}  # Discovery is not a streaming/playback observation.
            device.last_contact = device.state_at = stamp()
            self.store.observe(device)
            self.connections[device.id] = "observed"
            return {
                "result_status": "success",
                "message": "Доступность подтверждена; воспроизведение не проверялось.",
            }
        if device.kind == "led_strip":
            self.check_location(device)
            lock = self.tools.device_locks.setdefault("led", asyncio.Lock())
            if lock.locked():
                raise ValueError("Лента выполняет другую команду. Дождись завершения.")
            async with lock:
                result = await self.strip(device).inspect(listen_s=0, read_values=False)
            self.connections[device.id] = (
                "disconnected" if result["connection"] == "disconnected" else "error"
            )
            device = self.store.get(device.id)
            if result["services"]:
                device.last_contact = stamp()
            self.store.observe(device)
            return result
        if device.kind == "external_display":
            from backend.mac import MacDesktop
            from backend.night import BetterDisplay

            self.check_location(device)
            service = BetterDisplay(MacDesktop(self.s))
            found = [
                d
                for d in await service.inventory()
                if str(UUID(d["uuid"])) == device.endpoint and not d.get("builtin")
            ]
            if not found:
                raise ToolError("Внешний дисплей сейчас не подключён.")
            level = round(await service.read(device.endpoint, self.s.night.external_control) * 100)
            device.state.update(brightness=level)
            device.state_at = device.last_contact = stamp()
            self.store.observe(device)
            self.connections[device.id] = "observed"
            return {
                "message": "Подключение и яркость дисплея прочитаны.",
                "result_status": "success",
            }
        results = []
        for name, args in [
            ("get_system_status", {"topic": "battery"}),
            ("get_system_status", {"topic": "volume"}),
            ("get_brightness", {}),
            ("now_playing", {}),
        ]:
            if self.tools.enabled(name):
                results.append(await self.call(device, name, args, "Покажи состояние Mac"))
        failed = any(r.get("result_status") == "failed" for r in results)
        return {
            "message": "Часть данных Mac недоступна."
            if failed
            else "Доступные данные Mac обновлены.",
            "result_status": "partial_success" if failed else "success",
        }

    async def handle(self, value):
        request_id = value.get("request_id") if isinstance(value, dict) else None
        identifier = value.get("id") if isinstance(value, dict) else None
        busy = False
        try:
            if (
                not isinstance(value, dict)
                or not self.conv
                or not self.conv.device.authenticated
                or self.conv.device.target_agent_id != "local-mac"
            ):
                raise ActionDenied(
                    "Устройства доступны только авторизованному локальному приложению."
                )
            op = value.get("op")
            if op in {"color_save", "color_delete", "color_preview"}:
                self.require_adapter()
                if not self.tools.enabled("device_command"):
                    raise ActionDenied("Управление устройствами отключено в настройках доступа.")
                if op == "color_save":
                    saved = self.colors.save(
                        value.get("value"),
                        identifier=value.get("color_id"),
                        revision=value.get("revision", 0),
                    )
                    result = {
                        "message": "Цвет сохранён в палитре без отправки ленте.",
                        "saved_color_id": saved.id,
                    }
                elif op == "color_delete":
                    self.colors.delete(
                        value.get("color_id"),
                        value.get("revision"),
                        confirmed=value.get("confirmed"),
                    )
                    result = {"message": "Пользовательский цвет удалён."}
                else:
                    device = self.store.get(identifier)
                    if device.kind not in {"led_strip", "wled"}:
                        raise ValueError("Выбери RGB-ленту.")
                    self.colors.set_preview(device.id, value.get("color"))
                    self.remember_color_target(device)
                    result = {"message": "Предпросмотр цвета. Ленте ничего не отправлено."}
                self.publish(result={**result, "result_status": "success"}, request_id=request_id)
                return
            if await self.center_handle(value):
                return
            if op == "refresh":
                self.publish(request_id=request_id)
                return
            if op == "location":
                self.locations.select("manual", value["place_id"])
                self.preferred = None
                self.conv.controls.clear()
                self.publish(message="Текущее место выбрано вручную.", request_id=request_id)
                return
            if op == "discover":
                self.require_adapter()
                if not isinstance(request_id, str) or not re.fullmatch(
                    r"[a-zA-Z0-9-]{8,80}", request_id
                ):
                    raise ValueError("Для поиска нужен уникальный идентификатор запроса.")
                if type(value.get("show_all_ble", False)) is not bool:
                    raise ValueError("Диагностический режим должен быть true/false.")
                self.scan_reports[request_id] = {
                    "scan_id": request_id,
                    "started_at": stamp(),
                    "show_all_ble": value.get("show_all_ble", False),
                    "runtime": runtime_info(),
                }
                while len(self.scan_reports) > 32:
                    self.scan_reports.popitem(last=False)
                trace(
                    "backend_requested",
                    request_id,
                    kind=value.get("kind"),
                    show_all_ble=value.get("show_all_ble", False),
                    cached=False,
                )
                with measure("device_discovery"):
                    async with asyncio.timeout(25):
                        candidates = await self.discover(
                            value.get("kind"),
                            scan_id=request_id,
                            show_all_ble=value.get("show_all_ble", False),
                        )
                report = self.scan_reports[request_id]
                report.update(finished_at=stamp(), candidate_count=len(candidates))
                trace(
                    "backend_publish",
                    request_id,
                    count=len(candidates),
                    clients=self.bus.clients if self.bus else 0,
                )
                self.publish(candidates=candidates, discovery=report, request_id=request_id)
                return
            if op == "scan_trace":
                report = self.scan_reports.get(value.get("scan_id"))
                stage = value.get("stage")
                if (
                    not report
                    or stage not in {"frontend_received", "frontend_rendered", "frontend_ignored"}
                    or type(value.get("count")) is not int
                    or not 0 <= value["count"] <= 10000
                ):
                    return
                trace(
                    stage,
                    report["scan_id"],
                    count=value["count"],
                    reason="matching_active_scan"
                    if stage != "frontend_ignored"
                    else "stale_or_closed",
                    location_filter_applied=False,
                    category_filter_applied=False,
                )
                return
            if op == "check":
                self.require_adapter()
                candidate = self.candidate(value)
                if candidate.kind == "led_strip":
                    settings = self.s.model_copy(deep=True)
                    settings.led.enabled, settings.led.address = True, UUID(candidate.endpoint)
                    settings.led.name, settings.led.firmware = (
                        candidate.ble_name or "unknown",
                        "unknown",
                    )
                    strip = LEDStrip(settings)
                    strip.path = self.s.data_path / "devices" / "connection-check" / "led.json"
                    lock = self.tools.device_locks.setdefault("led", asyncio.Lock())
                    if lock.locked():
                        raise ValueError("Лента выполняет другую команду. Повтори проверку позже.")
                    async with lock:
                        inspection = await strip.inspect(listen_s=0, read_values=False)
                    message = inspection["message"]
                elif candidate.kind == "external_display":
                    from backend.mac import MacDesktop
                    from backend.night import BetterDisplay

                    found = await BetterDisplay(MacDesktop(self.s)).inventory()
                    if not any(
                        str(UUID(d["uuid"])) == candidate.endpoint and not d.get("builtin")
                        for d in found
                    ):
                        raise ToolError("Выбранный внешний дисплей сейчас не подключён.")
                    message = (
                        "Дисплей найден через BetterDisplay. Доступна яркость; "
                        "питание, громкость и входы не подключены."
                    )
                elif candidate.kind == "wled":
                    await self.network.inspect(candidate)
                    message = (
                        "WLED JSON API ответил. Доступны питание, RGB и яркость главного сегмента."
                    )
                elif candidate.kind == "airplay":
                    services = (await self.native.run("browse"))["services"]
                    if not any(v["endpoint"] == candidate.endpoint for v in services):
                        raise ToolError("AirPlay-приёмник больше не обнаруживается.")
                    message = (
                        "Bonjour видит приёмник. Выбор аудио и зеркалирование "
                        "выполняются отдельно через macOS; пульт приёмника не подключён."
                    )
                elif candidate.kind == "audio_output":
                    self.outputs = (await self.native.run("outputs"))["outputs"]
                    if not any(v["uid"] == candidate.endpoint for v in self.outputs):
                        raise ToolError("Аудиовыход больше не доступен macOS.")
                    message = "Выход найден в CoreAudio. Выбор будет подтверждён системой отдельно."
                else:
                    message = (
                        "Локальные функции Mac зарегистрированы. Батарея, звук и яркость "
                        "читаются кнопкой обновления после добавления."
                    )
                self.publish(
                    checked={
                        "message": message,
                        **(
                            {
                                "compatibility": inspection["compatibility"],
                                "connection": inspection["connection"],
                            }
                            if candidate.kind == "led_strip"
                            else {}
                        ),
                    },
                    request_id=request_id,
                )
                return
            if op == "add":
                device = self.candidate(value)
                device.name, device.place_id, device.room = (
                    value["name"],
                    value.get("place_id", "general"),
                    value.get("room", ""),
                )
                device.auto_connect = value.get("auto_connect", False)
                device = Device.model_validate(device.model_dump())
                self.valid_place(device.place_id)
                saved = self.store.save(device)
                self.ensure_led_tool()
                self.update_catalogue()
                self.publish(
                    saved=saved.id,
                    message="Устройство добавлено. Подключение проверяется отдельной кнопкой.",
                    request_id=request_id,
                )
                return
            device = self.store.get(identifier)
            if op in {"update", "delete", "favorites"} and (
                self.command_locks.get(identifier) and self.command_locks[identifier].locked()
            ):
                raise ValueError("Устройство выполняет команду. Дождись завершения.")
            if op in {"update", "delete"}:
                resource = {"led_strip": "led", "external_display": "tv", "mac": "mac"}.get(
                    device.kind, device.id
                )
                if (
                    self.tools.device_locks.get(resource)
                    and self.tools.device_locks[resource].locked()
                ):
                    raise ValueError(
                        "Устройство выполняет команду. Настройки можно изменить после завершения."
                    )
            if op in {
                "update",
                "delete",
                "favorites",
                "act",
                "inspect",
                "diagnose_ble",
                "ble_profile",
                "probe_led",
                "confirm_probe",
                "confirm",
            }:
                if identifier in self.inflight:
                    raise ValueError("Устройство занято. Дождись завершения текущей команды.")
                self.inflight.add(identifier)
                busy = True
            if op in {"update", "delete", "favorites"}:
                if type(value.get("revision")) is not int or value["revision"] != device.revision:
                    raise ValueError("Устройство изменилось в другом окне. Обнови список.")
                if op == "delete":
                    self.store.delete(identifier, device.revision)
                    self.strips.pop(identifier, None)
                else:
                    if op == "update":
                        for key in (
                            "name",
                            "place_id",
                            "room",
                            "aliases",
                            "favorite",
                            "auto_connect",
                        ):
                            if key in value:
                                setattr(device, key, value[key])
                        self.valid_place(device.place_id)
                    else:
                        colors = value["colors"]
                        if (
                            not isinstance(colors, list)
                            or len(colors) > 16
                            or any(
                                not isinstance(c, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", c)
                                for c in colors
                            )
                        ):
                            raise ValueError("Сохрани не больше 16 RGB-цветов.")
                        device.favorites = list(dict.fromkeys(c.upper() for c in colors))
                    self.store.save(Device.model_validate(device.model_dump()))
                self.update_catalogue()
                self.publish(
                    message="Устройство удалено из списка."
                    if op == "delete"
                    else "Настройки устройства сохранены.",
                    request_id=request_id,
                )
                return
            self.check_location(device)
            if op == "ble_profile":
                self.require_adapter()
                if device.kind != "led_strip" or value.get("consent") is not True:
                    raise ActionDenied("Выбери ленту и явно подтверди смену BLE-профиля.")
                if "mac.system" not in self.conv.device.capabilities:
                    raise ActionDenied("Смена BLE-профиля доступна только локальному Mac.")
                strip = self.strip(device)
                if "profile" in value:
                    if len(json.dumps(value["profile"])) > 32768:
                        raise ValueError("Профиль больше 32 КБ.")
                    profile = BLEProfile.model_validate(value["profile"])
                else:
                    choices = {
                        "auto": None,
                        "elk-bleddm-candidate": DDM_PROFILE,
                        "elk-bledob-legacy": legacy_profile(strip.cfg),
                    }
                    if value.get("driver") not in choices:
                        raise ValueError("Неизвестный BLE-драйвер.")
                    profile = choices[value["driver"]]
                changed = await strip.select_profile(profile)
                self.probes.pop(identifier, None)
                if changed:
                    device.sent, device.state = {}, {}
                device.result_status = "unverified"
                self.store.observe(device)
                self.update_catalogue()
                result = {
                    "message": "Профиль сохранён. Допуски сброшены; "
                    "выполни диагностику и ручную проверку.",
                    "result_status": "unverified",
                }
            elif op == "diagnose_ble":
                self.require_adapter()
                if device.kind != "led_strip" or "mac.system" not in self.conv.device.capabilities:
                    raise ActionDenied("Диагностика BLE доступна для выбранной ленты на Mac.")
                lock = self.tools.device_locks.setdefault("led", asyncio.Lock())
                if lock.locked():
                    raise ValueError("Лента занята другой командой.")
                async with lock:
                    result = await self.strip(device).inspect()
                self.connections[device.id] = result["connection"]
                self.update_catalogue()
            elif op == "inspect":
                self.require_adapter()
                with measure("device_connect"):
                    async with asyncio.timeout(45):
                        result = await self.refresh(device)
            elif op == "act":
                if not isinstance(request_id, str) or not re.fullmatch(
                    r"[a-zA-Z0-9-]{8,80}", request_id
                ):
                    raise ValueError("Для команды нужен уникальный идентификатор события.")
                key = (self.conv.device.session_id, request_id)
                identity = json.dumps(
                    [identifier, value.get("action"), value.get("parameters", {})], sort_keys=True
                )
                if key in self.completed:
                    if self.completed[key][0] != identity:
                        raise ValueError("Идентификатор уже относится к другой команде.")
                    self.publish(result=self.completed[key][1], request_id=request_id)
                    return
                name, args, text = self.operation(
                    device, value.get("action"), value.get("parameters", {})
                )
                self.completed[key] = (
                    identity,
                    {
                        "result_status": "unverified",
                        "message": "Команда уже принята; автоматического повтора нет.",
                    },
                )
                self.publish(request_id=request_id, running=True)
                started = time.monotonic()
                with measure("device_command"):
                    result = await self.call(device, name, args, text)
                if name != "device_command":
                    elapsed = round((time.monotonic() - started) * 1000)
                    self.store.log(
                        device.id, value.get("action"), value.get("parameters", {}), result, elapsed
                    )
                    self.metrics = {
                        "device_id": device.id,
                        "action": value.get("action"),
                        "total_ms": elapsed,
                    }
                if result.get("status") == "confirmation_required":
                    pending = self.tools.confirmations.pending.get(
                        (self.conv.device.device_id, self.conv.device.session_id)
                    )
                    self.probes["lock:" + identifier] = pending.context.request_id
                self.completed[key] = (identity, result)
                while len(self.completed) > 128:
                    self.completed.popitem(last=False)
            elif op == "confirm":
                pending = self.tools.confirmations.pending.get(
                    (self.conv.device.device_id, self.conv.device.session_id)
                )
                if (
                    not pending
                    or pending.name != "mac_power"
                    or pending.arguments != {"action": "lock"}
                    or self.probes.get("lock:" + identifier) != pending.context.request_id
                ):
                    raise ValueError("Подтверждение блокировки устарело. Повтори действие.")
                token = device_selection.set(device.id)
                try:
                    context = RequestContext(
                        self.conv.device, "Подтверждаю " + str(value.get("code", ""))
                    )
                    message = await self.tools.confirmations.respond(
                        context, self.tools.call, self.tools.direct_reply
                    )
                    result = {"message": message, "result_status": "unverified"}
                finally:
                    device_selection.reset(token)
            elif op == "probe_led":
                self.require_adapter()
                if (
                    device.kind != "led_strip"
                    or value.get("consent") is not True
                    or not self.tools.enabled("led_control")
                    or "mac.system" not in self.conv.device.capabilities
                ):
                    raise ActionDenied(
                        "Для теста LED нужно явно выбрать и подтвердить одну функцию."
                    )
                params = LEDParameters.model_validate(value.get("parameters"))
                nonnull = params.model_dump(exclude_none=True)
                if len(nonnull) != 1 or "brightness_delta" in nonnull:
                    raise ValueError("Проверяй одну абсолютную функцию за раз.")
                if params.color:
                    args = {
                        "action": "color",
                        "rgb": [int(params.color[i : i + 2], 16) for i in (1, 3, 5)],
                    }
                elif params.brightness is not None:
                    args = {"action": "brightness", "level": params.brightness}
                else:
                    args = {"action": "on" if params.power else "off"}
                lock = self.tools.device_locks.setdefault("led", asyncio.Lock())
                if lock.locked():
                    raise ValueError("Лента занята другой командой.")
                async with lock:
                    result = await self.strip(device).execute(**args, experiment=True)
                self.probes[identifier] = result["probe_id"]
                result["device_id"] = identifier
                result["message"] = (
                    "Тестовая команда отправлена. Проверь ленту и отметь, сработала ли функция. "
                    "Физический результат пока не подтверждён."
                )
            elif op == "confirm_probe":
                if (
                    device.kind != "led_strip"
                    or type(value.get("observed")) is not bool
                    or not self.strip(device)._load().pending_probe
                    or value.get("probe_id") != self.strip(device)._load().pending_probe["id"]
                ):
                    raise ValueError("Тест функции устарел. Повтори проверку.")
                await self.strip(device).confirm_probe(value["probe_id"], value["observed"])
                self.probes.pop(identifier, None)
                self.update_catalogue()
                result = {
                    "message": "Наблюдение сохранено. "
                    "Допуск функции относится только к этому профилю LED.",
                    "result_status": "success",
                }
            else:
                raise ValueError("Неизвестное действие раздела устройств.")
            self.publish(result=result, request_id=request_id)
        except asyncio.CancelledError:
            self.publish(
                error="Ожидание прервано. Фактический результат не подтверждён; повтора нет.",
                request_id=request_id,
            )
            raise
        except (
            ValueError,
            KeyError,
            TypeError,
            ActionDenied,
            ToolError,
            OSError,
            TimeoutError,
            sqlite3.Error,
            httpx.HTTPError,
        ) as exc:
            if isinstance(identifier, str):
                self.connections[identifier] = (
                    "error"
                    if isinstance(exc, (ToolError, TimeoutError, OSError))
                    else self.connections.get(identifier, "unknown")
                )
                if value.get("op") in {"act", "probe_led"}:
                    try:
                        failed_device = self.store.get(identifier)
                        failed_device.result_status = "failed"
                        self.store.observe(failed_device)
                    except (ValueError, sqlite3.Error):
                        pass
            message = (
                str(exc)
                if isinstance(exc, (ToolError, ActionDenied)) or type(exc) is ValueError
                else "Не удалось применить действие. Проверь параметры, доступ и подключение. "
                "Автоповтора нет."
            )
            self.publish(error=message, request_id=request_id)
        finally:
            if busy:
                self.inflight.discard(identifier)
                self.publish()

    async def close(self):
        for task in list(self.jobs):
            task.cancel()
        if self.jobs:
            await asyncio.gather(*self.jobs, return_exceptions=True)
        self.jobs.clear()
        self.operations.clear()
