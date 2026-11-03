"""Local ELK-BLEDOB control. Firmware experiments are never exposed as tools."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from bleak import BleakClient, BleakScanner
from bleak.exc import (
    BleakBluetoothNotAvailableError,
    BleakBluetoothNotAvailableReason,
    BleakDeviceNotFoundError,
    BleakError,
)
from pydantic import BaseModel, Field, ValidationError

from backend.config import LEDConfig, Settings
from backend.latency import measure
from backend.security import Risk, command_text, control_text
from backend.tools.registry import DeviceUnavailable, Tool, ToolError

log = logging.getLogger(__name__)

COLORS = {
    "красный": (255, 0, 0),
    "зелёный": (0, 255, 0),
    "синий": (0, 0, 255),
    "фиолетовый": (255, 0, 200),
    "розовый": (255, 0, 32),
    "светло-розовый": (255, 16, 32),
    "тёплый белый": (255, 48, 32),
    "белый": (255, 255, 255),
    "жёлтый": (255, 255, 0),
    "оранжевый": (255, 100, 0),
    "голубой": (0, 180, 255),
}
# Device aliases are local to this parser; never rewrite the trusted user request.
_TARGET = (
    r"(?:(?:led|лед|лет|светодиодн[а-я]+)[- ]*)?лент[ауыеой]*"
    r"|подсветк[ауиеой]*|свет[ауе]?|led|лед|лет"
)
_VERBS = {
    "сделай": "set",
    "сделаем": "set",
    "поставь": "set",
    "установи": "set",
    "хочу": "want",
    "включи": "on",
    "зажги": "on",
    "выключи": "off",
    "погаси": "off",
    "отключи": "off",
    "убавь": "lower",
    "уменьши": "lower",
    "прибавь": "raise",
    "увеличь": "raise",
    "вруби": "on",
    "выруби": "off",
    "поменяй": "set",
    "измени": "set",
}
_UNITS = "ноль один два три четыре пять шесть семь восемь девять".split()
_TEENS = "десять одиннадцать двенадцать тринадцать четырнадцать пятнадцать шестнадцать семнадцать восемнадцать девятнадцать".split()  # noqa: E501
_TENS = "двадцать тридцать сорок пятьдесят шестьдесят семьдесят восемьдесят девяносто".split()
_NUMBERS = {word: i for i, word in enumerate(_UNITS + _TEENS)}
for _i, _word in enumerate(_TENS, 2):
    _NUMBERS[_word] = _i * 10
    for _j, _unit in enumerate(_UNITS[1:], 1):
        _NUMBERS[f"{_word} {_unit}"] = _i * 10 + _j
_NUMBERS["сто"] = 100


def color_rgb(name: str) -> tuple[int, int, int] | None:
    name = re.sub(r"розовеньк(?:ий|ая|ую|ой|им)", "розовый", name)
    name = name.replace("нежно-", "светло-").replace("нежно ", "светло-")
    name = name.replace("ё", "е").replace("светло ", "светло-").strip()
    name = re.sub(r"^светл(?:ый|ая|ую|ой|ым) (розов(?:ый|ая|ую|ой|ым))$", r"светло-\1", name)
    for color, rgb in COLORS.items():
        base = color.replace("ё", "е")
        forms = {base}
        if base.endswith("ый"):
            forms |= {base[:-2] + ending for ending in ("ая", "ую", "ой", "ым")}
        if base == "синий":
            forms |= {"синяя", "синюю", "синей", "синим"}
        if base == "теплый белый":
            forms |= {"теплой белой", "теплым белым", "теплую белую"}
        if name in forms:
            return rgb
    return None


def _parse_single(text: str) -> dict | None:
    """Parse a single, direct LED intent from grammatical parts, not word order.

    The configured LED is the default for bare color/percent/relative requests.
    Unqualified imperative "установи яркость 25%" keeps its existing Mac meaning.
    All remaining words must be understood; negation, sources and other devices fail closed.
    """
    if not isinstance(text, str) or len(text) > 1000 or re.search(r'[«»"“”`\n;]', text):
        return None
    text = control_text(text)
    text = re.sub(r"[‐‑‒–—]", "-", text)
    # A frustrated lead-in is not the action. Accept the explicit final clause only
    # after "ладно", when the discarded part contains no device/action instructions.
    if match := re.fullmatch(r"(.{0,220}?[.!?] )?ладно[, ]+(.+)", text):
        lead = match[1] or ""
        if re.search(rf"\b(?:{_TARGET}|не|включи|выключи|сделай|поставь)\b", lead):
            return None
        text = match[2]
    text = text.rstrip(".!? ")
    if re.search(r"\b(?:не|нет|нельзя|если|когда|потом|завтра|позже|или|либо|вместо)\b", text):
        return None
    text = re.sub(r"[, ]+пожалуйста$", "", text)
    if re.fullmatch(rf"(?:статус|состояние|проверь состояние) (?:{_TARGET})", text):
        return {"action": "status"}
    first, _, rest = text.partition(" ")
    verb = _VERBS.get(first)
    body = rest if verb else text
    body = re.sub(r"\b(?:мне|мою|моя|мой|моей|моего|эту|этой)\b", " ", body)
    targets = list(re.finditer(rf"\b(?:{_TARGET})\b", body))
    if len(targets) > 1:
        return None
    targeted = bool(targets)
    if targeted:
        body = re.sub(rf"\b(?:{_TARGET})\b", " ", body)
    body = " ".join(body.split())
    if not body and targeted and verb in {"on", "off"}:
        return {"action": verb}
    if verb in {"off", "want"} and not (verb == "want" and targeted):
        return None
    if not targeted and not verb and not re.match(r"яркость\b", body):
        return None
    # Only grammatical particles are discarded, not arbitrary user words.
    body = re.sub(r"\b(?:на|цвет|цвета|цветом)\b", " ", body)
    body = " ".join(body.split())
    if verb in {"lower", "raise"} and body in {"яркость", ""} and (targeted or body):
        return {"action": "тусклее" if verb == "lower" else "ярче"}
    if verb == "set" and body in {"поярче", "ярче", "потусклее", "тусклее"}:
        return {"action": "ярче" if body in {"поярче", "ярче"} else "тусклее"}
    has_brightness = bool(re.search(r"\bяркост[ьи]\b", body))
    percent = bool(re.search(r"%|\bпроцент(?:ов|а)?\b", body))
    level_text = re.sub(r"\b(?:яркост[ьи]|процент(?:ов|а)?)\b|%", " ", body)
    level_text = " ".join(level_text.split())
    if (targeted and (has_brightness or percent) and verb in {"set", "on", None}) or (
        not targeted and verb is None and has_brightness
    ):
        level = (
            int(level_text) if re.fullmatch(r"\d{1,3}", level_text) else _NUMBERS.get(level_text)
        )
        if level is not None and 0 <= level <= 100:
            return {"action": "brightness", "level": level}
        return None
    if verb not in {"set", "on", "want", None} or (not targeted and verb is None):
        return None
    if rgb := color_rgb(body):
        return {"action": "color", "rgb": list(rgb)}
    raw = re.fullmatch(r"rgb (\d{1,3})[ ,]+(\d{1,3})[ ,]+(\d{1,3})", body)
    if targeted and raw and all(int(v) <= 255 for v in raw.groups()):
        return {"action": "color", "rgb": [int(v) for v in raw.groups()]}
    return None


def _combined(text: str, comfortable_brightness: int | None) -> dict | None:
    body = control_text(text)
    body = re.sub(r"[‐‑‒–—]", "-", body)
    body = re.sub(r"тепл(?:ый|ую|ой|ым) свет\b", "теплый белый", body)
    subjective = bool(re.search(r"(?:только )?не слишком ярк(?:ий|ую|ой|о)", body))
    if subjective:
        body = re.sub(r"(?:только )?не слишком ярк(?:ий|ую|ой|о)", "", body)
    if re.search(r"\b(?:не|нет|нельзя|если|когда|потом|завтра|позже|или|либо|вместо)\b", body):
        return None
    targeted = bool(re.search(rf"\b(?:{_TARGET})\b", body))
    verbs = re.findall(r"\b(?:" + "|".join(_VERBS) + r")\b", body)
    if not targeted and not verbs and not re.match(r"яркость\b", body):
        return None
    power = {_VERBS[v] for v in verbs} & {"on", "off"}
    if len(power) > 1:
        return None
    body = re.sub(rf"\b(?:{_TARGET})\b", " ", body)
    body = re.sub(r"\b(?:" + "|".join(_VERBS) + r")\b", " ", body)
    body = re.sub(r"\b(?:мне|мою|моя|мой|моей|моего|эту|этой|пожалуйста)\b", " ", body)
    body = " ".join(body.split())
    colors = []
    raw = re.search(r"\brgb (\d{1,3})[ ,]+(\d{1,3})[ ,]+(\d{1,3})", body)
    if raw:
        rgb = tuple(map(int, raw.groups()))
        if not targeted or max(rgb) > 255:
            return None
        colors.append(rgb)
        body = body[: raw.start()] + " " + body[raw.end() :]
    body = body.replace(",", " ")
    words = body.split()
    consumed = set()
    for size in (3, 2, 1):
        for start in range(len(words) - size + 1):
            indexes = set(range(start, start + size))
            if not indexes & consumed and (rgb := color_rgb(" ".join(words[start : start + size]))):
                colors.append(rgb)
                consumed |= indexes
    if len(set(colors)) > 1:
        return None
    body = " ".join(word for i, word in enumerate(words) if i not in consumed)
    levels = []
    for phrase, value in [(r"на максимум|на полную|сотка|сотку", 100), (r"наполовину", 50)]:
        if re.search(rf"\b(?:{phrase})\b", body):
            levels.append(value)
            body = re.sub(rf"\b(?:{phrase})\b", " ", body)
    for number in sorted(_NUMBERS, key=len, reverse=True):
        pattern = rf"\b{number}\b"
        for _ in re.findall(pattern, body):
            levels.append(_NUMBERS[number])
        body = re.sub(pattern, " ", body)
    numeric = re.findall(r"\b\d+\b", body)
    levels.extend(map(int, numeric))
    if subjective and not levels:
        if comfortable_brightness is None:
            return None
        levels.append(comfortable_brightness)
    has_unit = bool(re.search(r"яркост|процент|%", body))
    if numeric and not (has_unit or colors):
        return None
    body = re.sub(r"\b\d+\b", " ", body)
    if len(set(levels)) > 1 or any(not 0 <= value <= 100 for value in levels):
        return None
    if not targeted and not colors and levels and verbs and not power:
        return None  # Preserve the existing unqualified Mac brightness imperative.
    relative = []
    if re.search(r"поярче|\bярче\b", body) or any(_VERBS[v] == "raise" for v in verbs):
        relative.append("ярче")
    if re.search(r"потусклее|\bтусклее\b", body) or any(_VERBS[v] == "lower" for v in verbs):
        relative.append("тусклее")
    if len(set(relative)) > 1:
        return None
    body = re.sub(
        r"\b(?:на|до|и|а|цвет|цвета|цветом|яркост[ьи]|процент(?:ов|а)?|"
        r"поярче|ярче|потусклее|тусклее)\b|%|,",
        " ",
        body,
    )
    if body.strip() or ("off" in power and (colors or levels or relative)):
        return None
    commands = []
    if power:
        commands.append({"action": next(iter(power))})
    elif colors and (levels or relative):
        commands.append({"action": "on"})
    if colors:
        commands.append({"action": "color", "rgb": list(colors[0])})
    if levels:
        commands.append({"action": "brightness", "level": levels[0]})
    elif relative:
        commands.append({"action": relative[0]})
    if not commands:
        return None
    return commands[0] if len(commands) == 1 else {"action": "sequence", "commands": commands}


def parse(text: str, *, comfortable_brightness: int | None = None) -> dict | None:
    """Resolve the complete request before executing any same-device sequence."""
    if not isinstance(text, str) or len(text) > 1000 or re.search(r'[«»"“”`\n;]', text):
        return None
    single = _parse_single(text)
    if single is not None or not isinstance(text, str):
        return single
    return _combined(text, comfortable_brightness)


def diagnostic(stage: str, outcome="ok", **values) -> None:
    """No transcripts, BLE identifiers, credentials or model arguments in diagnostics."""
    from backend.latency import current

    profile = current.get()
    log.info(
        "LED_STAGE stage=%s outcome=%s request_id=%s %s",
        stage,
        outcome,
        profile.request_id if profile else "local",
        " ".join(f"{key}={value}" for key, value in values.items()),
    )
    if profile:
        identity = profile.begin("led_" + stage, device="led_strip", **values)
        profile.end(identity, outcome)


def intent_fields(args: dict) -> dict:
    names = {
        "color": "set_color",
        "brightness": "set_brightness",
        "ярче": "increase_brightness",
        "тусклее": "decrease_brightness",
    }
    value = {"action": names.get(args["action"], args["action"])}
    if args["action"] == "color":
        value["rgb"] = "/".join(map(str, args["rgb"]))
    if args["action"] == "brightness":
        value["brightness"] = args["level"]
    return value


def authorize(arguments: dict, original: str, *, comfortable_brightness=None) -> bool:
    from backend.security import ActionDenied

    intent = parse(original, comfortable_brightness=comfortable_brightness)
    proposed = parse(arguments["command"], comfortable_brightness=comfortable_brightness)
    matched = intent is not None and intent == proposed
    diagnostic(
        "authorization",
        "allowed" if matched else "denied",
        authority="original_user",
        arguments_match=matched,
    )
    if not matched:
        raise ActionDenied(
            "Команда инструмента отличается от исходной; "
            "нужна явная просьба именно об этом действии."
            if intent is not None
            else clarification()
        )
    return True


def request_candidate(text: str) -> bool:
    """Only for a local clarification; this predicate grants no device action."""
    command = command_text(text)
    return bool(
        re.match(r"(?:сделай|сделаем|включи|выключи|поставь|хочу|установи)\b", command)
        and re.search(rf"\b(?:{_TARGET})\b", command)
    )


def clarification() -> str:
    return (
        "Команду подсветки не разобрал однозначно и не отправлял. "
        "Можно сказать «Сделай ленту розовой» или «Яркость 30 процентов». "
        "Уточни неизвестные параметры; противоречивые действия не выполняю."
    )


def packet(action: str, *, rgb=None, level=None) -> bytes:
    """Frames stay gated by the local firmware-specific observation evidence."""
    if action == "color" and isinstance(rgb, (list, tuple)) and len(rgb) == 3:
        if all(type(v) is int and 0 <= v <= 255 for v in rgb):
            return bytes([0x7E, 7, 5, 3, *rgb, 0x10, 0xEF])
    elif action in {"on", "off"} and rgb is None and level is None:
        value = int(action == "on")
        return bytes([0x7E, 4, 4, value, 0, value, 0xFF, 0, 0xEF])
    elif action == "brightness" and type(level) is int and 0 <= level <= 100:
        return bytes([0x7E, 4, 1, level, 0xFF, 0xFF, 0xFF, 0, 0xEF])
    raise ToolError("Нужны RGB 0–255 или яркость 0–100; неизвестный режим подсветки.")


def _key(args: dict) -> str:
    if args["action"] == "color":
        return "color:" + ",".join(map(str, args["rgb"]))
    return args["action"]


def _description(args: dict) -> str:
    if args["action"] == "color":
        rgb = tuple(args["rgb"])
        return next((name for name, value in COLORS.items() if value == rgb), f"цвет RGB {rgb}")
    if args["action"] == "brightness":
        return f"яркость {args['level']}%"
    return {"on": "включение", "off": "выключение"}.get(args["action"], args["action"])


class LEDState(BaseModel):
    identity: dict[str, str]
    confirmed: set[str] = Field(default_factory=set)
    last_write: dict | None = None
    pending_probe: dict | None = None
    last_transaction: dict | None = None
    brightness: int | None = Field(default=None, ge=0, le=100)


class LEDStrip:
    def __init__(self, settings: Settings):
        self.cfg = settings.led
        self.path = settings.data_path / "led" / "device.json"
        self.lock = asyncio.Lock()

    @property
    def identity(self) -> dict[str, str]:
        return {
            key: str(getattr(self.cfg, key))
            for key in (
                "name",
                "firmware",
                "address",
                "service",
                "write_characteristic",
                "notify_characteristic",
            )
        }

    def _load(self) -> LEDState:
        try:
            state = LEDState.model_validate_json(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            known = self.identity == {key: str(getattr(LEDConfig(), key)) for key in self.identity}
            state = LEDState(
                identity=self.identity, confirmed={"color:255,0,0"} if known else set()
            )
        except (OSError, UnicodeError, ValidationError):
            raise ToolError(
                "Настройки LED недоступны или повреждены; команды не отправлял."
            ) from None
        if state.identity != self.identity:
            # Evidence belongs to this exact device, firmware and GATT profile.
            state = LEDState(identity=self.identity, confirmed=set())
        return state

    def _save(self, state: LEDState) -> None:
        temporary = self.path.with_suffix("." + uuid4().hex + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(state.model_dump_json(indent=2), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(self.path)
        except OSError:
            raise ToolError(
                "Не удалось сохранить настройки LED. Если запись уже началась, "
                "команда могла выполниться; проверь ленту. Автоповтора нет."
            ) from None
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)

    @asynccontextmanager
    async def _exclusive(self):
        async with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = self.path.with_suffix(".lock").open("a")
            except OSError:
                raise ToolError("Локальный каталог LED недоступен.") from None
            try:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ToolError(
                        "Лента занята другой локальной командой; попробуй позже."
                    ) from None
                yield
            finally:
                handle.close()

    @staticmethod
    def _allowed(state: LEDState, args: dict) -> bool:
        return _key(args) in state.confirmed or (
            args["action"] == "color" and "rgb" in state.confirmed
        )

    async def _disconnect(self, client) -> None:
        try:
            async with asyncio.timeout(3):
                await client.disconnect()
        except Exception:
            log.warning("LED disconnect failed; next request will use a new connection")

    async def _write(self, data: bytes, state: LEDState, args: dict) -> None:
        # Reconnect only before write. After write starts, delivery is ambiguous: no replay.
        client = None
        write_started = False
        try:
            for attempt in range(2):
                client = BleakClient(str(self.cfg.address), timeout=self.cfg.connect_timeout_s)
                try:
                    with measure("ble_connect", device="led_strip", attempt=attempt + 1):
                        async with asyncio.timeout(self.cfg.connect_timeout_s):
                            await client.connect()
                    break
                except BleakBluetoothNotAvailableError:
                    raise
                except BleakDeviceNotFoundError:
                    raise
                except (BleakError, OSError, TimeoutError):
                    await self._disconnect(client)
                    client = None
                    if attempt:
                        raise
            service = client.services.get_service(str(self.cfg.service))
            char = (
                service.get_characteristic(str(self.cfg.write_characteristic)) if service else None
            )
            if char is None or "write-without-response" not in char.properties:
                raise ToolError("Ожидаемый GATT service/write characteristic LED не найден.")
            # Some controllers drop writes made immediately after service discovery.
            await asyncio.sleep(self.cfg.settle_s)
            state.last_write = {"status": "unknown", "requested": args}
            state.pending_probe = None
            if args["action"] == "brightness":
                state.brightness = None
            self._save(state)
            diagnostic("delivery", "write_started", delivery_state="requested")
            write_started = True
            with measure("ble_write", device="led_strip"):
                async with asyncio.timeout(self.cfg.write_timeout_s):
                    await client.write_gatt_char(char, data, response=False)
            diagnostic("delivery", "sent", delivery_state="sent", physical_confirmed=False)
            # Keep the link alive while the controller consumes the queued write.
            await asyncio.sleep(self.cfg.settle_s)
        except BleakBluetoothNotAvailableError as exc:
            reasons = {
                BleakBluetoothNotAvailableReason.POWERED_OFF: "Bluetooth выключен на Mac.",
                BleakBluetoothNotAvailableReason.NO_BLUETOOTH: "Bluetooth-адаптер недоступен.",
                BleakBluetoothNotAvailableReason.NO_BLE_CENTRAL_ROLE: "Нет поддержки BLE.",
                BleakBluetoothNotAvailableReason.DENIED_BY_USER: "Запрещено пользователем macOS.",
                BleakBluetoothNotAvailableReason.DENIED_BY_SYSTEM: "Запрещено системой macOS.",
            }
            unavailable = not write_started and exc.reason in {
                BleakBluetoothNotAvailableReason.POWERED_OFF,
                BleakBluetoothNotAvailableReason.NO_BLUETOOTH,
                BleakBluetoothNotAvailableReason.NO_BLE_CENTRAL_ROLE,
            }
            failure = DeviceUnavailable if unavailable else ToolError
            raise failure(
                "Bluetooth недоступен. "
                + reasons.get(exc.reason, "Система не сообщила точную причину недоступности.")
            ) from None
        except BleakDeviceNotFoundError:
            failure = ToolError if write_started else DeviceUnavailable
            raise failure(
                "LED-лента не найдена по сохранённому BLE UUID. Проверь питание и близость; "
                "после смены UUID выполни python -m backend led discover и обнови led.address. "
                "Другую ленту автоматически не выбираю."
            ) from None
        except (BleakError, OSError, TimeoutError):
            raise ToolError(
                "Bluetooth: отключение, ошибка или тайм-аут LED. Выполнение не подтверждено; "
                "автоповтора записи нет. Следующая просьба подключится заново."
            ) from None
        except ToolError:
            raise
        except Exception as exc:
            # Platform backends can raise native errors outside BleakError.
            log.warning("LED backend failed type=%s", type(exc).__name__)
            raise ToolError(
                "Ошибка Bluetooth LED; выполнение неизвестно. Проверь ленту; автоповтора нет."
            ) from None
        finally:
            if client is not None:
                await self._disconnect(client)

    async def execute(self, *, action: str, rgb=None, level=None, experiment=False) -> dict:
        diagnostic("request", action="set_color" if action == "color" else action)
        if not self.cfg.enabled:
            raise DeviceUnavailable("LED-лента отключена в настройках.")
        async with self._exclusive():
            return await self._execute_locked(
                self._load(), action=action, rgb=rgb, level=level, experiment=experiment
            )

    async def _execute_locked(self, state, *, action, rgb=None, level=None, experiment=False):
        if action == "status":
            last = state.last_write
            status = "Команд подсветки в журнале пока нет"
            if last:
                status = "Последняя команда подсветки: " + _description(last["requested"])
                status += (
                    "; запись завершилась"
                    if last["status"] == "written"
                    else "; результат записи неизвестен"
                )
            return {
                "confirmed": sorted(state.confirmed),
                "last_write": state.last_write,
                "last_transaction": state.last_transaction,
                "message": status + ". Фактическое состояние ленты не считывал.",
            }
        if action in {"ярче", "тусклее"}:
            if state.brightness is None:
                raise ToolError("Сначала задай абсолютную яркость подсветки, например 50%.")
            level = max(0, min(100, state.brightness + (10 if action == "ярче" else -10)))
            action = "brightness"
        data = packet(action, rgb=rgb, level=level)
        args = {"action": action}
        if action == "color":
            args["rgb"] = list(rgb)
        elif action == "brightness":
            args["level"] = level
        if not experiment and not self._allowed(state, args):
            raise ToolError(
                "Этот режим LED ещё не проверен на вашей прошивке. "
                "Команду не отправлял. Нужна ручная проверка: python -m backend.led_cli probe."
            )
        await self._write(data, state, args)
        state.last_write = {
            "status": "written",
            "requested": args,
            "packet": data.hex(" "),
            "time": datetime.now(UTC).isoformat(),
            "physical_confirmed": False,
            "delivery_state": "sent",
        }
        if action == "brightness":
            state.brightness = level
        if experiment:
            state.pending_probe = {"id": uuid4().hex, "requested": args}
        self._save(state)
        diagnostic("result", "unverifiable", delivery_state="sent", physical_confirmed=False)
        return {
            "written": True,
            "physical_confirmed": False,
            "device_state_confirmed": False,
            "delivery_state": "sent",
            "verification": "unsupported",
            "probe_id": state.pending_probe["id"] if state.pending_probe else None,
            "message": "Команда подсветки отправлена по Bluetooth: "
            + _description(args)
            + ". Физическое выполнение не подтверждено.",
        }

    async def confirm_probe(self, probe_id: str, observed: bool) -> None:
        """Called only by the local experiment CLI, after a human observes the strip."""
        async with self._exclusive():
            state = self._load()
            probe = state.pending_probe
            if not probe or probe["id"] != probe_id:
                raise ToolError("Проверка устарела или запись не завершилась; режим не разрешён.")
            key = _key(probe["requested"])
            if observed:
                state.confirmed.add(key)
                if (
                    probe["requested"]["action"] == "color"
                    and {"color:255,0,0", "color:0,255,0", "color:0,0,255"} <= state.confirmed
                ):
                    state.confirmed.add("rgb")
            else:
                state.confirmed.discard(key)
                if probe["requested"]["action"] == "color":
                    state.confirmed.discard("rgb")
                if probe["requested"]["action"] == "brightness":
                    state.brightness = None
            state.last_write["physical_confirmed"] = observed
            state.pending_probe = None
            self._save(state)

    async def command(self, command: str) -> dict:
        args = parse(command, comfortable_brightness=self.cfg.comfortable_brightness)
        if args is None:
            raise ToolError("Укажи режим, цвет или яркость именно подсветки.")
        if args["action"] != "sequence":
            return await self.execute(**args)
        if not self.cfg.enabled:
            raise DeviceUnavailable("LED-лента отключена в настройках.")
        async with self._exclusive():
            state = self._load()
            # Preflight every operation before the first write. Relative brightness
            # is computed while holding the same device lock as the complete batch.
            simulated = state.model_copy(deep=True)
            commands = []
            for item in args["commands"]:
                item = dict(item)
                if item["action"] in {"ярче", "тусклее"}:
                    if simulated.brightness is None:
                        raise ToolError("Сначала задай абсолютную яркость подсветки, например 50%.")
                    delta = 10 if item["action"] == "ярче" else -10
                    item = {
                        "action": "brightness",
                        "level": max(0, min(100, simulated.brightness + delta)),
                    }
                packet(item["action"], rgb=item.get("rgb"), level=item.get("level"))
                if not self._allowed(simulated, item):
                    raise ToolError(
                        "В составной команде есть непроверенный режим LED. "
                        "Ни одной команды не отправлял."
                    )
                if item["action"] == "brightness":
                    simulated.brightness = item["level"]
                commands.append(item)
            return await self._transaction(state, commands)

    async def _transaction(self, state, commands):
        state.last_transaction = {
            "id": uuid4().hex,
            "time": datetime.now(UTC).isoformat(),
            "delivery_state": "requested",
            "physical_confirmed": False,
            "steps": [
                {"requested": command, "delivery_state": "not_started"} for command in commands
            ],
        }
        self._save(state)
        results = []
        for index, action in enumerate(commands):
            step = state.last_transaction["steps"][index]
            step["delivery_state"] = "requested"
            self._save(state)
            try:
                results.append(await self._execute_locked(state, **action))
                step["delivery_state"] = "sent"
                self._save(state)
            except asyncio.CancelledError:
                step["delivery_state"] = "unverified"
                state.last_transaction["delivery_state"] = "partial" if results else "unverified"
                with contextlib.suppress(ToolError):
                    self._save(state)
                raise
            except ToolError as exc:
                step["delivery_state"] = "unverified"
                state.last_transaction["delivery_state"] = "partial" if results else "failed"
                with contextlib.suppress(ToolError):
                    self._save(state)
                diagnostic("result", "failed", **intent_fields(action))
                if not results:
                    raise
                return {
                    "written": False,
                    "physical_confirmed": False,
                    "delivery_state": "partial",
                    "device_state_confirmed": False,
                    "results": results,
                    "failed_command": action,
                    "skipped_commands": commands[len(results) + 1 :],
                    "error": f"Завершённых записей: {len(results)}. "
                    f"Следующая команда не подтверждена: {exc}. "
                    "Физический результат проверь на ленте.",
                }
        state.last_transaction["delivery_state"] = "sent"
        self._save(state)
        if len(results) == 1:
            return results[0]
        return {
            "written": True,
            "physical_confirmed": False,
            "device_state_confirmed": False,
            "delivery_state": "sent",
            "results": results,
            "message": f"Команды подсветки записаны по Bluetooth: {len(results)}. "
            + ", ".join(_description(command) for command in commands)
            + ". "
            "Физическое выполнение не подтверждено.",
        }

    async def inspect(self, listen_s: float = 2) -> dict:
        """Discover GATT and capture raw FFF4 notifications; send no protocol commands."""
        if not self.cfg.enabled or not 0 <= listen_s <= 5:
            raise ToolError("LED отключена или время диагностики вне 0–5 секунд.")
        async with self._exclusive():
            client = BleakClient(str(self.cfg.address), timeout=self.cfg.connect_timeout_s)
            notifications = []

            def receive(_, data):
                if len(notifications) < 20:
                    notifications.append(bytes(data).hex(" "))

            try:
                async with asyncio.timeout(self.cfg.connect_timeout_s + listen_s + 3):
                    await client.connect()
                    service = client.services.get_service(str(self.cfg.service))
                    if service is None:
                        raise ToolError("Ожидаемый GATT service LED не найден.")
                    chars = [
                        {"uuid": c.uuid, "properties": list(c.properties)}
                        for c in service.characteristics
                    ]
                    notify = service.get_characteristic(str(self.cfg.notify_characteristic))
                    if notify and "notify" in notify.properties and listen_s:
                        await client.start_notify(notify, receive)
                        await asyncio.sleep(listen_s)
                    return {
                        "identity": self.identity,
                        "characteristics": chars,
                        "notifications": notifications,
                        "message": "Диагностика без команд. Сырые уведомления не являются ACK.",
                    }
            except (BleakError, OSError, TimeoutError):
                raise ToolError(
                    "Bluetooth/GATT LED недоступен; проверь питание и доступ macOS."
                ) from None
            except ToolError:
                raise
            except Exception as exc:
                log.warning("LED inspection failed type=%s", type(exc).__name__)
                raise ToolError("Не удалось проверить GATT LED; команды не отправлял.") from None
            finally:
                await self._disconnect(client)

    async def discover(self, timeout_s: float = 4) -> dict:
        """Explicit bounded discovery, never automatic rebinding or control packets."""
        if not self.cfg.enabled or not 0 < timeout_s <= 8:
            raise ToolError("LED отключена или время поиска вне 0–8 секунд.")
        async with self._exclusive():
            try:
                async with asyncio.timeout(timeout_s + 2):
                    found = await BleakScanner.discover(
                        timeout=timeout_s, service_uuids=[str(self.cfg.service)], return_adv=True
                    )
                candidates = [
                    {"name": device.name or adv.local_name, "address": device.address}
                    for device, adv in found.values()
                    if (adv.local_name or device.name) == self.cfg.name
                ]
                return {
                    "candidates": candidates,
                    "message": "Найденные UUID нужно проверить и указать в led.address. "
                    "Настройки не изменял; команды не отправлял.",
                }
            except (BleakError, OSError, TimeoutError):
                raise ToolError(
                    "Поиск LED не завершён: Bluetooth недоступен или истёк тайм-аут."
                ) from None

    def tool(self) -> Tool:
        tool = Tool(
            "led_control",
            "Локальная LED-лента: исходная просьба пользовательницы о цвете, питании или яркости. "
            "Непроверенные режимы блокируются. Не утверждать физический результат записи.",
            self.command,
            {"command": {"type": "string", "minLength": 1, "maxLength": 200}},
            ["command"],
            risk=Risk.CHANGE,
            intent=lambda args, original: authorize(
                args, original, comfortable_brightness=self.cfg.comfortable_brightness
            ),
            reply=lambda r: r["message"],
        )
        tool.required_capability = "mac.system"
        return tool
