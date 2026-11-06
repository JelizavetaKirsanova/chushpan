"""Local device metadata. Endpoints are identifiers, never credentials or URLs."""

from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Device(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-z0-9-]{1,64}$")
    revision: int = Field(default=0, ge=0)
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["led_strip", "mac", "external_display", "airplay", "audio_output", "wled"]
    protocol: Literal["ble", "local", "betterdisplay", "bonjour", "coreaudio", "http"]
    endpoint: str = Field(max_length=240)
    place_id: str = Field(default="general", max_length=64)
    room: str = Field(default="", max_length=60)
    state: dict = Field(default_factory=dict)
    sent: dict = Field(default_factory=dict)
    last_contact: str | None = None
    state_at: str | None = None
    result_status: str | None = None
    favorites: list[str] = Field(default_factory=list, max_length=16)

    aliases: list[str] = Field(default_factory=list, max_length=16)
    favorite: bool = False
    auto_connect: bool = False
    ble_name: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def identity(self):
        expected = {
            "led_strip": "ble",
            "mac": "local",
            "external_display": "betterdisplay",
            "airplay": "bonjour",
            "audio_output": "coreaudio",
            "wled": "http",
        }
        if self.protocol != expected[self.kind]:
            raise ValueError("Протокол не соответствует типу устройства.")
        if self.kind == "mac":
            if self.endpoint != "local-mac":
                raise ValueError("Поддерживается только этот Mac.")
        elif self.kind in {"led_strip", "external_display"}:
            self.endpoint = str(UUID(self.endpoint))
        elif self.kind == "wled":
            from backend.devices.adapters import local_ip

            self.endpoint = local_ip(self.endpoint)
        elif not self.endpoint.strip() or any(ord(c) < 32 for c in self.endpoint):
            raise ValueError("Нужен корректный идентификатор устройства.")
        self.aliases = list(dict.fromkeys(a.strip().casefold() for a in self.aliases))
        if any(not a or len(a) > 60 or any(ord(c) < 32 for c in a) for a in self.aliases):
            raise ValueError("Синоним должен содержать от 1 до 60 обычных символов.")
        self.name, self.room = self.name.strip(), self.room.strip()
        if not self.name or any(ord(c) < 32 for c in self.name + self.room):
            raise ValueError("Название и комната должны содержать обычный текст.")
        return self


class DeviceCapability(BaseModel):
    action: str
    supported: bool = False
    confirmed: bool = False


class DeviceConnection(BaseModel):
    status: Literal["unknown", "observed", "disconnected", "error"] = "unknown"
    checked_at: str | None = None


class Room(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-z0-9-]{1,64}$")
    revision: int = Field(default=0, ge=0)
    name: str = Field(min_length=1, max_length=60)
    place_id: str = Field(default="general", max_length=64)


class DeviceGroup(Room):
    room: str = Field(default="", max_length=60)
    device_ids: list[str] = Field(default_factory=list, max_length=100)


class DeviceCommand(BaseModel):
    device_id: str
    action: str
    parameters: dict = Field(default_factory=dict)
    result_status: str = "unverified"
    elapsed_ms: int = 0


class DeviceAdapter(Protocol):
    async def inspect(self, device: Device) -> dict: ...
    async def execute(self, device: Device, action: str, parameters: dict) -> dict: ...
