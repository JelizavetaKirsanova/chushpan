"""Fast, categorical recipient decisions. Confidence is not a speaker identity score."""

import re
from dataclasses import dataclass
from typing import Literal

from backend.dialogue import CLOSE, CONTROLS, normalize
from backend.security import confirmation_code


@dataclass(frozen=True)
class SpeechSignals:
    confirmed: bool
    stt_confident: bool
    echo: bool = False


@dataclass(frozen=True)
class Decision:
    confidence: Literal["high", "medium", "low"]
    reason: str
    kind: Literal["direct", "continuation", "task", "music", "led", "devices", "ignore"] = "ignore"

    @property
    def accepted(self) -> bool:
        return self.confidence == "high" and self.kind != "ignore"


@dataclass(frozen=True)
class TaskSnapshot:
    request_id: int
    tool: str
    status: str
    updated_at: float

    def summary(self) -> str:
        return {
            "running": "Предыдущая задача ещё выполняется.",
            "completed": "Предыдущий инструмент сообщил об успешном завершении задачи.",
            "submitted": (
                "Команда ленты записана по Bluetooth. Физическое выполнение не подтверждено."
            ),
            "failed": "Предыдущая задача вернула ошибку или отказ. Успех не подтверждён.",
            "cancelled": "Ожидание предыдущей задачи отменено. Фактический результат неизвестен.",
            "confirmation_required": (
                "Предыдущая задача запросила подтверждение. Без него не запускаю."
            ),
        }.get(self.status, "От предыдущей задачи получен результат, но успех не подтверждён.")


def discussing(text: str) -> bool:
    raw = " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())
    return bool(
        re.match(
            r"^(?:(?:мой|наш|этот|твой) чушпан\b|чушпан (?:опять|снова|сломал|сломался|говорит)\b)",
            raw,
        )
    )


def task_reference(text: str) -> bool:
    value = normalize(text)
    return bool(
        re.fullmatch(
            r"(?:ну )?что там(?: с (?:моим |моей )?(?:видео|сайтом|задачей))?|"
            r"ты уже открыл сайт|получилось|а почему не работает|"
            r"продолжай то что делал",
            value,
        )
    )


def choose_task(text: str, tasks: list[TaskSnapshot]) -> TaskSnapshot | None:
    value = normalize(text)
    if "видео" in value:
        tasks = [task for task in tasks if task.tool in {"night_mode", "youtube_sleep"}]
    elif "сайт" in value:
        tasks = []  # No instrument currently proves that a particular website opened.
    return tasks[0] if len(tasks) == 1 else None


def decide(
    text: str,
    *,
    addressed: bool,
    active: bool,
    topic: str,
    signals: SpeechSignals,
    task: TaskSnapshot | None = None,
    strictness: str = "contextual",
    confirmation_pending: bool = False,
    followup_pending: bool = False,
    control_context: bool = False,
    continuation_window: bool = False,
) -> Decision:
    value = normalize(text)
    if not signals.confirmed or signals.echo:
        return Decision("low", "no_speech_or_echo")
    if discussing(text):
        return Decision("low", "third_person_discussion")
    if not signals.stt_confident:
        return Decision("medium", "uncertain_stt")
    if addressed:
        return Decision("high", "explicit_address", "task" if task_reference(text) else "direct")
    if not value:
        return Decision("low", "empty_text")
    if not active:
        return Decision("medium" if task_reference(text) else "low", "wake_required")
    if (not continuation_window and re.search(r"чушпан|chushpan", value)) or re.search(
        r"мам[аеуы]?|пап[аеуы]?|приед|приход|забер|встретим|позвонишь", value
    ):
        return Decision("low", "other_person_or_third_person")
    if strictness == "addressed":
        return Decision("medium", "strict_wake_required")
    from backend.led import parse as led_parse

    if led_parse(text) is not None:
        return Decision("high", "local_led_command", "led")
    if control_context:
        return Decision("high", "local_control_plan", "devices")
    if followup_pending:
        from backend.brain.followups import is_short

        if is_short(text):
            return Decision("high", "bound_read_followup", "continuation")
    from backend.music import music_context, parse

    if music_context(topic) and parse(text) is not None:
        return Decision("high", "bound_music_command", "music")
    if task_reference(text):
        if task is not None:
            return Decision("high", "unique_task_reference", "task")
        return Decision("medium", "ambiguous_task")
    if value in CLOSE or (topic and value in CONTROLS | {"спасибо", "пока"}):
        return Decision("high", "anchored_control", "continuation")
    if confirmation_pending and confirmation_code(text) is not None:
        return Decision("high", "bound_confirmation", "continuation")
    if topic and re.fullmatch(
        r"а (?:можешь |можно )?(?:пример (?:показать|привести)|покажи пример)", value
    ):
        return Decision("high", "anchored_example", "continuation")
    if topic and re.fullmatch(r"а (?:завтра|сегодня|в воскресенье|в субботу|в понедельник)", value):
        if re.search(r"погод|прогноз|температур|дожд|врем|расписан", normalize(topic)):
            return Decision("high", "anchored_date", "continuation")
    if (
        topic
        and len(value.split()) >= 3
        and re.match(r"(?:а )?(?:почему|зачем|как|что такое|объясни|расскажи)\b", value)
        and not re.search(r"\b(?:он|она|они|ты|вы)\b", value)
    ):
        return Decision("high", "active_question", "continuation")
    if continuation_window and topic and len(value.split()) >= 2:
        # This grant comes only from the bounded post-answer listening phase,
        # never merely from an old topic or a recognizer confidence value.
        ambiguous = {"да", "нет", "ну", "ага", "угу", "ладно", "давай", "ок", "хорошо"}
        embedded_action = re.search(
            r"\b(?:открой|включи|выключи|запусти|поставь|установи|удали|выполни)\b", value
        )
        background = re.match(r"^(?:они |он |она |телевизор |музыка играет)", value)
        if not embedded_action and not background and not set(value.split()) <= ambiguous:
            return Decision("high", "active_dialogue", "continuation")
    return Decision("medium", "recipient_unclear")
