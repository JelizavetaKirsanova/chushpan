"""Validated, private lighting documents. No executable conditions or expressions."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.devices.colors import hex_rgb, rgb_hex


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LightAction(Document):
    kind: Literal["set", "preset", "fade", "effect", "stop", "status"] = "set"
    color: str | None = None
    brightness: int | None = Field(default=None, ge=0, le=100)
    power: bool | None = None
    preset_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    duration_s: int = Field(default=60, ge=2, le=86400)
    effect: Literal["cycle", "pulse", "breathing", "rainbow", "twinkle", "transition"] = "cycle"
    period_s: int = Field(default=20, ge=8, le=3600)
    colors: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("color")
    @classmethod
    def canonical(cls, value):
        return rgb_hex(hex_rgb(value)) if value is not None else None

    @field_validator("colors")
    @classmethod
    def palette(cls, values):
        return [rgb_hex(hex_rgb(v)) for v in values]

    @model_validator(mode="after")
    def valid(self):
        values = (self.color, self.brightness, self.power)
        if self.kind == "set" and all(v is None for v in values):
            raise ValueError("Укажи цвет, яркость или питание.")
        if self.kind == "fade" and self.color is None and self.brightness is None:
            raise ValueError("Для перехода нужен конечный цвет или яркость.")
        if self.kind == "preset" and not self.preset_id:
            raise ValueError("Выбери пресет.")
        if self.kind not in {"set", "fade"} and any(v is not None for v in values):
            raise ValueError("Параметры цвета, питания и яркости относятся к установке/переходу.")
        if (
            self.kind == "effect"
            and self.effect in {"cycle", "transition"}
            and len(self.colors) < 2
        ):
            raise ValueError("Для смены цветов выбери хотя бы два цвета.")
        return self


class Preset(Document):
    id: str = Field(default="", pattern=r"^(?:[a-f0-9]{32})?$")
    revision: int = Field(default=0, ge=0)
    name: str = Field(min_length=1, max_length=80)
    action: LightAction

    @model_validator(mode="after")
    def valid(self):
        self.name = " ".join(self.name.split())
        if not self.name or any(ord(c) < 32 for c in self.name):
            raise ValueError("Нужно обычное название пресета.")
        if self.action.kind != "set":
            raise ValueError("Пресет содержит только цвет, яркость и питание.")
        return self


class Rule(Document):
    kind: Literal["once", "interval", "weekly", "date", "sunrise", "sunset"] = "once"
    at: str = Field(default="23:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    weekdays: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1, max_length=7)
    timezone: str = "Europe/Tallinn"
    interval_s: int = Field(default=3600, ge=10, le=31536000)
    latitude: float | int | None = Field(default=None, ge=-90, le=90)
    longitude: float | int | None = Field(default=None, ge=-180, le=180)
    offset_minutes: int = Field(default=0, ge=-720, le=720)

    @model_validator(mode="after")
    def valid(self):
        ZoneInfo(self.timezone)
        if any(type(d) is not int or not 0 <= d <= 6 for d in self.weekdays):
            raise ValueError("Дни недели: 0–6.")
        if self.kind == "date" and not self.date:
            raise ValueError("Укажи дату.")
        if self.date:
            datetime.strptime(self.date, "%Y-%m-%d")
        if self.kind in {"sunrise", "sunset"} and (self.latitude is None or self.longitude is None):
            raise ValueError("Для восхода/заката введи координаты вручную.")
        return self


class LightJob(Document):
    id: str = Field(default="", pattern=r"^(?:[a-f0-9]{32})?$")
    revision: int = Field(default=0, ge=0)
    name: str = Field(min_length=1, max_length=100)
    targets: list[str] = Field(min_length=1, max_length=100)
    action: LightAction
    rule: Rule = Field(default_factory=Rule)
    next_at: str
    enabled: bool = True
    place_id: str = Field(default="general", pattern=r"^[a-z0-9_-]{1,64}$")
    conditions: dict[str, bool] = Field(default_factory=dict, max_length=20)
    catch_up_s: int = Field(default=900, ge=0, le=86400)
    status: str = "pending"
    progress: dict = Field(default_factory=dict)

    @field_validator("next_at")
    @classmethod
    def aware(cls, value):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("Время должно содержать часовой пояс.")
        return value

    @field_validator("targets")
    @classmethod
    def unique(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Лента повторяется в списке.")
        return values
