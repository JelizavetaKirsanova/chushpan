"""Exact local command routes; execution still passes through the guarded registry."""

import re

from backend.security import command_text


def route(text: str) -> tuple[str, dict] | None:
    command = command_text(text).rstrip(".!? ")
    if re.fullmatch(r"который час|сколько времени|текущее время|скажи время", command):
        return "current_time", {}
    if re.fullmatch(r"(?:какая|покажи) (?:системная )?громкость(?: mac| мака)?", command):
        return "get_system_status", {"topic": "volume"}
    if re.fullmatch(r"(?:какая|покажи) яркость(?: mac| макбука)?", command):
        return "get_brightness", {}
    match = re.fullmatch(
        r"(?:сделай|поставь) (?:системную )?громкость(?: mac| мака)?"
        r"(?: на)? (\d{1,3})(?:\s*%| процентов)?",
        command,
    )
    if match and 0 <= int(match[1]) <= 100:
        return "set_volume", {"level": int(match[1])}
    match = re.fullmatch(
        r"(?:сделай|установи) яркость(?: mac| макбука)?(?: на)? (\d{1,3})(?:\s*%| процентов)?",
        command,
    )
    if match and 0 <= int(match[1]) <= 100:
        return "set_brightness", {"level": int(match[1])}
    return None
