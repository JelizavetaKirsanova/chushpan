"""Small, strict validator for the JSON Schema subset used by assistant tools."""

from __future__ import annotations

import json
import math
import re
from pathlib import PurePath
from typing import Any


class ArgumentError(ValueError):
    pass


def validate_value(key: str, value: Any, schema: dict) -> None:
    kind = schema.get("type")
    valid = {
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "boolean": type(value) is bool,
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
    }
    if kind not in valid or not valid[kind]:
        raise ArgumentError(f"Параметр {key}: ожидается {kind}.")
    if "enum" in schema and value not in schema["enum"]:
        raise ArgumentError(f"Параметр {key}: недопустимое значение.")
    if isinstance(value, str):
        if len(value) > schema.get("maxLength", 1000):
            raise ArgumentError(f"Параметр {key}: слишком длинная строка.")
        if len(value.strip()) < schema.get("minLength", 0):
            raise ArgumentError(f"Параметр {key}: строка не должна быть пустой.")
        if any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ArgumentError(f"Параметр {key}: управляющие символы запрещены.")
        if schema.get("pattern") and not re.fullmatch(schema["pattern"], value):
            raise ArgumentError(f"Параметр {key}: неверный формат.")
        if schema.get("format") == "safe-path":
            if ".." in PurePath(value).parts or "\\" in value or value.startswith("-"):
                raise ArgumentError(f"Параметр {key}: небезопасный путь.")
    elif type(value) in (int, float):
        if not math.isfinite(value):
            raise ArgumentError(f"Параметр {key}: число должно быть конечным.")
        if value < schema.get("minimum", -1e9) or value > schema.get("maximum", 1e9):
            raise ArgumentError(f"Параметр {key}: число вне допустимого диапазона.")
    elif isinstance(value, list):
        if len(value) > schema.get("maxItems", 100):
            raise ArgumentError(f"Параметр {key}: слишком много элементов.")
        for item in value:
            validate_value(key, item, schema.get("items", {}))
    elif isinstance(value, dict):
        validate_arguments(value, schema.get("properties", {}), schema.get("required", []))


def validate_arguments(arguments: Any, properties: dict, required: list[str]) -> dict:
    if isinstance(arguments, str):
        if len(arguments) > 16_384:
            raise ArgumentError("Аргументы слишком велики.")
        try:
            arguments = json.loads(arguments or "{}")
        except (ValueError, RecursionError) as exc:
            raise ArgumentError("arguments are not valid JSON — нужен корректный JSON.") from exc
        if not isinstance(arguments, dict):
            raise ArgumentError("Аргументы должны быть JSON-объектом.")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ArgumentError("Аргументы должны быть JSON-объектом.")
    for key in required:
        if key not in arguments:
            raise ArgumentError(f"Не указан обязательный параметр {key}.")
    # Drop unknown keys for compatibility; they can never reach the handler.
    result = {key: value for key, value in arguments.items() if key in properties}
    for key, value in result.items():
        validate_value(key, value, properties[key])
    return result
