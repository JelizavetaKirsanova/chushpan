"""Action intent and single-use, expiring confirmations outside the LLM's control."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum

from backend.context import RequestContext, request_scope


class Risk(StrEnum):
    READ = "safe"
    CHANGE = "state_change"
    DANGEROUS = "dangerous"


class ActionDenied(RuntimeError):
    pass


class ConfirmationRequired(RuntimeError):
    def __init__(self, summary: str, fingerprint: str, arguments: dict) -> None:
        self.summary, self.fingerprint, self.arguments = summary, fingerprint, arguments


approved_action: ContextVar[str | None] = ContextVar("approved_action", default=None)


def fingerprint(name: str, arguments: dict, summary: str) -> str:
    payload = json.dumps([name, arguments, summary], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def command_text(text: str) -> str:
    """Only direct current-user commands, not quoted instructions or source excerpts."""
    text = " ".join(text.casefold().split()).strip()
    text = re.sub(
        r"^(?:(?:привет|здравствуй|добрый (?:день|вечер)|доброе утро)[, .!]+)?"
        r"(?:(?:эй|хей|хэй|hey)[, .!]+)?(?:чушпа[́]?н|чушпэн|chushpan|jarvis|джарвис)[, .!:]+",
        "",
        text,
    )
    text = re.sub(
        r"^(?:какая (?:еще |ещё )?явная просьба\?\s*)?"
        r"(?:я (?:тебя|вас) прошу|прошу(?: тебя| вас)?)[, .!:]+",
        "",
        text,
    )
    text = re.sub(r"^(?:пожалуйста[, ]+|please[, ]+)", "", text)
    text = re.sub(r"[, ]+пожалуйста[.!? ]*$", "", text)
    text = re.sub(r"^(?:можешь(?:,? пожалуйста)?|can you|could you)\s+", "", text)
    infinitives = {
        "открыть": "открой",
        "запустить": "запусти",
        "удалить": "удали",
        "забыть": "забудь",
        "запомнить": "запомни",
        "сделать": "сделай",
        "поставить": "поставь",
        "включить": "включи",
        "выключить": "выключи",
        "установить": "установи",
        "погасить": "погаси",
    }
    first, separator, rest = text.partition(" ")
    text = infinitives.get(first, first) + separator + rest
    text = re.sub(r"^(сделай|поставь|установи|включи|выключи|открой|запусти) мне ", r"\1 ", text)
    return text


def control_text(text: str) -> str:
    """Grammatical normalization for device plans; the original request stays intact."""
    value = command_text(text).replace("ё", "е").strip(".!? ")
    value = re.sub(r"(?<=[а-яa-z])(?=\d)|(?<=\d)(?=[а-яa-z])", " ", value)
    value = re.sub(r"^(?:а\s+)?теперь[, ]+", "", value)
    value = re.sub(r"\b(?:чуть|немного|немножко)\s+(?=(?:по)?темнее|(?:по)?ярче)", "", value)
    value = re.sub(r"\b(?:потемнее|темнее)\b", "тусклее", value)
    return value


def screen_requested(text: str) -> bool:
    """A visible-screen request; questions about code or errors are not screenshots."""
    lead = command_text(text.splitlines()[0] if text.splitlines() else text)
    return bool(
        re.match(r"что|посмотри|проанализируй|объясни|прочитай|почему", lead)
        and re.search(r"\bэкран(?:е|а)?\b", lead)
    )


def vision_requested(text: str) -> bool:
    command = command_text(text)
    return screen_requested(text) or bool(
        re.match(r"посмотри(?: на)? (?:эту |это )?(?:картинк|изображен)", command)
        or (
            re.match(r"посмотри|проанализируй|прочитай", command)
            and re.search(r"\.(?:png|jpe?g|webp)\b", command)
        )
    )


def action_requested(text: str) -> bool:
    """Conservative guard for direct action requests; quoted source text is not authority."""
    command = command_text(text)
    from backend.local_commands import route

    local = route(text)
    if local and local[0] in {"led_control", "open_website", "set_brightness", "set_volume"}:
        return True
    if vision_requested(text):
        return True
    if re.match(
        r"(?:открой|закрой|переключ\w*|сверни|усыпи|заблокируй"
        r"|включи|включай|запомни|забудь|удали|сотри|запусти|выполни"
        r"|open|close|switch|remember|forget|run)\b",
        command,
    ):
        return True
    return bool(
        re.match(
            r"(?:сделай|поставь|включи|выключи|измени|перемести|продолжи|останови|разверни|восстанови)\b",
            command,
        )
        and re.search(
            r"звук|громк|яркост|музык|песн|трек|таймер|окн|скриншот|mac|компьютер", command
        )
    )


_INTENT = {
    "open_app": r"(?:открой|открывай|запусти|open|launch|abre|abra)\b",
    "set_volume": (
        r"(?:сделай|прибавь|убавь|включи|выключи|верни|заглуши|громче|тише|звук|громкость"
        r"|set|mute|unmute|lower|raise|tire|volte|aument|abaix)"
    ),
    "play_music": r"(?:включи|поставь|сыграй|открой|продолжи|play|resume|toqu|coloc)",
    "media_control": (
        r"(?:поставь|включи|выключи|продолж|останов|пауза|паузу|стоп|следующ|предыдущ"
        r"|назад|перемеш|повтор|лайк|открой|pause|resume|stop|next|previous|shuffle|repeat"
        r"|like|paus|próxim|volta)"
    ),
    "media_volume": (
        r"(?:сделай|музык|громче|тише|прибавь|убавь|уменьш|увелич|громкость"
        r"|set|lower|raise|aument|abaix)"
    ),
    "media_seek": r"(?:перемот|промот|назад|вперёд|вперед|seek|skip)",
    "set_timer": r"(?:поставь|запусти|засеки|таймер|напомни|set|start|timer|coloc|crie)",
    "change_timer": r"(?:измени|поменяй|продли|сделай|change|make|altere|mude)",
    "cancel_timer": r"(?:отмени|убери|удали|стоп|cancel|cancele|cancela)",
    "remember": (
        r"(?:запомни|запиши|сохрани|исправь|обнови|remember|note|save|correct|update"
        r"|lembre|anota|guarde|corrig)"
    ),
    "forget": r"(?:забудь|удали|сотри|forget|delete|remove|esqueça|esqueca|apague)\b",
    "music_playlist": r"(?:запомни|назначь|сделай|поменяй|замени|поставь|выбери)\b",
    "go_to_sleep": (
        r"(?:спи|отдыхай|пока|спасибо|всё|все|go to sleep|bye|thanks|obrigad|tchau|pode)"
    ),
}


def explicit_request(name: str, arguments: dict, text: str) -> bool:
    command = command_text(text)
    if name == "manage_app":
        verb = (
            r"(?:переключ|перейди|активируй|покажи|switch|activate)"
            if arguments.get("action") == "switch"
            else r"(?:закрой|заверши|выйди|quit|close)"
        )
        return bool(re.match(verb, command)) and app_mentioned(arguments.get("name", ""), command)
    if name == "manage_window":
        verbs = {
            "focus": r"переключ|покажи|выбери|активируй|focus|switch",
            "minimize": r"сверни|сворачивай|minimize",
            "restore": r"разверни|восстанови|покажи|restore",
            "maximize": r"разверни|увеличь|растяни|maximize",
            "left": r"перемести|поставь|сдвинь|размести|move",
            "right": r"перемести|поставь|сдвинь|размести|move",
            "move": r"перемести|поставь|сдвинь|размести|move",
            "resize": r"измени|увеличь|уменьши|сделай|resize",
            "fullscreen": r"включи|разверни|сделай|переведи|fullscreen",
            "exit_fullscreen": r"выйди|выключи|убери|верни|exit",
            "move_space": r"перемести|перенеси|move",
        }
        action = arguments.get("action")
        if not re.match(verbs.get(action, r"(?!)"), command):
            return False
        if action in {"left", "right"}:
            return bool(re.search(r"лев|left" if action == "left" else r"прав|right", command))
        if action in {"fullscreen", "exit_fullscreen"}:
            return bool(re.search(r"полн.{0,12}экран|fullscreen|full screen", command))
        return True
    if name == "mac_power":
        patterns = {
            "lock": r"(?:заблокируй|заблокировать|lock).*"
            r"(?:экран|mac|мак|компьютер|ноутбук|screen)",
            "sleep": r"(?:усыпи.*(?:mac|мак|компьютер|ноутбук)|"
            r"(?:отправь|переведи).*(?:mac|мак|компьютер|ноутбук).*(?:сон|спящ))",
            "shutdown": r"(?:выключи|выключить|заверши работу|shut down).*"
            r"(?:mac|мак|компьютер|ноутбук)",
        }
        return bool(re.match(patterns.get(arguments.get("action"), r"(?!)"), command))
    if name == "set_brightness":
        return bool(
            re.match(r"измени|сделай|увеличь|уменьши|прибавь|убавь|установи|поставь|set", command)
            and re.search(r"яркост|brightness", command)
            and re.search(
                r"\b(?:mac|macbook|мак|макбука?|экран[ае]?|диспле[йя]|display|screen)\b", command
            )
        )
    if name == "take_screenshot":
        return bool(
            re.match(r"сделай|создай|сними|сохрани|take|capture", command)
            and re.search(r"скриншот|снимок экрана|screenshot", command)
        )
    pattern = _INTENT.get(name)
    if not pattern or not re.match(pattern, command):
        return False
    if name == "open_app":
        requested = str(arguments.get("name", "")).casefold().removesuffix(".app")
        return app_mentioned(requested, command) or bool(
            requested == "youtube music" and re.search(r"ютуб.{0,3}(?:музык|мьюзик)", command)
        )
    if name in {"set_volume", "media_volume"}:
        return bool(re.search(r"звук|громк|тише|музык|volume|mute|som|aument|abaix", command))
    if name in {"set_timer", "change_timer", "cancel_timer"}:
        return bool(re.search(r"таймер|timer|напомни|засеки|\b(?:его|it)\b", command))
    if name == "media_control":
        actions = {
            "pause": r"пауз|pause|paus",
            "stop": r"стоп|останов|stop",
            "resume": r"продолж|resume|volta|play",
            "next": r"следующ|next|próxim",
            "previous": r"предыдущ|назад|previous|volta",
            "open": r"открой|open",
            "like": r"лайк|понравивш|like",
            "shuffle_on": r"перемеш|shuffle",
            "shuffle_off": r"перемеш|shuffle",
            "repeat_on": r"повтор|repeat",
            "repeat_off": r"повтор|repeat",
            "repeat_one": r"повтор|repeat",
        }
        return bool(re.search(actions.get(arguments.get("action"), r"(?!)"), command))
    if name == "forget":
        return bool(
            re.match(r"забудь|forget|esqueça|esqueca", command)
            or re.search(r"памят|факт|воспоминан|memory|memories|fact|memória", command)
        )
    if name == "remember" and re.match(r"сохрани|исправь|обнови", command):
        return bool(re.search(r"памят|факт|запись|\bчто\b", command))
    if name == "play_music" and re.search(r"свет|компьютер|ноутбук", command):
        return False
    if name == "music_playlist":
        return bool(re.search(r"плейлист|музык|playlist", command))
    return True


def risk_for(name: str, arguments: dict) -> Risk:
    if name == "music_command":
        from backend.music import parse

        intent = parse(arguments.get("command", ""))
        return Risk.READ if intent and intent.action == "status" else Risk.CHANGE
    if name == "youtube_sleep":
        if arguments.get("action") == "reset":
            return Risk.DANGEROUS
        return Risk.READ if arguments.get("action") in {"list", "count", "current"} else Risk.CHANGE
    if name == "manage_memory":
        return (
            Risk.READ
            if arguments.get("action")
            in {"list", "recall", "last_saved", "explain", "character_list", "clarify"}
            else Risk.DANGEROUS
        )
    if (
        name in {"remember", "forget", "mac_power"}
        or (name == "manage_app" and arguments.get("action") == "quit")
        or (name == "music_playlist" and arguments.get("action") == "assign")
    ):
        return Risk.DANGEROUS
    if (
        name in {"manage_app", "manage_window", "set_brightness", "take_screenshot", "open_path"}
        or name in _INTENT
        and name != "music_playlist"
    ):
        return Risk.CHANGE
    return Risk.READ


APP_ALIASES = {
    "safari": ("сафари",),
    "finder": ("файндер", "файндере"),
    "terminal": ("терминал",),
    "intellij idea": ("intellij", "идея"),
    "intellij": ("идея",),
    "textedit": ("текстэдит",),
    "youtube music": ("ютуб музыка", "ютуб мьюзик"),
}


def local_app_name(text: str) -> str | None:
    """Only an exact known app name/alias is eligible for a deterministic route."""
    value = text.casefold().removesuffix(".app").strip()
    names = {
        "Safari",
        "Finder",
        "Terminal",
        "TextEdit",
        "IntelliJ IDEA",
        "Telegram",
        "Discord",
        "Notes",
        "Calendar",
        "Calculator",
    }
    return next(
        (
            name
            for name in sorted(names)
            if value in (name.casefold(), *APP_ALIASES.get(name.casefold(), ()))
        ),
        None,
    )


def app_mentioned(name: str, text: str) -> bool:
    name = name.casefold().removesuffix(".app").strip()
    return bool(name) and any(
        re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text.casefold())
        for alias in (name, *APP_ALIASES.get(name, ()))
    )


@dataclass(frozen=True)
class PendingAction:
    name: str
    arguments: dict
    summary: str
    fingerprint: str
    context: RequestContext
    code: str
    expires: float

    @property
    def message(self) -> str:
        spoken_code = " ".join(self.code)
        return (
            f"Нужно подтверждение: {self.summary}. "
            f"Скажи или напиши «Подтверждаю {spoken_code}» в течение 90 секунд. "
            "Или скажи «Отмена»."
        )


def confirmation_code(text: str) -> str | None:
    text = command_text(text).rstrip(".! ")
    match = re.fullmatch(r"(?:подтверждаю|confirm)(?: код)? ([\w ]+)", text)
    if not match:
        return None
    digits = {
        w: str(i)
        for i, w in enumerate(
            ("ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
        )
    }
    code = "".join(digits.get(part, part) for part in match[1].split())
    return code if re.fullmatch(r"\d{4}", code) else "invalid"


class Confirmations:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.pending: dict[tuple[str, str], PendingAction] = {}
        self.last_execution: tuple[str, dict, dict] | None = None
        self._used_codes: set[str] = set()

    def clear(self, context: RequestContext) -> None:
        self.pending.pop(context.binding, None)

    def request(
        self, name: str, args: dict, exc: ConfirmationRequired, context: RequestContext
    ) -> dict:
        # At most one exact action per device/session. Never extend an existing request.
        self.pending = {
            key: action for key, action in self.pending.items() if action.expires > self.clock()
        }
        if len(self.pending) >= 32 and context.binding not in self.pending:
            return {"error": "Слишком много ожидающих подтверждений."}
        pending = self.pending.get(context.binding)
        if pending is None or pending.fingerprint != exc.fingerprint:
            if len(self._used_codes) >= 9000:
                return {"error": "Лимит подтверждений сессии исчерпан. Начни новую сессию."}
            code = str(secrets.randbelow(9000) + 1000)
            while code in self._used_codes:
                code = str(secrets.randbelow(9000) + 1000)
            self._used_codes.add(code)
            pending = PendingAction(
                name,
                json.loads(json.dumps(args)),
                exc.summary,
                exc.fingerprint,
                context,
                code,
                self.clock() + 90,
            )
            self.pending[context.binding] = pending
        return {"status": "confirmation_required", "message": pending.message}

    async def respond(
        self,
        context: RequestContext,
        execute: Callable[[str, dict], Awaitable[str]],
        direct_reply: Callable[[str, dict], str | None] | None = None,
    ) -> str | None:
        self.last_execution = None
        code = confirmation_code(context.text)
        pending = self.pending.get(context.binding)
        if command_text(context.text).rstrip(".! ") in {"отмена", "отмени", "cancel"}:
            self.clear(context)
            return "Отменил." if pending else "Нечего отменять."
        if pending is None:
            return "Активного подтверждения нет. Повтори саму просьбу." if code else None
        if self.clock() >= pending.expires:
            self.clear(context)
            return "Подтверждение истекло. Повтори саму просьбу." if code else None
        if code is None:
            if command_text(context.text).rstrip(".! ") in {"да", "ага", "yes", "sim"}:
                return pending.message
            self.clear(context)  # A new request invalidates the old action.
            return None
        if code != pending.code or context.request_id == pending.context.request_id:
            return "Код не подходит к ожидающему действию. " + pending.message
        # Consume before awaiting: parallel replies and repeated codes cannot replay it.
        self.clear(context)
        if context.device != pending.context.device or not context.device.authenticated:
            return "Разрешения устройства изменились. Повтори просьбу."
        approved = approved_action.set(pending.fingerprint)
        try:
            with request_scope(
                RequestContext(context.device, pending.context.text, context.request_id)
            ):
                result = json.loads(await execute(pending.name, pending.arguments))
                self.last_execution = pending.name, pending.arguments, result
        finally:
            approved_action.reset(approved)
        if not isinstance(result, dict):
            return "Инструмент не подтвердил результат. Действие повторно не запускаю."
        if "error" in result:
            return "Не выполнил: " + result["error"]
        if result.get("status") == "confirmation_required":
            self.clear(context)
            return "Действие изменилось. Повтори просьбу и проверь новые параметры."
        if pending.name == "forget":
            return "Удалил этот факт из памяти."
        if pending.name == "remember":
            return "Запомнил."
        if direct_reply and (message := direct_reply(pending.name, result)):
            return message
        return "Готово: " + pending.summary + "."
