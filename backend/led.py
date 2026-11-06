"""Local ELK BLE control. Firmware experiments are never exposed as tools."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
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

from backend.ble_diagnostics import device_key, runtime_info, trace
from backend.ble_profiles import BLEProfile, candidate_profile, gatt_fingerprint
from backend.config import LEDConfig, Settings
from backend.latency import measure
from backend.security import Risk, command_text, control_text
from backend.tools.registry import DeviceUnavailable, Tool, ToolError

log = logging.getLogger(__name__)


def bluetooth_error(exc: BleakBluetoothNotAvailableError) -> str:
    reasons = {
        BleakBluetoothNotAvailableReason.POWERED_OFF: "Bluetooth выключен на Mac. Включи его.",
        BleakBluetoothNotAvailableReason.NO_BLUETOOTH: "Bluetooth-адаптер недоступен.",
        BleakBluetoothNotAvailableReason.NO_BLE_CENTRAL_ROLE: "Нет поддержки BLE.",
        BleakBluetoothNotAvailableReason.DENIED_BY_USER: (
            "Разреши Чушпану Bluetooth в Настройках macOS → Конфиденциальность и безопасность."
        ),
        BleakBluetoothNotAvailableReason.DENIED_BY_SYSTEM: "Доступ Bluetooth запрещён macOS.",
    }
    return "Bluetooth недоступен. " + reasons.get(exc.reason, "Проверь доступ Bluetooth в macOS.")


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
    explicit = re.fullmatch(
        r"(?:установи|сделай|поставь) красный (\d{1,3})[, ]+"
        r"зел[её]ный (\d{1,3})[, ]+синий (\d{1,3})[.!? ]*",
        control_text(text),
    )
    if explicit:
        values = list(map(int, explicit.groups()))
        return {"action": "color", "rgb": values} if max(values) <= 255 else None
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
    profile: BLEProfile | None = None
    gatt_fingerprint: str | None = None
    last_diagnostic: dict | None = None
    driver_fingerprint: str | None = None


class LEDStrip:
    def __init__(self, settings: Settings):
        self.cfg = settings.led
        self.path = settings.data_path / "led" / "device.json"
        self.lock = asyncio.Lock()
        self.before_write = None
        self.state_listener = None
        self.connection_closed = False

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
        driver = state.profile or candidate_profile(self.cfg)
        fingerprint = driver.fingerprint if driver else "unsupported"
        compatible = driver and state.driver_fingerprint in {
            fingerprint,
            driver.previous_fingerprint,
        }
        if not compatible:
            # Existing observations belong to the old BLEDOB serializer only.
            if state.driver_fingerprint or not driver or driver.id != "elk-bledob-legacy":
                state.confirmed.clear()
                state.pending_probe = state.last_write = state.last_transaction = None
                state.brightness = state.gatt_fingerprint = state.last_diagnostic = None
            state.driver_fingerprint = fingerprint
        else:
            state.driver_fingerprint = fingerprint
            # Earlier releases stored one explicitly observed RGB value but required
            # three primary colours to enable the same serializer. Never promote
            # the legacy default-red seed or an unobserved write.
            last = state.last_write or {}
            requested = last.get("requested", {})
            if requested.get("action") == "color" and last.get("physical_confirmed") is True:
                if _key(requested) in state.confirmed:
                    state.confirmed.add("rgb")
        return state

    def _save(self, state: LEDState) -> None:
        temporary = self.path.with_suffix("." + uuid4().hex + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(state.model_dump_json(indent=2), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(self.path)
            if self.state_listener:
                try:
                    self.state_listener(state)
                except Exception as exc:
                    log.warning("LED state observer unavailable type=%s", type(exc).__name__)
        except OSError:
            raise ToolError(
                "Не удалось сохранить настройки LED. Если запись уже началась, "
                "команда могла выполниться; проверь ленту. Автоповтора нет."
            ) from None
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)

    def driver(self, state=None):
        state = state if state is not None else self._load()
        return state.profile or candidate_profile(self.cfg)

    def driver_status(self):
        state = self._load()
        profile = self.driver(state)
        report = state.last_diagnostic or {}
        return {
            "driver": profile.name if profile else "Не выбран",
            "driver_id": profile.id if profile else "unsupported",
            "compatibility": "unsupported"
            if not profile
            else report.get("compatibility", "unverified"),
            "confirmed_functions": sorted(state.confirmed),
            "last_command": state.last_write,
            "diagnostic": report,
            "pending_probe": state.pending_probe,
        }

    async def select_profile(self, profile: BLEProfile | None):
        """Local explicit import/selection is not evidence of physical compatibility."""
        async with self._exclusive():
            state = self._load()
            previous = self.driver(state)
            state.profile = profile
            selected = self.driver(state)
            state.driver_fingerprint = selected.fingerprint if selected else "unsupported"
            changed = not previous or not selected or previous.fingerprint != selected.fingerprint
            if changed:
                state.confirmed.clear()
                state.pending_probe = state.last_write = state.last_transaction = None
                state.brightness = state.gatt_fingerprint = state.last_diagnostic = None
            self._save(state)
            return changed

    def serialize(self, state, action, *, rgb=None, level=None):
        profile = self.driver(state)
        if not profile:
            raise ToolError(
                "Протокол LED неизвестен. Импортируй подтверждённый профиль; команды не отправлял."
            )
        return profile.serialize(action, rgb=rgb, level=level)

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

    async def _disconnect(self, client) -> bool:
        diagnostic("disconnect", "started")
        try:
            async with asyncio.timeout(3):
                await client.disconnect()
            if getattr(client, "is_connected", False) is True:
                diagnostic("disconnect", "unknown")
                return False
            diagnostic("disconnect", "closed")
            return True
        except Exception:
            log.warning("LED disconnect failed; next request will use a new connection")
            diagnostic("disconnect", "unknown")
            return False

    async def _write(self, data: bytes, state: LEDState, args: dict) -> None:
        if self.before_write:
            self.before_write()
        # Reconnect only before write. After write starts, delivery is ambiguous: no replay.
        client = None
        write_started = False
        self.connection_closed = False
        try:
            for attempt in range(2):
                client = BleakClient(str(self.cfg.address), timeout=self.cfg.connect_timeout_s)
                try:
                    diagnostic("connect", "started", attempt=attempt + 1)
                    with measure("ble_connect", device="led_strip", attempt=attempt + 1):
                        async with asyncio.timeout(self.cfg.connect_timeout_s):
                            await client.connect()
                    diagnostic("connect", "connected", attempt=attempt + 1)
                    break
                except BleakBluetoothNotAvailableError:
                    raise
                except BleakDeviceNotFoundError:
                    raise
                except (BleakError, OSError, TimeoutError) as exc:
                    diagnostic(
                        "connect", "error", attempt=attempt + 1, error_type=type(exc).__name__
                    )
                    await self._disconnect(client)
                    client = None
                    if attempt:
                        raise
            profile = self.driver(state)
            if profile is None:
                raise ToolError("Протокол LED неизвестен; команды не отправлял.")
            services = list(client.services)
            fingerprint = gatt_fingerprint(services)
            if state.gatt_fingerprint and state.gatt_fingerprint != fingerprint:
                state.confirmed.clear()
                state.pending_probe = None
                state.brightness = None
                state.last_diagnostic = None
                state.gatt_fingerprint = fingerprint
                self._save(state)
                raise ToolError(
                    "GATT изменился; допуски сброшены. Повтори диагностику и проверку управления."
                )
            char, response = profile.bind(services)
            state.gatt_fingerprint = fingerprint
            # Some controllers drop writes made immediately after service discovery.
            await asyncio.sleep(self.cfg.settle_s)
            state.last_write = {"status": "unknown", "requested": args}
            state.pending_probe = None
            if args["action"] == "brightness":
                state.brightness = None
            self._save(state)
            diagnostic(
                "delivery",
                "write_started",
                delivery_state="requested",
                response=response,
                characteristic=char.uuid,
                driver=profile.id,
            )
            write_started = True
            with measure("ble_write", device="led_strip"):
                async with asyncio.timeout(self.cfg.write_timeout_s):
                    await client.write_gatt_char(char, data, response=response)
            diagnostic("delivery", "sent", delivery_state="sent", physical_confirmed=False)
            # Keep the link alive while the controller consumes the queued write.
            await asyncio.sleep(self.cfg.settle_s)
        except BleakBluetoothNotAvailableError as exc:
            unavailable = not write_started and exc.reason in {
                BleakBluetoothNotAvailableReason.POWERED_OFF,
                BleakBluetoothNotAvailableReason.NO_BLUETOOTH,
                BleakBluetoothNotAvailableReason.NO_BLE_CENTRAL_ROLE,
            }
            failure = DeviceUnavailable if unavailable else ToolError
            raise failure(bluetooth_error(exc)) from None
        except BleakDeviceNotFoundError:
            failure = ToolError if write_started else DeviceUnavailable
            raise failure(
                "LED-лента не найдена по сохранённому BLE UUID. Проверь питание и близость; "
                "после смены UUID выполни python -m backend led discover и обнови led.address. "
                "Другую ленту автоматически не выбираю."
            ) from None
        except (BleakError, OSError, TimeoutError) as exc:
            diagnostic(
                "delivery", "error", write_started=write_started, error_type=type(exc).__name__
            )
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
                self.connection_closed = await self._disconnect(client)

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
        data = self.serialize(state, action, rgb=rgb, level=level)
        if (
            experiment
            and self.driver(state).id != "elk-bledob-legacy"
            and (
                not state.last_diagnostic
                or state.last_diagnostic.get("compatibility") != "gatt_match"
            )
        ):
            raise ToolError(
                "Сначала выполни диагностику: GATT выбранного профиля ещё не проверен. "
                "Команды не отправлял."
            )
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
            "connection": "disconnected" if self.connection_closed else "unknown",
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
                if probe["requested"]["action"] == "color":
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

    def rgb_block_reason(self, state=None):
        state = state if state is not None else self._load()
        driver = self.driver(state)
        if not driver or "color" not in driver.commands:
            return "Выбранный драйвер не поддерживает RGB. Выбери подтверждённый профиль."
        if "rgb" in state.confirmed:
            return ""
        if state.pending_probe and state.pending_probe["requested"]["action"] == "color":
            return "Тест RGB отправлен. Подтверди, что лента действительно изменила цвет."
        return "RGB ещё не проверен. Нажми «Проверить RGB» и подтверди видимое изменение цвета."

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
                self.serialize(
                    simulated, item["action"], rgb=item.get("rgb"), level=item.get("level")
                )
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

    async def inspect(self, listen_s: float = 2, *, read_values: bool = True) -> dict:
        """Inventory every service. Never issue a control/query packet or export values."""
        if not self.cfg.enabled or not 0 <= listen_s <= 5:
            raise ToolError("LED отключена или время диагностики вне 0–5 секунд.")
        async with self._exclusive():
            state = self._load()
            profile = self.driver(state)
            report = {
                "report_id": uuid4().hex,
                "at": datetime.now(UTC).isoformat(),
                "driver_id": profile.id if profile else "unsupported",
                "services": [],
                "characteristics": [],
                "notifications": [],
                "errors": [],
                "stages": [],
                "compatibility": "unverified" if profile else "unsupported",
                "connection": "unknown",
                "result_status": "unverified",
                "control_packets_sent": 0,
                "values_redacted": True,
            }
            client = None
            subscriptions = []

            def stage(name, **values):
                report["stages"].append({"stage": name, **values})
                diagnostic("gatt_" + name, **values)

            def error(name, exc, **values):
                # Exception messages can contain peripheral addresses and read values.
                entry = {"stage": name, "type": type(exc).__name__, **values}
                report["errors"].append(entry)
                stage(name, outcome="error", error_type=type(exc).__name__, **values)

            def receive(char, data):
                if len(report["notifications"]) < 20:
                    entry = {"uuid": char.uuid, "byte_count": len(data), "value": "redacted"}
                    report["notifications"].append(entry)
                    stage("notification", uuid=char.uuid, byte_count=len(data))

            try:
                async with asyncio.timeout(40):
                    stage("connect_started")
                    client = BleakClient(str(self.cfg.address), timeout=self.cfg.connect_timeout_s)
                    async with asyncio.timeout(self.cfg.connect_timeout_s):
                        await client.connect()
                    stage("connected")
                    services = list(client.services)
                    fingerprint = gatt_fingerprint(services)
                    if state.gatt_fingerprint and state.gatt_fingerprint != fingerprint:
                        state.confirmed.clear()
                        state.pending_probe = None
                        state.brightness = None
                        stage("gatt_changed", confirmations_cleared=True)
                    state.gatt_fingerprint = fingerprint
                    for service in services:
                        chars = []
                        for char in service.characteristics:
                            props = sorted(char.properties)
                            entry = {
                                "uuid": char.uuid,
                                "properties": props,
                                "write_modes": [
                                    mode
                                    for prop, mode in (
                                        ("write", "response"),
                                        ("write-without-response", "without-response"),
                                    )
                                    if prop in props
                                ],
                            }
                            chars.append(entry)
                            report["characteristics"].append(entry)
                            if "read" in props and read_values:
                                try:
                                    stage("read_started", uuid=char.uuid)
                                    async with asyncio.timeout(self.cfg.write_timeout_s):
                                        value = await client.read_gatt_char(char)
                                    entry["read"] = {"byte_count": len(value), "value": "redacted"}
                                    stage("read_finished", uuid=char.uuid, byte_count=len(value))
                                except Exception as exc:
                                    entry["read"] = {"error": type(exc).__name__}
                                    error("read", exc, uuid=char.uuid)
                            if listen_s and ("notify" in props or "indicate" in props):
                                try:
                                    async with asyncio.timeout(self.cfg.write_timeout_s):
                                        await client.start_notify(char, receive)
                                    subscriptions.append(char)
                                    stage("notify_started", uuid=char.uuid)
                                except Exception as exc:
                                    error("notify", exc, uuid=char.uuid)
                        report["services"].append({"uuid": service.uuid, "characteristics": chars})
                    stage("services_enumerated", count=len(services))
                    if profile:
                        try:
                            char, response = profile.bind(services)
                            report["write_characteristic"] = char.uuid
                            report["write_mode"] = "response" if response else "without-response"
                            report["compatibility"] = "gatt_match"
                        except ToolError:
                            report["compatibility"] = "gatt_mismatch"
                    if subscriptions:
                        await asyncio.sleep(listen_s)
            except asyncio.CancelledError:
                stage("cancelled")
                report["result_status"] = "failed"
                raise
            except Exception as exc:
                error("connect_or_inventory", exc)
                report["result_status"] = "failed"
                if isinstance(exc, BleakBluetoothNotAvailableError):
                    report["message"] = bluetooth_error(exc)
            finally:
                if client is not None:
                    for char in subscriptions:
                        try:
                            async with asyncio.timeout(1):
                                await client.stop_notify(char)
                            stage("notify_stopped", uuid=char.uuid)
                        except Exception as exc:
                            error("stop_notify", exc, uuid=char.uuid)
                    closed = await self._disconnect(client)
                    report["connection"] = "disconnected" if closed else "unknown"
                    stage("disconnect", closed=closed)
                if report["result_status"] == "failed":
                    report["message"] = (
                        report.get("message", "")
                        + " Диагностика не завершена: смотри этапы и тайм-ауты в отчёте; "
                        "команды не отправлял."
                    )
                else:
                    report["message"] = (
                        "GATT перечислен. Протокол и функции этой ленты ещё не подтверждены. "
                        "Диагностика без команд; команды не отправлял. Уведомления не являются ACK."
                    )
                state.last_diagnostic = report
                self._save(state)
                path = self.path.parent / "diagnostics" / (report["report_id"] + ".json")
                path.parent.mkdir(parents=True, exist_ok=True)
                # No address, name, identity, free-form exceptions or raw payloads in export.
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(report, output, ensure_ascii=False, indent=2)
            return report

    async def discover(
        self, timeout_s: float = 4, *, scan_id: str | None = None, show_all_ble: bool = False
    ) -> dict:
        """Explicit bounded discovery, never automatic rebinding or control packets."""
        if not self.cfg.enabled or not 0 < timeout_s <= 8:
            raise ToolError("LED отключена или время поиска вне 0–8 секунд.")
        scan_id = scan_id or uuid4().hex
        async with self._exclusive():
            try:
                trace(
                    "scanner_start",
                    scan_id,
                    timeout_s=timeout_s,
                    service_filter=None,
                    name_filter=None,
                    show_all_ble=show_all_ble,
                    runtime=runtime_info(),
                )
                async with asyncio.timeout(timeout_s + 2):
                    found = await BleakScanner.discover(timeout=timeout_s, return_adv=True)
                trace("scanner_complete", scan_id, count=len(found))
                candidates = []
                for device, adv in found.values():
                    # Advertising a service is optional; absence is not incompatibility.
                    name = (adv.local_name or device.name or "Без имени").strip()
                    name = "".join(c for c in name if ord(c) >= 32)[:80] or "Без имени"
                    services = [str(s).lower() for s in getattr(adv, "service_uuids", [])]
                    family = not show_all_ble and bool(re.match(r"^ELK-BLE", name, re.IGNORECASE))
                    service_match = not show_all_ble and str(self.cfg.service).lower() in services
                    candidates.append(
                        {
                            "name": name,
                            "address": device.address,
                            "rssi": getattr(adv, "rssi", None),
                            "service_uuids": services,
                            "connection": "not_checked",
                            "compatibility": "candidate" if family or service_match else "unknown",
                            "compatibility_reason": (
                                "Диагностический режим: совместимость не определялась."
                                if show_all_ble
                                else "Семейство ELK BLE; требуется проверка GATT и протокола."
                                if family
                                else "Рекламируется FFF0; протокол ещё не проверен."
                                if service_match
                                else "Профиль неизвестен; совместимость не определена."
                            ),
                        }
                    )
                    trace(
                        "scanner_device",
                        scan_id,
                        device=device_key(device.address),
                        name=name,
                        rssi=getattr(adv, "rssi", None),
                        services=services,
                        included=True,
                        reason="no_filters",
                        compatibility=candidates[-1]["compatibility"],
                    )
                candidates.sort(
                    key=lambda c: (
                        c["compatibility"] != "candidate",
                        -(c["rssi"] if c["rssi"] is not None else -200),
                        c["name"].casefold(),
                        c["address"],
                    )
                )
                return {
                    "candidates": candidates,
                    "scanner_count": len(found),
                    "message": "Найденные BLE-устройства доступны в разделе «Устройства». "
                    "Имя и UUID сервиса не доказывают совместимость протокола. "
                    "Настройки не изменял; команды не отправлял.",
                }
            except BleakBluetoothNotAvailableError as exc:
                trace("scanner_error", scan_id, error=type(exc).__name__, reason=str(exc.reason))
                raise ToolError(bluetooth_error(exc)) from None
            except (BleakError, OSError, TimeoutError):
                trace("scanner_error", scan_id, reason="bluetooth_or_timeout")
                raise ToolError(
                    "Поиск LED не завершён: Bluetooth недоступен или истёк тайм-аут."
                ) from None
            except asyncio.CancelledError:
                trace("scanner_cancelled", scan_id)
                raise
            except Exception as exc:
                log.warning("BLE discovery failed type=%s", type(exc).__name__)
                raise ToolError(
                    "Ошибка поиска BLE. Проверь доступ Bluetooth и повтори поиск."
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
