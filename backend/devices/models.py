"""Local device metadata. Endpoints are identifiers, never credentials or URLs."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Device(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-z0-9-]{1,64}$")
    revision: int = Field(default=0, ge=0)
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["led_strip", "mac", "external_display"]
    protocol: Literal["ble", "local", "betterdisplay"]
    endpoint: str = Field(max_length=64)
    place_id: str = Field(default="general", max_length=64)
    room: str = Field(default="", max_length=60)
    state: dict = Field(default_factory=dict)
    sent: dict = Field(default_factory=dict)
    last_contact: str | None = None
    state_at: str | None = None
    result_status: str | None = None
    favorites: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def identity(self):
        expected = {"led_strip": "ble", "mac": "local", "external_display": "betterdisplay"}
        if self.protocol != expected[self.kind]:
            raise ValueError("Протокол не соответствует типу устройства.")
        if self.kind == "mac":
            if self.endpoint != "local-mac":
                raise ValueError("Поддерживается только этот Mac.")
        else:
            self.endpoint = str(UUID(self.endpoint))
        self.name, self.room = self.name.strip(), self.room.strip()
        if not self.name or any(ord(c) < 32 for c in self.name + self.room):
            raise ValueError("Название и комната должны содержать обычный текст.")
        return self
