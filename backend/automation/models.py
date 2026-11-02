"""Versioned, data-only automations. No scripts, expressions or executable templates."""

from __future__ import annotations

import re
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.brain.privacy import contains_secret
from backend.security import command_text


def normalize_phrase(text: str) -> str:
    text = command_text(text).replace("ё", "е")
    text = re.sub(r"\b(?:пожалуйста|ну|давай|ка)\b", " ", text)
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Variable(Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    kind: Literal["number", "string", "boolean"] = "number"
    default: Any = 50

    @model_validator(mode="after")
    def valid_default(self):
        expected = {"number": (int, float), "string": (str,), "boolean": (bool,)}[self.kind]
        if type(self.default) not in expected:
            raise ValueError("Тип значения переменной не совпадает с объявлением.")
        return self


class Condition(Model):
    source: Literal["variable", "available", "time"] = "variable"
    key: str = Field(min_length=1, max_length=120)
    operator: Literal["eq", "ne", "gt", "lt"] = "eq"
    value: Any = True


class Block(Model):
    kind: Literal["action", "delay", "sequence", "parallel", "if"] = "action"
    capability: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    seconds: float | int = Field(default=1, ge=0, le=3600)
    condition: Condition | None = None
    children: list[Block] = Field(default_factory=list)
    otherwise: list[Block] = Field(default_factory=list)
    on_error: Literal["stop", "continue"] = "stop"
    enabled: bool = True
    required: bool = True

    @model_validator(mode="after")
    def coherent(self):
        if self.kind == "action" and not self.capability:
            raise ValueError("Выбери действие из реестра.")
        if self.kind == "if" and self.condition is None:
            raise ValueError("Для IF нужно условие.")
        if self.kind not in {"if", "sequence", "parallel"} and (self.children or self.otherwise):
            raise ValueError("Вложенные блоки допустимы только в группах и IF.")
        if self.kind != "if" and self.otherwise:
            raise ValueError("ELSE допустим только для IF.")
        return self


class Trigger(Model):
    kind: Literal["voice", "manual", "schedule"] = "voice"
    at: str = Field(default="22:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekdays: list[int] = Field(default_factory=lambda: list(range(7)))

    @model_validator(mode="after")
    def days(self):
        if not self.weekdays or any(not 0 <= day <= 6 for day in self.weekdays):
            raise ValueError("Дни недели должны быть от 0 до 6.")
        return self


class Variant(Model):
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=120)
    place_id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_-]+$")
    enabled: bool = True
    actions: list[Block] = Field(default_factory=list)
    conditions: list[Condition] = Field(default_factory=list)


class Place(Model):
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-z0-9_-]{1,40}$")
    revision: int = Field(default=0, ge=0)
    name: str = Field(min_length=1, max_length=120)
    cities: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid(self):
        if any(not city.strip() or len(city) > 120 for city in self.cities):
            raise ValueError("Название города должно содержать от 1 до 120 символов.")
        if contains_secret(self.model_dump_json()):
            raise ValueError("Секреты нельзя сохранять в настройках места.")
        return self


class Scenario(Model):
    id: str = Field(default="", pattern=r"^(?:[a-f0-9]{32})?$")
    revision: int = Field(default=0, ge=0)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    icon: str = Field(default="✦", max_length=12)
    enabled: bool = True
    mode: Literal["exact", "smart"] = "smart"
    phrases: list[str] = Field(default_factory=list)  # Deliberately no phrase-count limit.
    actions: list[Block] = Field(default_factory=list)
    variants: list[Variant] = Field(default_factory=list)
    conditions: list[Condition] = Field(default_factory=list)
    triggers: list[Trigger] = Field(default_factory=lambda: [Trigger(), Trigger(kind="manual")])
    variables: list[Variable] = Field(default_factory=list)
    confirmation: Literal["auto", "always"] = "auto"
    # The root actions are the mandatory General version, even when its list is empty.
    schema_version: Literal[2] = 2
    version_policy: Literal["fallback"] = "fallback"
    general_enabled: Literal[True] = True
    inter_block_delay_s: float | int = Field(default=0, ge=0, le=60)
    show_status: bool = True
    error_policy: Literal["block", "stop", "continue"] = "block"
    timeout_s: float | int | None = Field(default=None, ge=1, le=3600)

    @model_validator(mode="after")
    def valid(self):
        seen = set()
        for phrase in self.phrases:
            normalized = normalize_phrase(phrase)
            if not normalized or len(phrase) > 500:
                raise ValueError("Фраза должна содержать от 1 до 500 символов.")
            if normalized in seen:
                raise ValueError("В сценарии повторяются одинаковые голосовые фразы.")
            seen.add(normalized)
        if len({v.name for v in self.variables}) != len(self.variables):
            raise ValueError("Имена переменных должны различаться.")

        def visit(blocks, depth=0):
            if depth > 12:
                raise ValueError("Вложенность блоков превышает 12 уровней.")
            total = len(blocks)
            for block in blocks:
                total += visit(block.children, depth + 1) + visit(block.otherwise, depth + 1)
            return total

        if len({v.id for v in self.variants}) != len(self.variants):
            raise ValueError("Идентификаторы версий должны различаться.")
        if len({v.place_id for v in self.variants}) != len(self.variants):
            raise ValueError("Для одного места допускается одна версия команды.")
        if any(
            visit(actions) > 500 for actions in [self.actions, *(v.actions for v in self.variants)]
        ):
            raise ValueError("В одной версии сценария допускается до 500 блоков.")
        if contains_secret(self.model_dump_json()):
            raise ValueError("Не сохраняй ключи, пароли и токены в сценарии.")
        return self


def upgrade_document(data: dict[str, Any]) -> dict[str, Any]:
    """Decode an old stored document; never use this to relax validation of a new API save."""
    from copy import deepcopy

    if data.get("schema_version", 1) != 1:
        return data
    upgraded = deepcopy(data)
    common = upgraded.get("actions", [])
    if upgraded.get("version_policy", "append") == "append":
        # Materialize the former prefix once into independent local versions. The runner
        # then executes exactly one list. Both nested parameters and disabled steps survive.
        for version in upgraded.get("variants", []):
            version["actions"] = deepcopy(common) + version.get("actions", [])
    elif upgraded.get("general_enabled") is False:
        # Previously disabled General actions must not unexpectedly become live effects.
        # Their complete original document is retained in the migration backup/revisions.
        upgraded["actions"] = []
    upgraded.update(schema_version=2, version_policy="fallback", general_enabled=True)
    return upgraded
