"""Bounded Russian lighting grammar. No LLM inference or partial-clause execution."""

import re
from datetime import timedelta

from backend.devices.lighting import now
from backend.tools.registry import ToolError


def select(devices, text):
    places = []
    for profile in devices.locations.list():
        names = [profile.name.casefold().replace("ё", "е")]
        if profile.id == "home":
            names += ["дома", "в доме"]
        if profile.id == "dorm":
            names += ["в общаге", "в общагу"]
        for name in names:
            if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text):
                places.append(profile.id)
                text = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", " ", text)
                break
    if len(set(places)) > 1:
        raise ValueError("Выбери одно местоположение.")
    place = places[0] if places else devices.locations.current().get("place_id")
    targets = []
    for item in [*devices.store.list(), *devices.store.collections("group")]:
        if getattr(item, "kind", "led_strip") not in {"led_strip", "wled"}:
            continue
        for name in sorted(devices.device_names(item), key=len, reverse=True):
            name = name.casefold().replace("ё", "е")
            if name and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text):
                targets.append(item.id)
                text = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", "ленту", text)
                break
    all_lights = bool(re.search(r"\b(?:обе ленты|все ленты|освещение|весь свет)\b", text))
    text = re.sub(r"\b(?:обе ленты|все ленты|освещение|весь свет)\b", "ленту", text)
    if not targets:
        candidates = [
            d
            for d in devices.store.list()
            if d.kind in {"led_strip", "wled"} and d.place_id in {place, "general"}
        ]
        if all_lights:
            targets = [d.id for d in candidates]
        elif len(candidates) == 1:
            targets = [candidates[0].id]
        else:
            try:
                targets = [devices.resolve("led_control").id]
            except ToolError:
                raise ValueError("Уточни название ленты или скажи «обе ленты».") from None
    return targets, " ".join(text.split()), places[0] if places else "general"


def voice_plan(devices, text):
    temporal = bool(
        re.search(
            r"через|каждый будний день|плавное затухание|"
            r"плавно (?:увеличь|уменьши)|режим кино|пресет",
            text,
        )
    )
    if not temporal:
        return None
    names = [
        n.casefold().replace("ё", "е")
        for d in [*devices.store.list(), *devices.store.collections("group")]
        if getattr(d, "kind", "led_strip") in {"led_strip", "wled"}
        for n in devices.device_names(d)
        if n
    ]
    light_request = bool(
        re.search(r"лент|подсвет|освещени|весь свет|плавное затухание|режим кино|пресет", text)
    ) or any(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text) for name in names)
    if not light_request:
        return None
    if len(text) > 500 or re.search(r"\b(?:не|если|или|либо|потом)\b|[;«»\"`\n]", text):
        return {"error": "Уточни одну полную команду освещения без отрицаний и альтернатив."}
    try:
        targets, normalized, place = select(devices, text)
        timer = re.fullmatch(
            r"(выключи|включи) (?:ленту|подсветку) через (\d{1,5}) "
            r"(секунд[уы]?|минут[уы]?|час(?:а|ов)?)",
            normalized,
        )
        if timer:
            multiplier = (
                3600 if timer[3].startswith("час") else 60 if timer[3].startswith("минут") else 1
            )
            delay = int(timer[2]) * multiplier
            if not 2 <= delay <= 31536000:
                raise ValueError("Укажи задержку от 2 секунд до года.")
            return {
                "commands": [
                    {
                        "target": t,
                        "action": "light_timer",
                        "delay_s": delay,
                        "power": timer[1] == "включи",
                    }
                    for t in targets
                ]
            }
        fade = re.fullmatch(
            r"(?:сделай плавное затухание|плавно уменьши яркость)"
            r"(?: ленту| ленты)? на (\d{1,4}) минут[уы]?",
            normalized,
        )
        if fade:
            duration = int(fade[1]) * 60
            if not 2 <= duration <= 86400:
                raise ValueError("Длительность перехода: до 24 часов.")
            return {
                "commands": [
                    {"target": t, "action": "light_fade", "brightness": 0, "duration_s": duration}
                    for t in targets
                ]
            }
        ramp = re.fullmatch(
            r"плавно (?:увеличь|уменьши) яркость(?: ленту| ленты)? до (\d{1,3})"
            r"(?: ?%| процентов)? (?:за|на) (\d{1,4}) минут[уы]?",
            normalized,
        )
        if ramp:
            level, minutes = map(int, ramp.groups())
            if not 0 <= level <= 100 or not 1 <= minutes <= 1440:
                raise ValueError("Укажи яркость 0–100% и длительность до 24 часов.")
            return {
                "commands": [
                    {
                        "target": t,
                        "action": "light_fade",
                        "brightness": level,
                        "duration_s": minutes * 60,
                    }
                    for t in targets
                ]
            }
        weekly = re.fullmatch(
            r"каждый будний день в (\d{1,2}:[0-5]\d) выключай (?:ленту|подсветку)", normalized
        )
        if weekly:
            at = weekly[1].zfill(5)
            return {
                "lighting_schedule": {
                    "name": "Выключение по будням",
                    "targets": targets,
                    "place_id": place,
                    "action": {"power": False},
                    "rule": {
                        "kind": "weekly",
                        "at": at,
                        "weekdays": [0, 1, 2, 3, 4],
                        "timezone": devices.s.location.tz_name,
                    },
                    "next_at": (now() + timedelta(days=1)).isoformat(),
                }
            }
        preset = re.fullmatch(r"(?:включи|примени|поставь) (?:режим |пресет )(.+)", normalized)
        if preset:
            name = {"кино": "просмотр фильма"}.get(preset[1], preset[1])
            matches = [
                p
                for p in devices.lighting.list("preset")
                if p.name.casefold().replace("ё", "е") == name
            ]
            if len(matches) != 1:
                if devices.tools.automations and any(
                    s.name.casefold().replace("ё", "е") == name
                    for s in devices.tools.automations.store.list()
                ):
                    return None  # Existing scenario router owns its registered phrase.
                if preset[1] == "кино":
                    return {
                        "commands": [
                            {"target": t, "action": "led", "color": "#8A28CC"} for t in targets
                        ]
                    }
                raise ValueError("Сначала создай пресет с таким названием в разделе «Освещение».")
            return {
                "commands": [
                    {"target": t, "action": "light_preset", "preset_id": matches[0].id}
                    for t in targets
                ]
            }
        return {"error": "Команду освещения не разобрал полностью. Уточни ленту, время и действие."}
    except ValueError as exc:
        return {"error": str(exc)}
