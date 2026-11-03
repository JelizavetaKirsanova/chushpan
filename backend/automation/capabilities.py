"""Capability catalogue backed exclusively by registered, guarded integration tools."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from backend.intents import LEDAction, LEDParameters
from backend.security import risk_for
from backend.tools.validation import validate_arguments


@dataclass
class Capability:
    id: str
    name: str
    device: str
    tool: str
    parameters: dict
    required: list[str]
    permissions: list[str] = field(default_factory=list)
    description: str = ""

    def arguments(self, parameters):
        if set(parameters) - self.parameters.keys():
            raise ValueError("Неизвестные параметры действия.")
        args = validate_arguments(parameters, self.parameters, self.required)
        if self.id == "led.set":
            return LEDAction(parameters=LEDParameters(**args)).call()[1]
        return args


class CapabilityRegistry:
    def __init__(self, tools, settings):
        self.tools, self.s = tools, settings
        self.items: dict[str, Capability] = {}
        definitions = [
            (
                "assistant.personality",
                "Выбрать характер Чушпана",
                "assistant",
                "set_personality_profile",
                [],
            ),
            ("mac.brightness", "Яркость MacBook", "mac", "set_brightness", []),
            ("mac.volume", "Системная громкость", "mac", "set_volume", ["automation"]),
            ("mac.open_app", "Открыть приложение", "mac", "open_app", []),
            ("mac.app", "Переключить / закрыть приложение", "mac", "manage_app", ["automation"]),
            ("mac.website", "Открыть YouTube", "mac", "open_website", []),
            ("display.brightness", "Яркость внешнего дисплея", "tv", "set_external_brightness", []),
            ("music.command", "Управление музыкой", "music", "music_command", ["automation"]),
            ("audio.volume", "Громкость аудиоплеера", "music", "media_volume", ["automation"]),
            (
                "audio.control",
                "Плеер: пауза, следующий трек и режимы",
                "music",
                "media_control",
                ["automation"],
            ),
            ("music.play", "Включить музыку по названию", "music", "play_music", ["automation"]),
            (
                "shortcut.run",
                "Запустить разрешённую Shortcut",
                "shortcuts",
                "run_shortcut",
                ["shortcuts"],
            ),
            ("mac.power", "Сон / блокировка / выключение Mac", "mac", "mac_power", []),
        ]
        for identifier, name, device, tool_name, permissions in definitions:
            tool = tools._tools.get(tool_name)
            if tool:
                self.items[identifier] = Capability(
                    identifier,
                    name,
                    device,
                    tool_name,
                    tool.parameters,
                    tool.required,
                    permissions,
                    tool.description,
                )
        if "led_control" in tools:
            # These JSON Schema types are nullable in Pydantic; the editor uses concrete fields.
            schema = {
                "power": {"type": "boolean"},
                "color": {"type": "string", "pattern": "#[0-9A-F]{6}"},
                "brightness": {"type": "integer", "minimum": 0, "maximum": 100},
                "brightness_delta": {"type": "integer", "enum": [-10, 10]},
            }
            self.items["led.set"] = Capability(
                "led.set",
                "Питание, цвет и яркость ленты",
                "led",
                "led_control",
                schema,
                [],
                ["bluetooth"],
                "Проверенные функции ELK-BLEDOB; физическая обратная связь отсутствует.",
            )

    def get(self, identifier):
        if identifier not in self.items or self.items[identifier].tool not in self.tools:
            raise ValueError(f"Действие {identifier!r} отключено или не поддерживается.")
        return self.items[identifier]

    def risk(self, capability, arguments):
        tool = self.tools._tools[capability.tool]
        return (
            tool.risk_resolver(arguments)
            if tool.risk_resolver
            else tool.risk or risk_for(tool.name, arguments)
        )

    def validate(self, scenario):
        variables = {v.name: v for v in scenario.variables}

        def check(blocks):
            for block in blocks:
                if not block.enabled:
                    continue
                if block.kind == "action":
                    if not block.required and (
                        block.capability not in self.items
                        or self.items[block.capability].tool not in self.tools
                    ):
                        continue
                    capability = self.get(block.capability)
                    parameters = {}
                    for key, value in block.parameters.items():
                        if isinstance(value, dict):
                            if set(value) != {"variable"} or value["variable"] not in variables:
                                raise ValueError("Неизвестная переменная параметра.")
                            value = variables[value["variable"]].default
                        parameters[key] = value
                    args = capability.arguments(parameters)
                    if capability.id == "shortcut.run":
                        # The existing service checks the configured UUID and accepted input type.
                        self.tools._tools[capability.tool].prepare(args)
                check(block.children)
                check(block.otherwise)

        check(scenario.actions)
        conditions = scenario.conditions + list(self.conditions(scenario.actions))
        for variant in scenario.variants:
            if variant.enabled:
                check(variant.actions)
                conditions += variant.conditions + list(self.conditions(variant.actions))
        for condition in conditions:
            if condition.source == "variable" and condition.key not in variables:
                raise ValueError("Условие ссылается на неизвестную переменную.")
            if condition.source == "available":
                self.get(condition.key)
            if condition.source == "time" and condition.key != "hour":
                raise ValueError("Для времени поддерживается только час (hour).")
            if condition.source == "location" and condition.key != "place":
                raise ValueError("Для места поддерживается только профиль (place).")
        return scenario

    @staticmethod
    def conditions(blocks):
        for block in blocks:
            if not block.enabled:
                continue
            if block.condition:
                yield block.condition
            yield from CapabilityRegistry.conditions(block.children)
            yield from CapabilityRegistry.conditions(block.otherwise)

    def catalogue(self):
        result = []
        for capability in self.items.values():
            parameters = json.loads(json.dumps(capability.parameters))
            if capability.id == "shortcut.run":
                parameters["name"]["enum"] = list(self.s.shortcuts.allowed)
            if capability.id == "assistant.personality":
                profiles = getattr(self.tools, "personality_profiles", None)
                if profiles:
                    parameters["profile_id"]["enum"] = [p.id for p in profiles.list()]
            result.append(
                {
                    "id": capability.id,
                    "name": capability.name,
                    "device": capability.device,
                    "description": capability.description,
                    "parameters": parameters,
                    "required": capability.required,
                    "permissions": capability.permissions,
                    "risk": "depends_on_parameters"
                    if capability.id in {"shortcut.run", "mac.app"}
                    else (
                        self.tools._tools[capability.tool].risk or risk_for(capability.tool, {})
                    ).value,
                    "availability": "not_checked",
                }
            )
        return result

    @staticmethod
    def verify(tool, args, result: Any):
        if not isinstance(result, dict):
            return "unverified"
        if (
            result.get("error")
            or result.get("ok") is False
            or result.get("delivery_state") == "partial"
        ):
            return "failed"
        if result.get("status") == "confirmation_required":
            return "blocked"
        if tool == "set_personality_profile":
            return (
                "confirmed"
                if result.get("ok") is True and result.get("profile_id") == args.get("profile_id")
                else "unverified"
            )
        if tool in {"set_brightness", "set_external_brightness"}:
            return "confirmed" if result.get("level") == args.get("level") else "unverified"
        if (
            tool == "set_volume"
            and result.get("ok")
            and all(
                result.get({"level": "volume_percent", "mute": "muted"}[key]) == value
                for key, value in args.items()
                if key in {"level", "mute"}
            )
            and (
                "direction" not in args
                or type(result.get("volume_percent")) is int
                and 0 <= result["volume_percent"] <= 100
            )
        ):
            return "confirmed"
        if result.get("physical_confirmed") is True or result.get("device_state_confirmed") is True:
            return "confirmed"
        if (
            tool == "music_command"
            and result.get("ok")
            and (result.get("playback_confirmed") or result.get("state") == "paused")
        ):
            return "confirmed"
        if (
            result.get("written")
            or result.get("submitted")
            or result.get("delivery_state") == "sent"
        ):
            return "sent"
        if tool in {"open_app", "run_shortcut"} and result.get("ok"):
            return "sent"  # Process completion is not device/screen confirmation.
        return "unverified"
