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
    if not tv:
        trailer = re.fullmatch(
            r"(?:смотреть|посмотри|покажи|открой|включи) (?:официальный )?трейлер"
            r"(?: (?:фильма|сериала))?(?: (.{1,200}))?",
            value,
        )
        if trailer:
            return {"action": "trailer", "title": trailer[1] or ""}
        watch = re.fullmatch(
            r"(?:смотреть|посмотри|включи|открой) (фильм|сериал)(?: (.{1,200}))?", value
        )
        if watch:
            kind = "movie" if watch[1] == "фильм" else "series"
            return (
                {"action": "find_sources", "title": watch[2], "kind": kind, "watch": True}
                if watch[2]
                else {"action": "continue", "kind": kind, "focused_watch": True}
            )
        if value in {"добавь этот фильм в избранное", "добавь этот сериал в избранное"}:
            return {"action": "favorite"}
        rating = re.fullmatch(
            r"поставь (?:этому фильму|этому сериалу) "
            r"(10|[1-9](?:[.,]5)?|один|два|три|четыре|пять|шесть|семь|восемь|"
            r"девять|десять)( с половиной)?",
            value,
        )
        if rating:
            words = {
                word: i
                for i, word in enumerate(
                    "один два три четыре пять шесть семь восемь девять десять".split(), 1
                )
            }
            number = words.get(rating[1]) or float(rating[1].replace(",", "."))
            number += 0.5 if rating[2] else 0
            return {"action": "rate", "rating": number} if number <= 10 else None
        find = re.fullmatch(r"найди (?:мне )?где посмотреть (.{1,200})", value)
        if find:
            return {"action": "find_sources", "title": find[1]}
        if value in {"покажи мой кинопрофиль", "открой мой кинопрофиль", "покажи кинопрофиль"}:
            return {"action": "profile"}
        if value in {
            "найди другой сайт для этого сериала",
            "найди другой источник",
            "смени источник просмотра",
        }:
            return {"action": "alternative"}
        if value in {
            "что мне сегодня посмотреть",
            "что сегодня посмотреть",
            "найди что-нибудь похожее на мои любимые фильмы",
            "найди что нибудь похожее на мои любимые фильмы",
        }:
            return {"action": "recommend", "mood": "any"}
        recommend = re.fullmatch(
            r"посоветуй (?:мне )?(?:хороший |хорошую |интересный )?"
            r"(триллер|комедию|драму|ужасы|фантастику)",
            value,
        )
        if recommend:
            return {
                "action": "recommend",
                "mood": "tense"
                if recommend[1] in {"триллер", "ужасы"}
                else "calm"
                if recommend[1] == "комедию"
                else "any",
                "genre": {"комедию": "комедия", "драму": "драма", "фантастику": "фантастика"}.get(
                    recommend[1], recommend[1]
                ),
            }
        episode = re.fullmatch(
            r"(?:включи|открой) (\d{1,4}|первую|вторую|третью|четвертую|четвёртую|"
            r"пятую|шестую|седьмую|восьмую|девятую|десятую) "
            r"серию (\d{1,3}|первого|второго|третьего|четвертого|четвёртого|"
            r"пятого|шестого|седьмого|восьмого|девятого|десятого) сезона",
            value,
        )
        if episode:
            ordinal = {
                word: number
                for number, words in enumerate(
                    [
                        ("первую", "первого"),
                        ("вторую", "второго"),
                        ("третью", "третьего"),
                        ("четвертую", "четвёртую", "четвертого", "четвёртого"),
                        ("пятую", "пятого"),
                        ("шестую", "шестого"),
                        ("седьмую", "седьмого"),
                        ("восьмую", "восьмого"),
                        ("девятую", "девятого"),
                        ("десятую", "десятого"),
                    ],
                    1,
                )
                for word in words
            }
            return {
                "action": "episode",
                "episode": int(episode[1]) if episode[1].isdigit() else ordinal[episode[1]],
                "season": int(episode[2]) if episode[2].isdigit() else ordinal[episode[2]],
            }
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
