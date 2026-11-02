"""A conservative fast-mode allowlist, never a replacement for intent/security checks."""

import re

from backend.security import action_requested, command_text


def simple_request(text: str) -> bool:
    text = command_text(text).rstrip(".!? ")
    text = re.sub(r"[,.!?]?\s+(?:ответь коротко|ответь одним предложением)$", "", text)
    if (
        len(text) > 320
        or action_requested(text)
        or re.search(
            r"\d\s*[-+*/=]|код|программ|докаж|доказ|рассч|посчита|анализ|сравн|услови|"
            r"алгоритм|математ|стратег|диагноз|симптом|лечени|лекар|договор|юрид|финанс|инвест|подроб|"
            r"теорем|уравнен|формул|реши |задач|бюджет|ограничен|операци|сердц|"
            r"взлом|парол|безопасност|оруж|опасн|травм|проект|план |"
            r"пошаг|исслед|обосн|источник|актуаль|сегодня|погода|новост|помни|запомни|плейлист|"
            r"включ|выключ|удали|открой|найди|проверь|курс валют|цена|стоимост|\n|;",
            text,
            re.I,
        )
    ):
        return False
    return bool(
        text in {"привет", "как дела", "как ты", "расскажи шутку"}
        or re.fullmatch(r"привет[,! ]+(?:как (?:ты|дела|настроение))", text)
        # Routine explanation and a follow-up can use the same history and facts
        # without a hidden reasoning pass. Tool/current/high-stakes requests above
        # keep the full reasoning route. This is not an authorization decision.
        or re.fullmatch(r"(?:а )?почему .+", text)
        or re.fullmatch(r"(?:объясни|расскажи)[, ]+(?:как|почему|что такое|о |про ).+", text)
        or re.fullmatch(r"что такое [\w -]+(?:[.?] ответь одним предложением)?", text)
        or re.fullmatch(
            r"(?:как (?:мне )?|(?:подскажи|посоветуй)(?: мне)?[, ]+(?:как )?|"
            r"помоги(?: мне)? (?:выбрать (?:способ )?)?)"
            r"(?:лучше |быстро )?(?:отдохнуть|расслабиться)"
            r"(?: (?:после (?:уч[её]бы|работы|тяж[её]лого дня)|вечером))?"
            r"(?:[.?,] (?:дай|предложи) (?:мне )?(?:три|несколько|пару) "
            r"(?:конкретных |простых )?совет(?:а|ов))?",
            text,
        )
    )
