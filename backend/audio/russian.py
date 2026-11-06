"""Deterministic, offline Russian speech copy; never mutate the displayed response."""

from __future__ import annotations

import re
import unicodedata
from datetime import date

DEFAULT_PRONUNCIATIONS = {
    "Чушпан": "Чу́шпан",
    "Chushpan": "Чу́шпан",
    "YouTube Sleep Timer": "Ютуб слип таймер",
    "Youtube sleep": "Ютуб слип",
    "Apple Shortcuts": "Эпл шорткатс",
    "MacBook Pro": "Макбук Про",
    "YouTube": "Ютуб",
    "MacBook": "Макбук",
    "Safari": "Сафари",
    "Gemini": "Джемини",
    "Ollama": "Оллама",
    "Python": "Пайтон",
    "JavaScript": "Джаваскрипт",
    "GitHub": "Гитхаб",
    "Cursor": "Кёрсор",
    "BetterDisplay": "Беттер Дисплей",
    "Flash": "Флэш",
    "Silero": "Силеро",
    "Piper": "Пайпер",
    "macOS": "мак ос",
    "Mac": "Мак",
    "Apple": "Эпл",
    "Shortcuts": "Шорткатс",
    "Codex": "Кодекс",
    "Terminal": "Терминал",
    "API": "эй пи ай",
    "TTS": "ти ти эс",
    "CPU": "си пи ю",
    "GPU": "джи пи ю",
    "SQLite": "эс кью лайт",
    "Whisper": "Виспер",
    "Swift": "Свифт",
}
ONES = "ноль один два три четыре пять шесть семь восемь девять".split()
TEENS = (
    "десять одиннадцать двенадцать тринадцать четырнадцать пятнадцать "
    "шестнадцать семнадцать восемнадцать девятнадцать"
).split()
TENS = (
    "ноль десять двадцать тридцать сорок пятьдесят шестьдесят семьдесят восемьдесят девяносто"
).split()
HUNDREDS = "ноль сто двести триста четыреста пятьсот шестьсот семьсот восемьсот девятьсот".split()
SCALES = [
    ("тысяча", "тысячи", "тысяч"),
    ("миллион", "миллиона", "миллионов"),
    ("миллиард", "миллиарда", "миллиардов"),
    ("триллион", "триллиона", "триллионов"),
]
# Secrets and executable/structured content are omitted from speech, not rewritten in UI.
PROTECTED = re.compile(
    r"```[\s\S]*?(?:```|\Z)|`[^`]*(?:`|\Z)|https?://[^\s<>]+|www\.[^\s<>]+"
    r"|\b[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    r"|(?<![\w/])(?:~/|/|[A-Za-z]:\\|\./|\.\./)[^\s,;]+"
    r"|\b(?:sk-|AIza)[A-Za-z0-9_-]+|\b[A-Za-z0-9_-]{24,}\b"
    r"|\b(?!AC/DC\b)[A-Za-z_][\w.-]*(?:/[\w.-]+)+\b"
    r"|\b(?!(?:Next\.js|Node\.js)\b)[A-Za-z_][\w.-]*\.(?:py|js|ts|json|yaml|yml|env|txt|swift|sh)\b"
    r"|\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+",
    re.I,
)
NUMBER = r"[-−+]?\d+(?:[.,]\d+)?"
UNITS = {
    "%": ("процент", "процента", "процентов", "m"),
    "мин": ("минута", "минуты", "минут", "f"),
    "ч": ("час", "часа", "часов", "m"),
    "с": ("секунда", "секунды", "секунд", "f"),
    "кг": ("килограмм", "килограмма", "килограммов", "m"),
    "г": ("грамм", "грамма", "граммов", "m"),
    "км": ("километр", "километра", "километров", "m"),
    "м": ("метр", "метра", "метров", "m"),
    "см": ("сантиметр", "сантиметра", "сантиметров", "m"),
    "мм": ("миллиметр", "миллиметра", "миллиметров", "m"),
    "л": ("литр", "литра", "литров", "m"),
    "гб": ("гигабайт", "гигабайта", "гигабайтов", "m"),
    "мб": ("мегабайт", "мегабайта", "мегабайтов", "m"),
    "°c": ("градус Цельсия", "градуса Цельсия", "градусов Цельсия", "m"),
    "°с": ("градус Цельсия", "градуса Цельсия", "градусов Цельсия", "m"),
    "км/ч": ("километр в час", "километра в час", "километров в час", "m"),
}

for alias, key in {
    "kg": "кг",
    "cm": "см",
    "mm": "мм",
    "km": "км",
    "km/h": "км/ч",
    "gb": "гб",
    "mb": "мб",
}.items():
    UNITS[alias] = UNITS[key]


def form(n: int, words) -> str:
    n = abs(n)
    return words[2 if 11 <= n % 100 <= 14 else 0 if n % 10 == 1 else 1 if 2 <= n % 10 <= 4 else 2]


def integer(n: int, gender: str = "m") -> str:
    if n < 0:
        return "минус " + integer(-n, gender)
    if n == 0:
        return ONES[0]
    if n >= 10**15:
        return " ".join(ONES[int(c)] for c in str(n))
    parts = []
    for power in range(4, 0, -1):
        scale = 1000**power
        count, n = divmod(n, scale)
        if count:
            parts += [integer(count, "f" if power == 1 else "m"), form(count, SCALES[power - 1])]
    if n >= 100:
        parts.append(HUNDREDS[n // 100])
        n %= 100
    if 10 <= n < 20:
        parts.append(TEENS[n - 10])
    else:
        if n >= 20:
            parts.append(TENS[n // 10])
        n %= 10
        if n:
            parts.append(
                "одна"
                if n == 1 and gender == "f"
                else "одно"
                if n == 1 and gender == "n"
                else "две"
                if n == 2 and gender == "f"
                else ONES[n]
            )
    return " ".join(parts)


def number(value: str, gender: str = "m") -> str:
    sign = "минус " if value.startswith(("-", "−")) else "плюс " if value.startswith("+") else ""
    value = value.lstrip("-−+")
    if "." not in value and "," not in value:
        return sign + integer(int(value), gender)
    whole, fraction = re.split(r"[.,]", value)
    denominators = ["десятых", "сотых", "тысячных", "десятитысячных", "стотысячных", "миллионных"]
    if len(fraction) > len(denominators):
        return sign + integer(int(whole)) + " запятая " + " ".join(ONES[int(c)] for c in fraction)
    ending = denominators[len(fraction) - 1]
    if int(fraction) % 10 == 1 and int(fraction) % 100 != 11:
        ending = ending[:-2] + "ая"
    return (
        sign
        + integer(int(whole), "f")
        + " "
        + form(int(whole), ("целая", "целых", "целых"))
        + " "
        + integer(int(fraction), "f")
        + " "
        + ending
    )


def _date(m: re.Match) -> str:
    d, month, year = map(int, m.groups())
    try:
        date(year, month, d)
    except ValueError:
        return m[0]
    days = (
        "первое второе третье четвёртое пятое шестое седьмое восьмое "
        "девятое десятое одиннадцатое двенадцатое тринадцатое "
        "четырнадцатое пятнадцатое шестнадцатое семнадцатое восемнадцатое "
        "девятнадцатое двадцатое"
    ).split()
    day = (
        days[d - 1]
        if d <= 20
        else "тридцатое"
        if d == 30
        else ("двадцать " if d < 30 else "тридцать ") + days[d % 10 - 1]
    )
    months = (
        "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
    )
    # Cardinal year is unambiguous without pretending to implement full Russian morphology.
    return f"{day} {months[month - 1]}, год {integer(year)}"


def normalize(
    text: str,
    pronunciations: dict[str, str] | None = None,
    *,
    diagnostics=None,
    numbers=True,
    foreign=True,
) -> str:
    """Prepare a speech copy; literal content stays in the original UI/history only."""
    text = unicodedata.normalize("NFC", text)
    from backend.audio.abbreviations import expand, protected_compound

    def protect(match):
        if match[0] == "km/h":
            return match[0]
        if compound := protected_compound(match[0]):
            return compound
        if diagnostics is not None:
            diagnostics.append({"kind": "protected_literal", "reason": "not_spoken"})
        return " фрагмент в тексте "

    text = PROTECTED.sub(protect, text)
    if numbers:
        # Grouping spaces are accepted only in complete three-digit groups.
        text = re.sub(
            r"(?<!\w)\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?!\d)",
            lambda m: re.sub(r"\s", "", m[0]),
            text,
        )

        text = re.sub(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", _date, text)
        text = re.sub(
            r"\b(\d{4})-(\d{2})-(\d{2})\b",
            lambda m: _date(re.match(r"(\d+)/(\d+)/(\d+)", f"{m[3]}/{m[2]}/{m[1]}")),
            text,
        )

        def clock(m):
            h, minute = int(m[1]), int(m[2])
            if h > 23 or minute > 59 or (m[3] and int(m[3][1:]) > 59):
                return m[0]
            result = integer(h) + " " + form(h, ("час", "часа", "часов"))
            result += " " + integer(minute, "f") + " " + form(minute, ("минута", "минуты", "минут"))
            if m[3]:
                second = int(m[3][1:])
                result += (
                    " "
                    + integer(second, "f")
                    + " "
                    + form(second, ("секунда", "секунды", "секунд"))
                )
            return result

        text = re.sub(r"\b(\d{1,2}):(\d{2})(:\d{2})?\b", clock, text)
        # Versions are recognized before decimals and before replacing product names.
        text = re.sub(
            r"\b([A-Za-z][A-Za-z ]*|версия|версии)\s+(\d+(?:\.\d+)+)\b",
            lambda m: m[1] + " " + " точка ".join(integer(int(n)) for n in m[2].split(".")),
            text,
        )
        text = re.sub(
            r"(?<!\w)([-−]?\d+)/(\d+)(?!\w)",
            lambda m: number(m[1]) + " делённое на " + integer(int(m[2])),
            text,
        )
    from backend.audio.foreign import NAMES

    dictionary = {
        k.casefold(): v for k, v in {**DEFAULT_PRONUNCIATIONS, **(NAMES if foreign else {})}.items()
    }
    dictionary.update({k.casefold(): v for k, v in (pronunciations or {}).items() if k})
    pattern = (
        r"(?<!\w)(?:"
        + "|".join(re.escape(k) for k in sorted(dictionary, key=len, reverse=True))
        + r")(?!\w)"
    )
    text = re.sub(pattern, lambda m: dictionary[m[0].casefold()], text, flags=re.I)
    text = expand(text.replace("‑", "-").replace("‐", "-"), pronunciations)

    def unit(m):
        value, key = m[1], m[2].casefold()
        words = UNITS[key]
        label = words[1] if re.search(r"[.,]", value) else form(int(value.replace("−", "-")), words)
        return number(value, words[3]) + " " + label

    units = "|".join(re.escape(k) for k in sorted(UNITS, key=len, reverse=True))
    if numbers:
        text = re.sub(rf"(?<!\w)({NUMBER})\s*({units})(?!\w)", unit, text, flags=re.I)

        def contextual(m):
            after = text[m.end() :].lstrip().casefold()
            gender = (
                "n"
                if re.match(r"видео\b|окно\b|сообщени", after)
                else "f"
                if re.match(r"минут|секунд|часовн|страниц|верси|песн|задач|попыт|ошибк", after)
                else "m"
            )
            return number(m[0], gender)

        text = re.sub(rf"(?<!\w){NUMBER}(?!\w)", contextual, text)
    from backend.audio.foreign import prepare_foreign

    if foreign:
        text = prepare_foreign(text, pronunciations, diagnostics=diagnostics)
    return re.sub(r"\s+", " ", text).strip()
