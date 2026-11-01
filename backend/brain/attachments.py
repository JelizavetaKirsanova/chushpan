"""An in-memory, validated image. Future mobile transports must issue trusted source IDs."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class ImageInput:
    data: bytes = field(repr=False)
    mime_type: str
    source: Literal["mac", "iphone", "ipad"] = "mac"
