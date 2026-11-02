"""Exact local command routes; execution still passes through the guarded registry."""

import re

from backend.security import command_text


def route(text: str, *, control_session=None, binding=None, comfortable_brightness=None):
    if control_session is not None:
        plan = control_session.plan(text, binding, comfortable_brightness)
        if plan is not None:
            return "device_plan", plan
    from backend.led import parse as led_parse

    if led_parse(text) is not None:
        return "led_control", {"command": text}
    command = command_text(text).rstrip(".!? ")
    if re.fullmatch(r"(?:открой|запусти) (?:youtube|ютуб|ютьюб)", command):
        return "open_website", {"site": "youtube"}
    if re.fullmatch(r"который час|сколько времени|текущее время|скажи время", command):
        return "current_time", {}
    weather = re.fullmatch(
        r"какая (?:(?:сейчас|сегодня) )?погода(?: (?:сейчас|сегодня))?"
        r"(?: в ([\w-]+(?: [\w-]+){0,3}))?",
        command,
    )
    if weather and weather[1]:
        city = weather[1]
        # Do not reinterpret a forecast, a follow-up place, or a compound question.
        if city and set(city.split()) & {
            "завтра",
            "послезавтра",
            "сегодня",
            "сейчас",
            "неделю",
            "и",
            "а",
            "нем",
            "нём",
            "ней",
            "этом",
            "том",
            "моем",
            "моём",
            "другом",
            "вечером",
            "утром",
            "днем",
            "днём",
            "ночью",
            "неделе",
            "выходные",
        }:
            return None
        # Open-Meteo does not resolve the Russian locative from the observed request.
        if city in {"таллин", "таллинн", "таллине", "таллинне"}:
            city = "Tallinn"
        return "get_weather", {"days": 1, **({"city": city} if city else {})}
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
        r"(?:сделай|установи|поставь) яркость(?: mac| макбука)?(?: на)? (\d{1,3})"
        r"(?:\s*%| процентов)?",
        command,
    )
    if match and 0 <= int(match[1]) <= 100:
        return "set_brightness", {"level": int(match[1])}
    return None
