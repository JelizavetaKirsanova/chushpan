"""Validated structured LLM fallback; exposes fixed capabilities, never executable code."""

import re
from uuid import uuid4

from backend.automation.matching import BLOCKED
from backend.automation.models import Block, Scenario
from backend.context import request_context
from backend.led import _NUMBERS
from backend.security import ActionDenied, Risk, command_text, explicit_request
from backend.tools.registry import Tool

TARGETS = {
    "led.set": r"лент|подсвет|свет",
    "mac.brightness": (
        r"(?:mac|мак|ноутбук|встроенн).*(?:яркост|экран)|"
        r"яркост.*(?:mac|мак|ноутбук|встроенн)"
    ),
    "mac.volume": r"громк|системн.*звук|звук.*(?:mac|мак|компьютер)",
    "display.brightness": r"(?:телевизор|внешн).*яркост|яркост.*(?:телевизор|внешн)",
    "mac.open_app": r"открой|запусти",
    "music.command": r"музык|песн|трек|плейлист",
}


def natural_tool(service):
    supported = [key for key in TARGETS if key in service.capabilities.items]
    properties = {}
    for key in supported:
        properties.update(service.capabilities.items[key].parameters)
    # Union fields stay optional here; each selected capability validates its own
    # complete schema, numeric limits and required fields before any side effect.
    parameter_schema = {"type": "object", "properties": properties, "additionalProperties": False}

    def validate(actions, text):
        command = command_text(text)
        if BLOCKED.search(text) or not re.match(
            r"^(?:сделай|поставь|хочу|включи|выключи|вруби|выруби|измени|поменяй|"
            r"установи|прибавь|убавь|уменьши|увеличь|открой|запусти)\b",
            command,
        ):
            raise ActionDenied("Для управления нужна прямая просьба без отрицаний и условий.")
        if not actions:
            raise ValueError("Список действий пуст.")
        blocks = []
        for action in actions:
            if set(action) != {"capability", "parameters"}:
                raise ValueError("Неизвестные поля намерения.")
            key, parameters = action["capability"], action["parameters"]
            if key not in supported or not re.search(TARGETS[key], command):
                raise ActionDenied("Устройство из плана не названо в исходной просьбе.")
            if key == "mac.open_app" and not explicit_request("open_app", parameters, text):
                raise ActionDenied("Приложение из плана не названо в просьбе.")
            for field in ("level", "brightness"):
                if field in parameters:
                    value = parameters[field]
                    spelled = any(
                        n == value and re.search(r"\b" + re.escape(word) + r"\b", command)
                        for word, n in _NUMBERS.items()
                    )
                    if (
                        not re.search(r"\b" + re.escape(str(value)) + r"\b", command)
                        and not spelled
                    ):
                        raise ActionDenied(
                            "Числовой параметр отсутствует в исходной просьбе. Назови процент."
                        )
            if key == "led.set" and "color" in parameters:
                parameters = {**parameters, "color": parameters["color"].upper()}
            capability = service.capabilities.get(key)
            args = capability.arguments(parameters)
            if service.capabilities.risk(capability, args) == Risk.DANGEROUS:
                raise ActionDenied("Опасные действия не входят в быстрый план устройств.")
            blocks.append(Block(capability=key, parameters=parameters))
        scenario = Scenario(id=uuid4().hex, revision=1, name="Естественная команда", actions=blocks)
        service.capabilities.validate(scenario)
        return scenario

    async def execute(actions):
        context = request_context.get()
        if context is None:
            raise ActionDenied("Нет текущего запроса.")
        scenario = validate(actions, context.text)
        return await service.engine.run(scenario)

    return Tool(
        "control_devices",
        "Нестандартная прямая команда управления устройствами. "
        "Верни структурированные действия по названным устройствам. Не выдумывай проценты, "
        "устройства и параметры. Для известных формулировок используется локальный путь.",
        execute,
        {
            "actions": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "capability": {"type": "string", "enum": supported},
                        "parameters": parameter_schema,
                    },
                    "required": ["capability", "parameters"],
                    "additionalProperties": False,
                },
            }
        },
        ["actions"],
        risk=Risk.CHANGE,
        intent=lambda args, text: bool(validate(args["actions"], text)),
        reply=lambda result: result.get("message", "Результат не подтверждён."),
    )
