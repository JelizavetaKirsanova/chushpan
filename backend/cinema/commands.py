"""Exact Russian cinema routes. Ambiguous controls require a live cinema session."""

import re

from backend.security import command_text


def command(text, active=False):
    value = command_text(text).strip(".!? ")
    if any(char in value for char in ('"', "«", "»", ";", "\n")) or re.search(r"\bне\b", value):
        return None
    tv = bool(re.search(r"(?: и (?:включи|покажи))? на телевизоре$", value))
    if tv:
        value = re.sub(r"(?: и (?:включи|покажи))? на телевизоре$", "", value)
    action = None
    add = re.fullmatch(r"добавь (.{1,200}?) в (?:список )?(?:хочу посмотреть|на просмотр)", value)
    if add and not tv:
        return {"action": "plan", "title": add[1]}
    discover = re.fullmatch(
        r"найди (?:мне )?(?:какой[ -]нибудь )?"
        r"(триллер|комедию|драму|ужасы|фантастику)",
        value,
    )
    if discover and not tv:
        return {
            "action": "discover",
            "genre": {"комедию": "комедия", "драму": "драма", "фантастику": "фантастика"}.get(
                discover[1], discover[1]
            ),
        }
    if (
        value in {"что у меня в списке на просмотр", "что у меня в списке хочу посмотреть"}
        and not tv
    ):
        return {"action": "planned"}
    if re.fullmatch(
        r"(?:продолжи|включи) (?:мой |мою |мне )?(?:сериал|фильм|кино|просмотр)|"
        r"продолжи (?:то,? что я смотрела|последний сериал)|"
        r"включи последнюю серию,? которую я смотрела",
        value,
    ):
        action = "continue"
    elif re.fullmatch(r"открой (?:мою )?(?:библиотеку|кинотеку)|открой кино", value):
        action = "library"
    elif re.fullmatch(r"(?:включи|открой) следующую серию", value):
        action = "next"
    elif re.fullmatch(
        r"(?:поставь (?:фильм|сериал|кино) на паузу|останови (?:фильм|сериал))", value
    ):
        action = "pause"
    elif value in {"какая это серия", "сколько осталось до конца"}:
        action = "info"
    elif re.fullmatch(r"отметь (?:эту )?серию просмотренной", value):
        action = "watched"
    elif re.fullmatch(r"отметь (?:этот )?фильм просмотренным", value):
        action = "title_watched"
    elif active and value in {"продолжай", "продолжи", "пауза", "поставь на паузу"}:
        action = (
            "play"
            if value in {"продолжай", "продолжи"}
            else "tv"
            if value == "включи это" and tv
            else "pause"
        )
    elif active and value == "включи это" and tv:
        action = "tv"
    if tv and action not in {"continue", "next", "tv"}:
        return None
    if action:
        return {"action": action, **({"device": "airplay"} if tv else {})}
    if active:
        match = re.fullmatch(
            r"(?:перемотай|верни) (?:на )?"
            r"(\d{1,5}|пять|пяти|тридцать|тридцати|одну|две|десять) "
            r"(минут(?:у|ы)?|секунд(?:у|ы)?)( назад| впер[её]д)?",
            value,
        )
        if match:
            words = {
                "пять": 5,
                "пяти": 5,
                "тридцать": 30,
                "тридцати": 30,
                "одну": 1,
                "две": 2,
                "десять": 10,
            }
            amount = int(match[1]) if match[1].isdigit() else words[match[1]]
            seconds = amount * (60 if match[2].startswith("минут") else 1)
            if seconds <= 86400:
                return {"action": "seek", "seconds": -seconds if match[3] == " назад" else seconds}
    return None
