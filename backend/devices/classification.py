"""Conservative device classification from BLE advertisement evidence."""

import re
from dataclasses import dataclass

ELK_FFF0 = "0000fff0-0000-1000-8000-00805f9b34fb"


@dataclass(frozen=True)
class Classification:
    kind: str
    confidence: float
    evidence: tuple[str, ...]
    reason: str


def classify_ble(name: str, service_uuids=(), manufacturer_data=None) -> Classification:
    """Require independent evidence before calling a BLE peripheral an LED controller."""
    normalized = (name or "").strip().upper()
    services = {str(value).lower() for value in service_uuids or ()}
    manufacturer_data = manufacturer_data or {}
    evidence = []
    score = 0.0
    if re.fullmatch(r"ELK-BLEDDM(?:\s+.*)?", normalized):
        score += 0.65
        evidence.append("exact_elk_bleddm_name")
    elif re.fullmatch(r"ELK-BLEDOB(?:\s+.*)?", normalized):
        score += 0.65
        evidence.append("exact_elk_bledob_name")
    elif re.match(r"^ELK-BLE", normalized):
        score += 0.35
        evidence.append("elk_family_name")
    if ELK_FFF0 in services:
        score += 0.25
        evidence.append("elk_service_uuid")
    # No manufacturer identifiers are treated as confirmed until backed by a driver profile.
    if manufacturer_data:
        evidence.append("manufacturer_data_present_unverified")
    confidence = min(score, 1.0)
    if confidence >= 0.8:
        return Classification(
            "led_strip",
            confidence,
            tuple(evidence),
            "Подтверждены имя ELK и профильный Service UUID.",
        )
    return Classification(
        "unknown",
        confidence,
        tuple(evidence),
        "Недостаточно независимых признаков для определения типа; управление недоступно.",
    )
