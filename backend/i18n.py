"""Per-language strings, date formatting, default titles and voices."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime


def noun_form(value: float, forms: tuple[str, ...]) -> str:
    """Choose a singular/plural form, including Russian one/few/many agreement."""
    n = abs(value)
    if len(forms) == 2:
        return forms[0] if n == 1 else forms[1]
    if n != int(n):
        return forms[1]
    n = int(n)
    if 11 <= n % 100 <= 14:
        return forms[2]
    if n % 10 == 1:
        return forms[0]
    return forms[1] if 2 <= n % 10 <= 4 else forms[2]


@dataclass(frozen=True)
class Locale:
    code: str  # e.g. pt-BR
    lang: str  # e.g. pt
    language_name: str
    weekdays: tuple[str, ...]
    months: tuple[str, ...]
    seasons: dict[str, str]
    periods: dict[str, str]
    greetings: dict[str, str]  # keyed by period
    titles: dict[str, str]  # keyed by gender kind: f | m
    persona: str
    voice: str  # default Piper voice
    you_label: str
    farewell: str
    stt_hint: str  # primes Whisper with the assistant's name and conversational style
    native_prompt: bool = True  # False: English prompt + reply-in-language rule
    # Spoken durations and timer announcements
    units: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "h": ("hour", "hours"),
            "m": ("minute", "minutes"),
            "s": ("second", "seconds"),
        }
    )
    and_word: str = "and"
    timer_done: str = "Excuse me{addr}, your {duration} timer is up."
    timer_done_label: str = "Excuse me{addr}, your {duration} timer is up: {label}."
    one_moment: str = "One moment."  # while a slow tool runs
    no_reply: str = "I couldn't produce a reply. Please ask again."
    # Said by code when the person ends the conversation ("that's all, you can rest"):
    # the model echoed it ("Pode descansar, senhora... Disponha") (D-43).
    sign_off: str = "Very good{addr}. I shall be here."
    sign_off_thanked: str = "You're welcome{addr}. I shall be here."
    welcome: str = "You're welcome{addr}."  # a lone thank-you; the model added "Disponha"

    def format_datetime(self, now: datetime) -> str:
        wd, month = self.weekdays[now.weekday()], self.months[now.month - 1]
        if self.lang == "pt":
            return f"{wd}, {now.day} de {month} de {now.year}, {now:%H:%M}"
        if self.lang == "ru":
            return f"{wd}, {now.day} {month} {now.year} года, {now:%H:%M}"
        return f"{wd}, {now.day} {month} {now.year}, {now:%H:%M}"

    def full_address(self, title: str | None, name: str | None) -> str | None:
        if not title:
            return name
        if not name or title == name:
            return title
        return f"{title} {name}" if self.lang in ("pt", "ru") else title

    def format_duration(self, seconds: float) -> str:
        """3725 → "1 hour, 2 minutes and 5 seconds", in words a voice can read."""
        total = max(0, round(seconds))
        h, rest = divmod(total, 3600)
        m, s = divmod(rest, 60)
        parts = [
            f"{n} {noun_form(n, self.units[k])}" for n, k in ((h, "h"), (m, "m"), (s, "s")) if n
        ] or [f"0 {noun_form(0, self.units['s'])}"]
        if len(parts) == 1:
            return parts[0]
        return f"{', '.join(parts[:-1])} {self.and_word} {parts[-1]}"


PT_BR = Locale(
    code="pt-BR",
    lang="pt",
    language_name="português do Brasil",
    weekdays=(
        "segunda-feira",
        "terça-feira",
        "quarta-feira",
        "quinta-feira",
        "sexta-feira",
        "sábado",
        "domingo",
    ),
    months=(
        "janeiro",
        "fevereiro",
        "março",
        "abril",
        "maio",
        "junho",
        "julho",
        "agosto",
        "setembro",
        "outubro",
        "novembro",
        "dezembro",
    ),
    seasons={"spring": "primavera", "summer": "verão", "autumn": "outono", "winter": "inverno"},
    periods={"dawn": "madrugada", "morning": "manhã", "afternoon": "tarde", "night": "noite"},
    greetings={
        "dawn": "boa noite",
        "morning": "bom dia",
        "afternoon": "boa tarde",
        "night": "boa noite",
    },
    titles={"f": "senhora", "m": "senhor"},
    persona=(
        "mordomo britânico: formal, educado e impecável, com humor seco e sutil, "
        "usado com moderação; nunca bajulador"
    ),
    voice="pt_BR-faber-medium",
    you_label="Você",
    farewell="Às suas ordens{addr}. Até breve.",
    stt_hint="Olá, {name}. Como está o tempo hoje? Quais são as notícias?",
    units={"h": ("hora", "horas"), "m": ("minuto", "minutos"), "s": ("segundo", "segundos")},
    and_word="e",
    timer_done="Com licença{addr}, o timer de {duration} terminou.",
    timer_done_label="Com licença{addr}, o timer de {duration} terminou: {label}.",
    one_moment="Um momento.",
    no_reply="Não consegui formular uma resposta. Pode repetir?",
    sign_off="Pois não{addr}. Estarei por aqui.",
    sign_off_thanked="Por nada{addr}. Estarei por aqui.",
    welcome="Por nada{addr}.",
)

EN = Locale(
    code="en-GB",
    lang="en",
    language_name="British English",
    weekdays=("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    months=(
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ),
    seasons={"spring": "spring", "summer": "summer", "autumn": "autumn", "winter": "winter"},
    periods={
        "dawn": "small hours",
        "morning": "morning",
        "afternoon": "afternoon",
        "night": "evening",
    },
    greetings={
        "dawn": "good evening",
        "morning": "good morning",
        "afternoon": "good afternoon",
        "night": "good evening",
    },
    titles={"f": "madam", "m": "sir"},
    persona=(
        "a British butler: formal, polite and impeccable, with a dry, subtle wit "
        "used sparingly; never sycophantic"
    ),
    voice="en_GB-alan-medium",
    you_label="You",
    farewell="At your service{addr}. Until next time.",
    stt_hint="Hello, {name}. What's the weather like today? Any news?",
)

RU = Locale(
    code="ru-RU",
    lang="ru",
    language_name="русский",
    weekdays=("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"),
    months=(
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    ),
    seasons={"spring": "весна", "summer": "лето", "autumn": "осень", "winter": "зима"},
    periods={"dawn": "ночь", "morning": "утро", "afternoon": "день", "night": "вечер"},
    greetings={
        "dawn": "доброй ночи",
        "morning": "доброе утро",
        "afternoon": "добрый день",
        "night": "добрый вечер",
    },
    titles={},
    persona=(
        "дерзкий саркастичный знакомый мужик лет тридцати, немного задолбавшийся; "
        "живой разговорный русский, сухие подколы, уместный редкий мат; "
        "обращается к пользовательнице на «ты», в женском роде, о себе в мужском; "
        "без лести и унижения; в тяжёлых и опасных ситуациях серьёзен"
    ),
    voice="ru_RU-dmitri-medium",
    you_label="Ты",
    farewell="До связи{addr}.",
    stt_hint="Эй, {name}. Какая сегодня погода? Какие новости? Поставь таймер.",
    units={
        "h": ("час", "часа", "часов"),
        "m": ("минута", "минуты", "минут"),
        "s": ("секунда", "секунды", "секунд"),
    },
    and_word="и",
    timer_done="Таймер сработал{addr}: {duration}.",
    timer_done_label="Таймер «{label}» сработал{addr}: {duration}.",
    one_moment="Момент.",
    no_reply="Не получилось сформулировать ответ. Спроси ещё раз.",
    sign_off="Ладно{addr}. Отдыхаю.",
    sign_off_thanked="Не за что{addr}. Отдыхаю.",
    welcome="Не за что{addr}.",
)

OTHER_NAMES = {
    "es": "español",
    "fr": "français",
    "de": "Deutsch",
    "it": "italiano",
    "nl": "Nederlands",
    "ja": "日本語",
    "zh": "中文",
}


def normalize_code(code: str) -> str:
    parts = code.replace("_", "-").strip().split("-")
    lang = parts[0].lower()
    return f"{lang}-{parts[1].upper()}" if len(parts) > 1 and parts[1] else lang


def get_locale(code: str | None) -> Locale:
    """ru-*, pt-* and en-* are fully supported; others get the English prompt."""
    code = normalize_code(code or "ru-RU")
    lang = code.split("-")[0]
    if lang == "ru":
        return replace(RU, code=code)
    if lang == "pt":
        return replace(PT_BR, code=code)
    if lang == "en":
        return replace(
            EN, code=code, language_name="English" if code != "en-GB" else EN.language_name
        )
    return replace(
        EN,
        code=code,
        lang=lang,
        language_name=OTHER_NAMES.get(lang, code),
        native_prompt=False,
    )
