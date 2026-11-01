"""Local waiting interjections, independent of model text and conversation history."""

from __future__ import annotations

import logging
import random
import re
import time
from collections import deque


def trace(event: str, **fields) -> None:
    """Timing and lifecycle only: never log transcripts or generated text."""
    from backend.state import request_context

    logging.getLogger("backend.voice.events").info(
        "VOICE_EVENT event=%s request=%s t=%.6f %s",
        event,
        request_context.get(),
        time.monotonic(),
        " ".join(f"{key}={value}" for key, value in fields.items()),
    )


PHRASES = (
    "Ой, ну ща.",
    "Секунду.",
    "Так, щас.",
    "Погоди.",
    "Ща разберусь.",
    "Момент.",
    "Ща гляну.",
    "Хм, интересно.",
    "Да подожди ты.",
    "Так, думаю.",
    "Бля, щас.",
    "Ну, погнали.",
    "Так-так…",
    "Ща, не мешай гению работать.",
    "Погоди, я тут мысли в кучу собираю.",
    "Ну и задачку ты мне подкинула, конечно.",
    "Сейчас разберусь, куда эта хрень запряталась.",
    "Так, магия сама себя не сделает, подожди.",
)


def allow_waiting(text: str) -> bool:
    """Whole local social commands only; never suppress a substantive follow-up question."""
    from backend.dialogue import CLOSE, normalize
    from backend.local_commands import route
    from backend.music import parse

    intent = parse(text)
    if intent and intent.action in {
        "pause",
        "resume",
        "next",
        "previous",
        "status",
        "quieter",
        "louder",
        "diagnose",
        "request_access",
    }:
        return False

    if route(text) is not None or normalize(text) in CLOSE | {"повтори последнее"}:
        return False
    words = re.sub(r"[^\w\s]", " ", text.casefold().replace("́", "")).split()
    words = [word for word in words if word not in {"эй", "чушпан"}]
    return bool(words) and " ".join(words) not in {
        "доброе утро",
        "добрый вечер",
        "добрый день",
        "спокойной ночи",
        "доброй ночи",
        "привет",
        "здравствуй",
        "здравствуйте",
        "спасибо",
        "спасибо большое",
        "благодарю",
        "пока",
        "до свидания",
    }


class WaitingPhrases:
    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.recent: deque[str] = deque(maxlen=4)

    def choose(self, durations: dict[str, float]) -> str | None:
        available = [
            phrase for phrase in PHRASES if phrase in durations and phrase not in self.recent
        ]
        if not available:
            return None
        short = [phrase for phrase in available if durations[phrase] <= 2]
        longer = [phrase for phrase in available if durations[phrase] > 2]
        pool = (short if self.rng.random() < 0.7 else longer) or available
        phrase = self.rng.choice(pool)
        self.recent.append(phrase)
        return phrase
