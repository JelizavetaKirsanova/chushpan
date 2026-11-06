"""Private named colours, independent of peripheral availability and BLE state."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


def rgb_hex(rgb) -> str:
    if (
        not isinstance(rgb, (list, tuple))
        or len(rgb) != 3
        or any(type(v) is not int or not 0 <= v <= 255 for v in rgb)
    ):
        raise ValueError("RGB: нужны три целых числа от 0 до 255.")
    return "#" + "".join(f"{v:02X}" for v in rgb)


def hex_rgb(value: str) -> list[int]:
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("HEX: укажи # и шесть шестнадцатеричных символов.")
    return [int(value[i : i + 2], 16) for i in (1, 3, 5)]


class ColorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=80)
    rgb: list[int] = Field(min_length=3, max_length=3)
    favorite: bool = False

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        value = " ".join(value.split())
        if not value or any(ord(c) < 32 for c in value) or any(c in value for c in '«»";`'):
            raise ValueError("Укажи обычное название цвета длиной 1–80 символов.")
        return value

    @field_validator("rgb")
    @classmethod
    def valid_rgb(cls, value):
        rgb_hex(value)
        return value


class UserColor(ColorInput):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    revision: int = Field(ge=1)
    created_at: str
    updated_at: str

    @computed_field
    @property
    def hex(self) -> str:
        return rgb_hex(self.rgb)


def color_name(value):
    return value.casefold().replace("ё", "е")


class ColorLibrary:
    def __init__(self, memory):
        self.memory, self.db = memory, memory.db
        with memory.memory_transaction():
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS user_colors (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS device_color_preview (device_id TEXT PRIMARY KEY, hex TEXT NOT NULL)"
            )

    def list(self):
        return [
            UserColor.model_validate_json(row[0])
            for row in self.db.execute("SELECT data FROM user_colors ORDER BY rowid")
        ]

    def named(self, name):
        matches = [c for c in self.list() if color_name(c.name) == color_name(name)]
        if len(matches) != 1:
            raise ValueError("Такого пользовательского цвета нет. Сначала сохрани его в палитре.")
        return matches[0]

    def save(self, value, *, identifier=None, revision=0):
        self.memory._require_memory()
        item = ColorInput.model_validate(value)
        if type(revision) is not int:
            raise ValueError("Нужна актуальная версия цвета.")
        with self.memory.memory_transaction():
            items = self.list()
            existing = next((c for c in items if c.id == identifier), None)
            if (identifier and not existing) or revision != (existing.revision if existing else 0):
                raise ValueError("Цвет изменился или удалён в другом окне. Обнови палитру.")
            if any(
                c.id != identifier and color_name(c.name) == color_name(item.name) for c in items
            ):
                raise ValueError("Цвет с таким названием уже есть. Выбери другое название.")
            now = datetime.now(UTC).isoformat()
            saved = UserColor(
                **item.model_dump(),
                id=identifier or uuid4().hex,
                revision=revision + 1,
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
            self.db.execute(
                "INSERT INTO user_colors VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (saved.id, saved.model_dump_json(exclude={"hex"})),
            )
            return saved

    def delete(self, identifier, revision, *, confirmed=False):
        self.memory._require_memory()
        if confirmed is not True or type(revision) is not int:
            raise ValueError("Подтверди удаление выбранного цвета.")
        with self.memory.memory_transaction():
            existing = next((c for c in self.list() if c.id == identifier), None)
            if not existing or existing.revision != revision:
                raise ValueError("Цвет изменился или удалён в другом окне. Обнови палитру.")
            self.db.execute("DELETE FROM user_colors WHERE id=?", (identifier,))

    def preview(self, device_id):
        row = self.db.execute(
            "SELECT hex FROM device_color_preview WHERE device_id=?", (device_id,)
        ).fetchone()
        return row[0] if row else None

    def set_preview(self, device_id, value):
        self.memory._require_memory()
        canonical = rgb_hex(hex_rgb(value))
        with self.memory.memory_transaction():
            self.db.execute(
                "INSERT INTO device_color_preview VALUES (?,?) ON CONFLICT(device_id) DO UPDATE SET hex=excluded.hex",
                (device_id, canonical),
            )
        return canonical
