"""Local dialogue intent gates, not a second memory or a speaker identity model."""

import re

from backend.security import command_text, confirmation_code

CLOSE = {"закрой сессию", "заверши диалог", "закончи разговор", "на сегодня всё"}
CONTROLS = {
    "расскажи подробнее",
    "нет я имела в виду другое",
    "нет я имел в виду другое",
    "повтори последнее",
    "скажи короче",
    "продолжай",
}


def normalize(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", command_text(text)).split())


def follow_up(text: str) -> bool:
    """Conservative nameless input gate; conversational background isn't a command."""
    from backend.local_commands import route
    from backend.night import night_requested

    value = normalize(text)
    if value in CLOSE | CONTROLS | {"спасибо", "привет", "пока", "доброе утро", "спокойной ночи"}:
        return True
    if route(text) or night_requested(text):
        return True
    if confirmation_code(text) is not None or value in {"да", "нет", "отмена"}:
        return True
    return bool(
        re.match(
            r"^(?:а (?:завтра|сегодня|в |на |почему|как|что)|"
            r"кто |что |где |когда |почему |зачем |как |сколько |как\w+ |"
            r"расскажи |объясни |покажи |найди |открой |включи |выключи |"
            r"сделай |поставь |установи |запусти |проверь |запомни |напомни )",
            value,
        )
    )


def private_headset(audio) -> bool:
    """Read device metadata only. Both capture and output must be a private headset.

    This is a prerequisite, not proof of speaker identity. Unknown devices fail closed.
    """
    try:
        import sounddevice as sd

        names = [
            str(sd.query_devices(device, kind)["name"]).casefold()
            for device, kind in [(audio.input_device, "input"), (audio.output_device, "output")]
        ]
        return all(
            re.search(r"airpods|headset|headphone|науш|гарнитур|beats", name) for name in names
        )
    except Exception:
        return False
