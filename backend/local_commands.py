"""Exact local command routes; execution still passes through the guarded registry."""

import re

from backend.security import command_text, local_app_name


def route(
    text: str, *, control_session=None, binding=None, comfortable_brightness=None, cinema=None
):
    if cinema is not None and (cinema_route := cinema.route(text)):
        return cinema_route
    if control_session is not None:
        plan = control_session.plan(text, binding, comfortable_brightness)
        if plan is not None:
            return "device_plan", plan
    from backend.led import _NUMBERS
    from backend.led import parse as led_parse

    if led_parse(text) is not None:
        return "led_control", {"command": text}
    command = command_text(text).rstrip(".!? ")
    # Remove grammatical fillers; recognize a read intent with complete coverage,
    # regardless of whether the date precedes or follows the noun.
    calendar_words = re.sub(r"[,!?]", " ", command)
    calendar_words = re.sub(r"\b(?:у меня|мне|там|на|в|блять|нахуй)\b", " ", calendar_words)
    calendar_words = " ".join(calendar_words.split())
    calendar_words = re.sub(r"\b(?:сегодня|завтра|\d{4}-\d{2}-\d{2})\b", " ", calendar_words)
    calendar_words = " ".join(calendar_words.split())
    if not re.search(r'[«»"“”`\n;]', text) and (
        re.fullmatch(
            r"(?:покажи|посмотри|проверь|прочитай) календар[ье]",
            calendar_words,
        )
        or re.fullmatch(r"(?:какие|что) (?:планы|события)", calendar_words)
    ):
        day = "tomorrow" if "завтра" in command else "today"
        if date := re.search(r"\b\d{4}-\d{2}-\d{2}\b", command):
            day = date[0]
        return "get_calendar", {"day": day}
    if re.fullmatch(r"(?:открой|запусти) (?:youtube|ютуб|ютьюб)", command):
        return "open_website", {"site": "youtube"}
    app = re.fullmatch(r"(?:открой|запусти)(?: приложение)? ([\w][\w .-]{0,79})", command)
    if app:
        # Exact requested name only. Existing registry checks still validate the
        # application, caller, permissions and original explicit request.
        if name := local_app_name(app[1]):
            return "open_app", {"name": name}
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
        r"(?:(?:сделай|установи|поставь) )?(?:системную )?громкость(?: mac| мака)?"
        r"(?: на)? (\d{1,3}|[а-яё]+(?: [а-яё]+){0,2})(?:\s*%| процент(?:ов|а)?)?",
        command,
    )
    if match:
        amount = re.sub(r"\s+процент(?:ов|а)?$", "", match[1])
        level = int(amount) if amount.isdigit() else _NUMBERS.get(amount)
        if level is not None and 0 <= level <= 100:
            return "set_volume", {"level": level}
    quieter = re.fullmatch(
        r"(?:(?:сделай )?(?:звук|громкость)(?: mac| мака)? (?:по)?тише|"
        r"(?:убавь|уменьши|снизь) (?:звук|громкость)(?: mac| мака)?)",
        command,
    )
    louder = re.fullmatch(
        r"(?:(?:сделай )?(?:звук|громкость)(?: mac| мака)? (?:по)?громче|"
        r"(?:прибавь|увеличь|повысь) (?:звук|громкость)(?: mac| мака)?)",
        command,
    )
    if quieter or louder:
        return "set_volume", {"direction": "down" if quieter else "up"}
    match = re.fullmatch(
        r"(?:(?:сделай|установи|поставь) )?яркость(?: mac| macbook| макбука| экрана)"
        r"(?: на)? (\d{1,3})"
        r"(?:\s*%| процентов)?",
        command,
    )
    if match and 0 <= int(match[1]) <= 100:
        return "set_brightness", {"level": int(match[1])}
    return None
