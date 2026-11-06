"""Declarative BLE drivers. Names select candidates, never grant permission to write."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.tools.registry import ToolError

Action = Literal["on", "off", "color", "brightness"]
Token = int | Literal["r", "g", "b", "level"]
SOURCE = (
    "https://github.com/dave-code-ruiz/elkbledom/blob/main/custom_components/elkbledom/models.json"
)


class BLEProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z0-9-]{1,60}$")
    name: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=500)
    service: str
    write_characteristic: str
    write_mode: Literal["without-response", "response", "auto"] = "without-response"
    commands: dict[Action, list[Token]] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_commands(self):
        for value in (self.service, self.write_characteristic):
            if str(UUID(value)) != value:
                raise ValueError("Нужен полный UUID в нижнем регистре.")
        for action, tokens in self.commands.items():
            if not 1 <= len(tokens) <= 64:
                raise ValueError("Пакет должен содержать 1–64 байта.")
            fields = {t for t in tokens if isinstance(t, str)}
            expected = (
                {"r", "g", "b"}
                if action == "color"
                else {"level"}
                if action == "brightness"
                else set()
            )
            if fields != expected or any(type(t) is int and not 0 <= t <= 255 for t in tokens):
                raise ValueError("Недопустимые поля или байты команды.")
        return self

    @property
    def fingerprint(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def serialize(self, action, *, rgb=None, level=None):
        fields = {}
        if action == "color":
            if (
                not isinstance(rgb, (list, tuple))
                or len(rgb) != 3
                or any(type(v) is not int or not 0 <= v <= 255 for v in rgb)
            ):
                raise ToolError("Нужен RGB 0–255.")
            fields = dict(zip("rgb", rgb, strict=True))
        elif action == "brightness":
            if type(level) is not int or not 0 <= level <= 100:
                raise ToolError("Нужна яркость 0–100.")
            fields = {"level": level}
        elif action not in {"on", "off"} or rgb is not None or level is not None:
            raise ToolError("Неизвестный режим подсветки.")
        if action not in self.commands:
            raise ToolError("Драйвер не поддерживает эту функцию; команды не отправлял.")
        return bytes(fields[t] if isinstance(t, str) else t for t in self.commands[action])

    def bind(self, services):
        matches = [
            c
            for s in services
            if s.uuid.lower() == self.service
            for c in s.characteristics
            if c.uuid.lower() == self.write_characteristic
        ]
        if len(matches) != 1:
            raise ToolError("GATT профиля отсутствует или неоднозначен; команды не отправлял.")
        char = matches[0]
        response = self.write_mode == "response"
        if self.write_mode == "auto":
            response = "write-without-response" not in char.properties
        prop = "write" if response else "write-without-response"
        if prop not in char.properties:
            raise ToolError("GATT не поддерживает режим записи профиля; команды не отправлял.")
        return char, response


def legacy_profile(cfg):
    return BLEProfile(
        id="elk-bledob-legacy",
        name="ELK-BLEDOB · существующий драйвер",
        source="https://github.com/user154lt/ELK-BLEDOM-Command-Util",
        service=str(cfg.service),
        write_characteristic=str(cfg.write_characteristic),
        commands={
            "on": [126, 4, 4, 1, 0, 1, 255, 0, 239],
            "off": [126, 4, 4, 0, 0, 0, 255, 0, 239],
            "color": [126, 7, 5, 3, "r", "g", "b", 16, 239],
            "brightness": [126, 4, 1, "level", 255, 255, 255, 0, 239],
        },
    )


DDM_PROFILE = BLEProfile(
    id="elk-bleddm-candidate",
    name="ELK-BLEDDM · кандидат, нужна проверка",
    source=SOURCE,
    service="0000fff0-0000-1000-8000-00805f9b34fb",
    write_characteristic="0000fff3-0000-1000-8000-00805f9b34fb",
    commands={
        "on": [126, 4, 4, 240, 0, 1, 255, 0, 239],
        "off": [126, 4, 4, 0, 0, 0, 255, 0, 239],
        "color": [126, 0, 5, 3, "r", "g", "b", 0, 239],
        "brightness": [126, 4, 1, "level", 255, 0, 255, 0, 239],
    },
)


def candidate_profile(cfg):
    name = cfg.name.upper().strip()
    if re.fullmatch(r"ELK-BLEDOB(?:\s.*)?", name):
        return legacy_profile(cfg)
    if re.fullmatch(r"ELK-BLEDDM(?:\s.*)?", name):
        return DDM_PROFILE
    return None


def gatt_fingerprint(services):
    shape = sorted(
        (s.uuid.lower(), sorted((c.uuid.lower(), sorted(c.properties)) for c in s.characteristics))
        for s in services
    )
    return hashlib.sha256(json.dumps(shape).encode()).hexdigest()
