"""Conservative Russian statement patterns; no assistant guesses or transcript backfill."""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def extract_statement(t):
    patterns = (
        (r"меня зовут (.{2,60})", "profile", lambda m: "name"),
        (r"(?:называй меня|обращайся ко мне) (.{2,60})", "profile", lambda m: "address"),
        (r"не называй меня (.{2,60})", "correction", lambda m: "address:avoid:" + m[1]),
        (r"(?:мой язык|говори со мной) (.{2,60})", "profile", lambda m: "language"),
        (
            r"мой (?:преподаватель|коллега) (.{2,80})",
            "people",
            lambda m: "person:" + m[1].split()[0],
        ),
        (
            r"(?:я изучаю|мой предмет|мой преподаватель по) (.{2,160})",
            "study",
            lambda m: "study:" + m[1].split()[0],
        ),
        (
            (
                r"(?:у меня (?:экзамен|дедлайн)|завтра (?:экзамен|дедлайн)|завтра "
                r"у меня экзамен)(.{0,160})"
            ),
            "temporary",
            lambda m: "temporary:" + m[0],
        ),
        (
            (
                r"(?:я работаю (?:в|над)|моё собеседование|мое собеседование|ищу "
                r"работу|моя профессиональная цель) (.{2,160})"
            ),
            "work",
            lambda m: "work:" + m[1].split()[0],
        ),
        (
            r"(?:мой компьютер|мой телефон|моя лента|моё устройство|мое устройство) (.{2,160})",
            "device",
            lambda m: "device:" + m[0].split()[1],
        ),
        (
            r"(?:в моей комнате|мой сценарий|дома я предпочитаю) (.{2,160})",
            "home",
            lambda m: "home:" + m[1].split()[0],
        ),
        (r"я (?:обычно|каждый день) (.{3,160})", "habit", lambda m: "habit:" + m[1].split()[0]),
        (r"(?:моя цель|мой план) (.{3,160})", "goal", lambda m: "goal:" + m[1].split()[0]),
        (
            r"мы (?:решили|договорились) (.{3,160})",
            "conversation",
            lambda m: "decision:" + m[1].split()[0],
        ),
        (
            r"(?:этот способ не работает|теперь лента работает|эту серию я уже смотрела)",
            "correction",
            lambda m: "report:" + m[0],
        ),
    )
    for pattern, category, key in patterns:
        m = re.fullmatch(pattern, t)
        if m:
            return [(category, key(m), t)]
    m = re.fullmatch(r"я (?:больше |теперь )?(?:не люблю|люблю|предпочитаю) (.{2,160})", t)
    if m and re.search(r"аниме|фильм|сериал|музык|джаз|рок|комеди|жанр", m[1]):
        subject = re.sub(r"^(?:смотреть|слушать)\s+", "", m[1])
        return [("entertainment", "taste:" + subject, t)]
    return []


def expiration(text, days, timezone="Europe/Tallinn"):
    now = datetime.now(ZoneInfo(timezone))
    if re.search(r"\bзавтра\b", text, re.I):
        # Keep tomorrow's deadline through the end of the local calendar day.
        value = (now + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif re.search(r"\bсегодня\b", text, re.I):
        value = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        value = now + timedelta(days=days)
    return value.astimezone(UTC).isoformat(timespec="seconds")
