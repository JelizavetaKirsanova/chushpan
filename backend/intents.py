"""Typed local control plans over the existing parsers and guarded tool registry."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.context import (
    planned_operation,
    request_context,
    voice_addressed,
    voice_control_command,
    voice_led_command,
    voice_music_command,
    voice_session,
)
from backend.latency import current, measure
from backend.led import _NUMBERS, _TARGET, _VERBS, color_rgb
from backend.led import parse as led_parse
from backend.security import control_text


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LEDParameters(StrictModel):
    power: bool | None = None
    color: str | None = Field(default=None, pattern=r"^#[0-9A-F]{6}$")
    brightness: int | None = Field(default=None, ge=0, le=100)
    brightness_delta: Literal[-10, 10] | None = None

    @model_validator(mode="after")
    def coherent(self):
        if all(
            value is None
            for value in (self.power, self.color, self.brightness, self.brightness_delta)
        ):
            raise ValueError("Нужен хотя бы один параметр подсветки.")
        if self.brightness is not None and self.brightness_delta is not None:
            raise ValueError("Абсолютная и относительная яркость противоречат друг другу.")
        if self.power is False and any(
            v is not None for v in (self.color, self.brightness, self.brightness_delta)
        ):
            raise ValueError("Уточни: выключить подсветку или настроить включённую?")
        return self


class LEDAction(StrictModel):
    intent: Literal["device_control"] = "device_control"
    device: Literal["led_strip"] = "led_strip"
    parameters: LEDParameters

    def call(self):
        p, clauses = self.parameters, []
        if p.power is not None:
            clauses.append("Включи ленту" if p.power else "Выключи ленту")
        if p.color:
            rgb = [int(p.color[i : i + 2], 16) for i in (1, 3, 5)]
            clauses.append("Сделай ленту RGB " + " ".join(map(str, rgb)))
        if p.brightness is not None:
            clauses.append(f"Яркость ленты {p.brightness} процентов")
        if p.brightness_delta is not None:
            clauses.append("Сделай ленту " + ("поярче" if p.brightness_delta > 0 else "тусклее"))
        return "led_control", {"command": " и ".join(clauses)}


class Brightness(StrictModel):
    brightness: int = Field(ge=0, le=100)


class DisplayAction(StrictModel):
    device: Literal["mac_display"] = "mac_display"
    parameters: Brightness

    def call(self):
        return "set_brightness", {"level": self.parameters.brightness}


class ExternalDisplayAction(StrictModel):
    device: Literal["external_display"] = "external_display"
    parameters: Brightness

    def call(self):
        return "set_external_brightness", {"level": self.parameters.brightness}


class MusicParameters(StrictModel):
    command: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def known(self):
        from backend.music import parse

        if parse(self.command) is None:
            raise ValueError("Неизвестная музыкальная команда.")
        return self


class MusicAction(StrictModel):
    device: Literal["music"] = "music"
    parameters: MusicParameters

    def call(self):
        return "music_command", {"command": self.parameters.command}


class BrowserParameters(StrictModel):
    site: Literal["youtube"] = "youtube"


class BrowserAction(StrictModel):
    device: Literal["browser"] = "browser"
    parameters: BrowserParameters = Field(default_factory=BrowserParameters)

    def call(self):
        return "open_website", {"site": self.parameters.site}


class VolumeParameters(StrictModel):
    level: int | None = Field(default=None, ge=0, le=100)
    direction: Literal["up", "down"] | None = None

    @model_validator(mode="after")
    def one_mode(self):
        if (self.level is None) == (self.direction is None):
            raise ValueError("Нужен уровень либо направление изменения громкости.")
        return self


class VolumeAction(StrictModel):
    device: Literal["mac_sound"] = "mac_sound"
    parameters: VolumeParameters

    def call(self):
        return "set_volume", self.parameters.model_dump(exclude_none=True)


class AppParameters(StrictModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[\w][\w .-]*$")


class AppAction(StrictModel):
    device: Literal["application"] = "application"
    parameters: AppParameters

    def call(self):
        return "open_app", {"name": self.parameters.name}


class TVParameters(StrictModel):
    volume: Literal["quieter"] = "quieter"


class TVAction(StrictModel):
    device: Literal["tv"] = "tv"
    parameters: TVParameters = Field(default_factory=TVParameters)

    def call(self):
        return None, {}


Action = Annotated[
    LEDAction
    | DisplayAction
    | ExternalDisplayAction
    | MusicAction
    | BrowserAction
    | TVAction
    | VolumeAction
    | AppAction,
    Field(discriminator="device"),
]


class ControlPlan(StrictModel):
    intent: Literal["compound_control"] = "compound_control"
    mode: Literal["sequence", "parallel"] = "sequence"
    actions: list[Action] = Field(default_factory=list, max_length=8)
    clarification: str | None = None
    clarification_kind: Literal["led_brightness", "brightness_target"] | None = None

    @model_validator(mode="after")
    def executable_or_question(self):
        if bool(self.actions) == bool(self.clarification):
            raise ValueError("Нужен исполнимый план либо уточнение, а не оба сразу.")
        if self.clarification_kind and not self.clarification:
            raise ValueError("Тип уточнения допустим только для вопроса, без действий.")
        if self.mode == "parallel":
            from backend.tools.registry import TOOL_RESOURCES

            resources = []
            for action in self.actions:
                name, _ = action.call()
                resource = TOOL_RESOURCES.get(name)
                # Browser and music can both operate Safari even with different
                # legacy registry locks. Keep these branches sequential as well.
                domains = {resource} if resource else set()
                if action.device in {"browser", "application", "music"}:
                    domains.add("safari")
                if not domains or any(domains & previous for previous in resources):
                    raise ValueError("Параллельные действия конфликтуют. Уточни порядок.")
                resources.append(domains)
        return self


def led_action(args):
    params = {}
    for step in args["commands"] if args["action"] == "sequence" else [args]:
        action = step["action"]
        if action in {"on", "off"}:
            key, value = "power", action == "on"
        elif action == "color":
            key, value = "color", "#" + "".join(f"{v:02X}" for v in step["rgb"])
        elif action == "brightness":
            key, value = "brightness", step["level"]
        elif action in {"ярче", "тусклее"}:
            key, value = "brightness_delta", 10 if action == "ярче" else -10
        else:
            return None  # A status query stays on its original read route.
        if key in params and params[key] != value:
            raise ValueError("Параметры подсветки противоречат друг другу.")
        params[key] = value
    return LEDAction(parameters=LEDParameters(**params))


def _display(text):
    external = re.search(
        r"\b(?:внешн(?:его|ий|ем) (?:диспле[йяе]|экран[ае]?)|телевизор[ае]?)\b", text
    )
    if external:
        text = text[: external.start()] + "экрана" + text[external.end() :]
    if not re.search(r"\b(?:mac|macbook|мак|макбука?|экран[ае]?|диспле[йя])\b", text):
        return None
    if not re.match(r"(?:убавь|уменьши|сделай|поставь|установи|яркость)\b", text):
        return None
    rest = re.sub(
        r"\b(?:убавь|уменьши|сделай|поставь|установи|мне|яркость|яркости|"
        r"mac|macbook|мак|макбука?|экран[ае]?|диспле[йя]|на|до|процентов|процента)\b|%",
        " ",
        text,
    )
    rest = " ".join(rest.split())
    level = (
        0
        if rest in {"минимум", "минимальную", "минимальной"}
        else 100
        if rest in {"максимум", "максимальную", "полную"}
        else 50
        if rest == "наполовину"
        else _NUMBERS.get(rest)
    )
    if re.fullmatch(r"\d{1,3}", rest):
        level = int(rest)
    if level is not None and 0 <= level <= 100:
        action = ExternalDisplayAction if external else DisplayAction
        return action(parameters=Brightness(brightness=level))
    return None


def _control_target(text):
    """Identify the grammatical object, not a color mentioned inside a saved fact."""
    text = re.sub(r"^(?:и|а)\s+", "", text.strip())
    text = re.sub(r"^(?:" + "|".join(_VERBS) + r"|открой|запусти)\s+", "", text)
    text = re.sub(r"^(?:мне|мою|мой|моей|эту)\s+", "", text)
    if re.match(
        rf"^(?:{_TARGET}|яркост[ьи]|mac|macbook|мак|экран[ае]?|телевизор|звук|громкость)\b", text
    ):
        return True
    words = text.split()
    return any(color_rgb(" ".join(words[:size])) for size in (1, 2, 3))


def compile_plan(text, *, comfortable_brightness=None, brightness_target=None):
    """Full coverage grammar: unknown words or conflicts never produce a partial plan."""
    from backend.local_commands import route
    from backend.music import parse as music_parse

    if not isinstance(text, str) or len(text) > 1000:
        return None
    command = control_text(text)
    if re.search(r"\bодновременно\b", command):
        if re.search(r"\b(?:затем|потом)\b", command):
            return ControlPlan(
                clarification="Для смешанного порядка создай сценарий с группами действий."
            )
        base = compile_plan(
            re.sub(r"\bодновременно\b\s*", "", text, flags=re.I),
            comfortable_brightness=comfortable_brightness,
            brightness_target=brightness_target,
        )
        if base is None or base.clarification:
            return base
        try:
            return ControlPlan.model_validate({**base.model_dump(), "mode": "parallel"})
        except ValidationError:
            return ControlPlan(
                clarification="Параллельные действия могут управлять одним ресурсом. "
                "Назови их последовательность; ничего не отправлял."
            )
    ordered = re.split(r"\s*,?\s+(?:а )?(?:затем|потом)\s+", command)
    if len(ordered) > 1:
        children = [
            compile_plan(
                part.rstrip(","),
                comfortable_brightness=comfortable_brightness,
                brightness_target=brightness_target,
            )
            for part in ordered
        ]
        if not any(children):
            return None
        if (
            any(child is None or child.clarification for child in children)
            or sum(len(child.actions) for child in children if child) > 8
            or re.search(r'[«»"“”`\n;]', text)
        ):
            return ControlPlan(clarification="Уточни все действия последовательности и устройства.")
        return ControlPlan(actions=[action for child in children for action in child.actions])
    if not re.match(
        r"^(?:" + "|".join(_VERBS) + rf"|открой|запусти|яркост[ьи]|громкость|{_TARGET})\b", command
    ):
        return None  # Mentioning a color in a fact, quote or question is not control.
    candidate = any(_control_target(part) for part in re.split(r"\s+(?:и|а)\s+|,\s*", command))
    if re.search(r'[«»"“”`\n;]', text) or re.search(
        r"\b(?:если|когда|потом|завтра|позже|или|либо|вместо)\b", command
    ):
        return (
            ControlPlan(clarification="Уточни одну прямую команду без условий и альтернатив.")
            if candidate
            else None
        )
    explicit_led = bool(re.search(rf"\b(?:{_TARGET})\b", command))
    explicit_display = bool(
        re.search(r"\b(?:mac|macbook|мак|макбука?|экран[ае]?|диспле[йя])\b", command)
    )
    # Device-free brightness is ambiguous even though an individual legacy parser
    # has a default. Resolve the target once before any handler gets the command.
    if (
        not explicit_led
        and not explicit_display
        and re.fullmatch(
            r"(?:(?:сделай|поставь|установи|убавь|уменьши|прибавь|увеличь) )?"
            r"яркост[ьи](?: (?:на|до))?(?: [\w %]+)?",
            command,
        )
    ):
        if brightness_target not in {"led_strip", "mac_display", "external_display"}:
            return ControlPlan(
                clarification="Яркость ленты или экрана Mac?",
                clarification_kind="brightness_target",
            )
        command += {
            "led_strip": " ленты",
            "mac_display": " экрана",
            "external_display": " внешнего дисплея",
        }[brightness_target]
        text = command
    if args := led_parse(text, comfortable_brightness=comfortable_brightness):
        action = led_action(args)
        return ControlPlan(actions=[action]) if action else None
    if (
        candidate
        and comfortable_brightness is None
        and re.search(r"не слишком ярк", command)
        and not re.search(r"\b(?:mac|мак|экран[ае]?|диспле[йя])\b", command)
    ):
        return ControlPlan(
            clarification="Какую яркость подсветки поставить в процентах? "
            "Предпочтение для «не слишком ярко» пока не задано.",
            clarification_kind="led_brightness",
        )
    if action := _display(command):
        return ControlPlan(actions=[action])
    legacy = route(text)
    if legacy and legacy[0] == "set_brightness":
        return ControlPlan(
            actions=[DisplayAction(parameters=Brightness(brightness=legacy[1]["level"]))]
        )
    if legacy and legacy[0] == "set_volume":
        return ControlPlan(actions=[VolumeAction(parameters=VolumeParameters(**legacy[1]))])
    if legacy and legacy[0] == "open_app":
        return ControlPlan(actions=[AppAction(parameters=AppParameters(**legacy[1]))])
    if re.fullmatch(r"(?:сделай )?телевизор потише|убавь громкость телевизора", command):
        return ControlPlan(actions=[TVAction()])
    parts = [
        re.sub(r"^(?:и|а)\s+", "", part.strip())
        for part in re.split(r"\s+(?:и|а)\s+|,(?!\s*\d)\s*", command)
        if part.strip()
    ]
    if not 2 <= len(parts) <= 8 or not candidate:
        if candidate and re.search(r"не слишком ярк", command):
            return ControlPlan(
                clarification="Какую яркость подсветки поставить в процентах? "
                "Предпочтение для «не слишком ярко» пока не задано."
            )
        return (
            ControlPlan(
                clarification="Уточни устройство и поддерживаемые параметры. "
                "Для субъективной яркости назови процент; ничего не отправлял."
            )
            if candidate
            else None
        )
    actions = []
    for part in parts:
        if args := led_parse(part, comfortable_brightness=comfortable_brightness):
            action = led_action(args)
        elif re.fullmatch(r"(?:сделай )?телевизор потише|убавь громкость телевизора", part):
            action = TVAction()
        elif action := _display(part):
            pass
        else:
            routed = route(part)
            if routed and routed[0] == "open_website":
                action = BrowserAction()
            elif routed and routed[0] == "set_volume":
                action = VolumeAction(parameters=VolumeParameters(**routed[1]))
            elif routed and routed[0] == "set_brightness":
                action = DisplayAction(parameters=Brightness(brightness=routed[1]["level"]))
            elif routed and routed[0] == "open_app":
                action = AppAction(parameters=AppParameters(**routed[1]))
            else:
                part = re.sub(r"^музыку (включи|поставь)$", r"\1 музыку", part)
                if music_parse(part):
                    action = MusicAction(parameters=MusicParameters(command=part))
                else:
                    return ControlPlan(
                        clarification="Не разобрал все действия или параметры. "
                        "Уточни составную команду; ничего не отправлял."
                    )
        if action is None:
            return ControlPlan(clarification="Неизвестное действие в составной команде.")
        previous = next((item for item in actions if item.device == action.device), None)
        if previous:
            if isinstance(action, LEDAction):
                merged = previous.parameters.model_dump(exclude_none=True)
                for key, value in action.parameters.model_dump(exclude_none=True).items():
                    if key in merged and merged[key] != value:
                        return ControlPlan(
                            clarification="В команде противоречивые параметры подсветки."
                        )
                    merged[key] = value
                if "color" in merged and {"brightness", "brightness_delta"} & merged.keys():
                    merged.setdefault("power", True)
                actions[actions.index(previous)] = LEDAction(parameters=LEDParameters(**merged))
            elif previous != action:
                return ControlPlan(
                    clarification="Два разных действия одного устройства: уточни порядок."
                )
        else:
            actions.append(action)
    return ControlPlan(actions=actions)


class ControlSession:
    def __init__(
        self,
        ttl_s=60,
        clock=time.monotonic,
        *,
        default_brightness_device="ask",
        ambiguity_rule="context_then_ask",
    ):
        self.clock, self.ttl_s = clock, ttl_s
        self.default_brightness_device = default_brightness_device
        self.ambiguity_rule = ambiguity_rule
        self.device = None
        self.expires = 0
        self.binding = None
        self.completed = OrderedDict()
        self.pending_brightness = None
        self.pending_target = None
        self.sent_state = {}
        self.confirmed_state = {}
        self.previous_confirmed = {}
        self.last_action = None
        self.history = []

    def plan(self, text, binding, comfortable_brightness=None):
        with measure("context_resolution"):
            return self._plan(text, binding, comfortable_brightness)

    def _plan(self, text, binding, comfortable_brightness=None):
        value = control_text(text)
        if self.binding is not None and (self.binding != binding or self.clock() >= self.expires):
            self.clear()
        fresh = self.binding == binding and self.clock() < self.expires and self.device is not None
        target = self.device if fresh and self.ambiguity_rule == "context_then_ask" else None
        if profile := current.get():
            explicit_target = re.search(
                rf"\b(?:{_TARGET}|mac|macbook|мак|макбука?|экран[ае]?|диспле[йя])\b", value
            )
            profile.update(context_used=bool(target and not explicit_target))
        if target not in {"led_strip", "mac_display", "external_display"}:
            target = self.default_brightness_device
        pending_target = self.pending_target
        if (
            pending_target
            and pending_target["binding"] == binding
            and self.clock() < pending_target["expires"]
        ):
            selected = (
                "led_strip"
                if re.fullmatch(rf"(?:для )?(?:{_TARGET})", value)
                else "mac_display"
                if re.fullmatch(r"(?:для )?(?:экран[ае]?|диспле[йя]|mac|мак)", value)
                else None
            )
            if selected:
                return compile_plan(pending_target["text"], brightness_target=selected)
        pending = self.pending_brightness
        if pending:
            number = re.sub(r"^на\s+", "", value)
            number = re.sub(r"\s*(?:%|процент(?:ов|а)?)$", "", number).strip()
            level = int(number) if re.fullmatch(r"\d{1,3}", number) else _NUMBERS.get(number)
            if level is not None and 0 <= level <= 100:
                if pending["binding"] != binding or self.clock() >= pending["expires"]:
                    return ControlPlan(
                        clarification="Уточнение истекло. Повтори просьбу об устройстве."
                    )
                return compile_plan(pending["text"], comfortable_brightness=level)
        relative = re.fullmatch(
            r"(?:(?:сделай|поставь) )?(?:(?:ее|его|это) )?(?:по)?(ярче|тусклее)", value
        )
        resume = value in {"включи обратно", "включи снова"}
        restore = re.fullmatch(r"верни (?:(?:предыдущий|прежний) (цвет|уровень)|как было)", value)
        if relative or resume or restore:
            if not fresh:
                return ControlPlan(clarification="Для какого устройства изменить состояние?")
            if restore:
                previous = self.previous_confirmed.get(self.device, {})
                params = {
                    k: v
                    for k, v in previous.items()
                    if not restore[1] or k == ("color" if restore[1] == "цвет" else "brightness")
                }
                if not params:
                    return ControlPlan(
                        clarification="Предыдущее фактическое состояние не подтверждено. "
                        "Назови нужный цвет или уровень."
                    )
                if self.device == "led_strip":
                    return ControlPlan(actions=[LEDAction(parameters=LEDParameters(**params))])
                if self.device in {"mac_display", "external_display"} and "brightness" in params:
                    action = (
                        ExternalDisplayAction
                        if self.device == "external_display"
                        else DisplayAction
                    )
                    return ControlPlan(
                        actions=[action(parameters=Brightness(brightness=params["brightness"]))]
                    )
                return ControlPlan(
                    clarification="Не могу достоверно восстановить предыдущий параметр "
                    "этого устройства."
                )
            if self.device != "led_strip":
                return ControlPlan(
                    clarification="Назови абсолютную яркость выбранного экрана в процентах."
                )
            return ControlPlan(
                actions=[
                    LEDAction(
                        parameters=LEDParameters(
                            **(
                                {"power": True}
                                if resume
                                else {"brightness_delta": 10 if relative[1] == "ярче" else -10}
                            )
                        )
                    )
                ]
            )
        continuation = re.match(r"^(?:нет[, ]+лучше|лучше|и яркость)\b", value)
        if continuation:
            if self.binding != binding or self.clock() >= self.expires or self.device is None:
                return ControlPlan(clarification="Для какого устройства изменить яркость?")
            rest = re.sub(r"^(?:нет[, ]+лучше|лучше|и яркость)\s*", "", value)
            if self.device == "mac_sound":
                text = f"Поставь громкость {rest}"
            else:
                target = (
                    "ленты"
                    if self.device == "led_strip"
                    else "внешнего дисплея"
                    if self.device == "external_display"
                    else "Mac"
                )
                text = f"Поставь яркость {target} {rest}"
        if (
            self.device == "mac_sound"
            and self.binding == binding
            and self.clock() < self.expires
            and re.fullmatch(r"(?:еще |ещё )?(?:по)?(?:тише|громче)", value)
        ):
            text = "Сделай звук " + ("тише" if "тише" in value else "громче")
        try:
            with measure("intent_parsing"):
                return compile_plan(
                    control_text(text),
                    comfortable_brightness=comfortable_brightness,
                    brightness_target=target,
                )
        except ValueError:
            return ControlPlan(clarification="Параметры противоречат друг другу. Уточни команду.")

    def clear(self, *, keep_state=False):
        self.device = None
        self.expires = 0
        self.binding = None
        self.pending_brightness = None
        self.pending_target = None
        if not keep_state:
            self.sent_state.clear()
            self.confirmed_state.clear()
            self.previous_confirmed.clear()
            self.last_action = None

    async def _run_actions(self, plan, conv, context, key):
        """Own every started branch until it settles, including during cancellation."""
        started, finished, tasks = {}, {}, []

        async def invoke(index, action):
            with measure("tool_routing"):
                name, arguments = action.call()
            started[index] = (action, name, arguments)
            if profile := current.get():
                profile.update(intent="device_control", target=action.device, tool=name)
            if name is None or name not in conv.tools:
                result = {
                    "error": "Управление громкостью телевизора не подключено."
                    if action.device == "tv"
                    else "Управление подсветкой отключено в настройках."
                    if action.device == "led_strip"
                    else "Инструмент устройства отключён или недоступен."
                }
            else:
                if conv.on_tool:
                    conv.on_tool(name)
                grant = planned_operation.set(
                    (context.request_id, name, json.dumps(arguments, sort_keys=True))
                )
                try:
                    result = json.loads(await conv.tools.call(name, arguments))
                finally:
                    planned_operation.reset(grant)
            if not isinstance(result, dict):
                result = {"error": "Инструмент не вернул структурированный результат."}
            finished[index] = result
            return action, name, arguments, result

        try:
            if plan.mode == "parallel":
                tasks = [
                    asyncio.create_task(invoke(index, action))
                    for index, action in enumerate(plan.actions)
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                failure = next((r for r in results if isinstance(r, BaseException)), None)
                if failure is not None:
                    raise failure
                for result in results:
                    yield result
            else:
                for index, action in enumerate(plan.actions):
                    yield await invoke(index, action)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            from backend.tools.registry import result_status

            observed = []
            for index, (action, name, arguments) in sorted(started.items()):
                result = finished.get(index, {"status": "interrupted"})
                canonical = (
                    result_status(name, arguments, result) if index in finished else "unverified"
                )
                observed.append(
                    {
                        "device": action.device,
                        "tool": name,
                        "arguments": arguments,
                        "result": result,
                        "status": {"success": "confirmed", "failed": "failed"}.get(
                            canonical, "unverified"
                        ),
                        "result_status": canonical,
                    }
                )
            self.clear()
            self.completed[key] = {
                "message": "Ожидание команды отменено или прервано. "
                "Уже отправленные действия не отменяются; автоповтора нет.",
                "steps": observed,
                "status": "unverified",
                "result_status": "unverified",
                "plan": plan.model_dump(mode="json", exclude_none=True),
            }
            raise

    async def execute(self, plan, conv):
        context = request_context.get()
        binding = (context.binding, conv.id) if context else None
        expected = (
            self.plan(context.text, binding, conv.s.led.comfortable_brightness) if context else None
        )
        if context is None or not context.device.authenticated or expected != plan:
            return {
                "error": "План не совпадает с исходным запросом авторизованной сессии.",
                "steps": [],
            }
        if (
            not voice_addressed.get()
            and voice_session.get() != context.binding
            and context.text
            not in {
                voice_led_command.get(),
                voice_music_command.get(),
                voice_control_command.get(),
            }
        ):
            return {"error": "Для выполнения нужен прямой запрос к Чушпану.", "steps": []}
        if plan.clarification:
            if profile := current.get():
                profile.update(intent="device_control", target="ambiguous", reason="clarification")
            self.clear()
            if plan.clarification_kind == "led_brightness":
                self.pending_brightness = {
                    "text": context.text,
                    "binding": binding,
                    "expires": self.clock() + self.ttl_s,
                }
            elif plan.clarification_kind == "brightness_target":
                self.pending_target = {
                    "text": context.text,
                    "binding": binding,
                    "expires": self.clock() + self.ttl_s,
                }
            return {"message": plan.clarification, "steps": [], "status": "clarification"}
        key = (context.binding, context.request_id)
        if key in self.completed:
            previous = self.completed[key].get("plan")
            if previous is not None and previous != plan.model_dump(mode="json", exclude_none=True):
                return {
                    "error": "Повторный идентификатор события относится к другой команде.",
                    "steps": [],
                }
            return {**self.completed[key], "replayed": True}
        self.completed[key] = {
            "message": "Эта команда уже выполняется; повторно не запускаю.",
            "steps": [],
            "plan": plan.model_dump(mode="json", exclude_none=True),
        }
        while len(self.completed) > 64:
            self.completed.popitem(last=False)
        steps, replies = [], []
        # Only an explicit parallel plan can overlap handlers. Results and history
        # retain the user's order even if the independent branches finish out of order.
        async for action, name, arguments, result in self._run_actions(plan, conv, context, key):
            status = (
                "partial"
                if result.get("delivery_state") == "partial"
                else "failed"
                if result.get("error")
                else "sent"
                if result.get("written") or result.get("submitted")
                else "confirmed"
                if (
                    result.get("device_state_confirmed")
                    or result.get("physical_confirmed")
                    or name == "set_brightness"
                    and result.get("level") == arguments.get("level")
                    or name == "set_volume"
                    and result.get("ok") is True
                    and isinstance(result.get("volume_percent"), int)
                    and ("level" not in arguments or result["volume_percent"] == arguments["level"])
                    or name == "music_command"
                    and result.get("ok")
                    and (
                        result.get("state") == "paused" or result.get("playback_confirmed") is True
                    )
                )
                else "unverified"
            )
            from backend.tools.registry import result_status

            canonical = result.get("result_status") or result_status(name, arguments, result)
            if canonical == "failed":
                status = "failed"
            steps.append(
                {
                    "device": action.device,
                    "tool": name,
                    "arguments": arguments,
                    "result": result,
                    "status": status,
                    "result_status": canonical,
                }
            )
            self.last_action = {
                "device": action.device,
                "tool": name,
                "status": canonical,
                "updated_at": self.clock(),
            }
            if status == "partial":
                self.confirmed_state.pop(action.device, None)
                self.previous_confirmed.pop(action.device, None)
            if status in {"sent", "confirmed"}:
                params = action.parameters.model_dump(exclude_none=True)
                self.sent_state[action.device] = {
                    **self.sent_state.get(action.device, {}),
                    **params,
                }
                if status == "confirmed":
                    self.previous_confirmed[action.device] = dict(
                        self.confirmed_state.get(action.device, {})
                    )
                    self.confirmed_state[action.device] = {
                        **self.confirmed_state.get(action.device, {}),
                        **params,
                    }
                else:
                    # A newer unverified write invalidates knowledge of the affected
                    # fields. Keep the sent values separate from physical evidence.
                    known = self.confirmed_state.get(action.device, {})
                    for field in params:
                        known.pop(field, None)
                    if "brightness_delta" in params:
                        known.pop("brightness", None)
                    self.previous_confirmed.pop(action.device, None)
            replies.append(
                result.get("error")
                or conv.tools.direct_reply(name, result)
                or "Результат действия проверить не удалось."
            )
        good = any(step["status"] in {"sent", "confirmed", "partial"} for step in steps)
        keep_context = (
            len(plan.actions) == 1
            and good
            and plan.actions[0].device
            in {"led_strip", "mac_display", "mac_sound", "external_display"}
        )
        self.clear(keep_state=keep_context)
        if keep_context:
            self.device, self.expires, self.binding = (
                plan.actions[0].device,
                self.clock() + self.ttl_s,
                binding,
            )
        receipt = {
            "message": " ".join(replies),
            "steps": steps,
            "status": "partial"
            if good and any(s["status"] in {"failed", "partial"} for s in steps)
            else "failed"
            if any(s["status"] in {"failed", "partial"} for s in steps)
            else "unverified"
            if any(s["status"] == "unverified" for s in steps)
            else "confirmed"
            if all(s["status"] == "confirmed" for s in steps)
            else "sent",
            "plan": plan.model_dump(mode="json", exclude_none=True),
        }
        receipt["result_status"] = (
            "partial_success"
            if receipt["status"] == "partial"
            else "failed"
            if receipt["status"] == "failed"
            else "success"
            if receipt["status"] == "confirmed"
            else "unverified"
        )
        self.history.append(
            {
                "at": time.time(),
                "result_status": receipt["result_status"],
                "devices": [a.device for a in plan.actions],
                "tools": [s["tool"] for s in steps if s["tool"]],
            }
        )
        self.history[:] = self.history[-30:]
        if profile := current.get():
            profile.update(
                result_status=receipt["result_status"],
                target=plan.actions[0].device if len(plan.actions) == 1 else "multiple",
            )
        self.completed[key] = receipt
        return receipt
