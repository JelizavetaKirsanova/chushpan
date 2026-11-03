"""A guarded read adapter for the existing local PersonalContext calendar integration."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from backend.brain.morning import calendar_sentences
from backend.personal import PersonalContext
from backend.security import Risk
from backend.tools.registry import Tool


def calendar_tool(settings, store=None, personal=None):
    personal = personal or PersonalContext(settings)

    async def read(day="today"):
        if getattr(settings, "_calendar_disabled", False) or (
            store and store.preference("dashboard", "calendar_enabled") is False
        ):
            return {
                "error": "Apple Calendar отключён в настройках интеграций. "
                "Можно подключить его в настройках Чушпана.",
                "error_code": "not_configured",
            }
        now = datetime.now(settings.location.tz)
        try:
            selected = (
                now.date() + timedelta(days=day == "tomorrow")
                if day in {"today", "tomorrow"}
                else datetime.fromisoformat(day).date()
            )
        except ValueError:
            return {"error": "Назови существующую дату календаря.", "error_code": "invalid_date"}
        try:
            result = (
                await personal.read(
                    request_access=False, parts=("calendar",), day=selected.isoformat()
                )
            ).get("calendar", {})
        except (OSError, RuntimeError, TimeoutError):
            return {
                "error": "Apple Calendar временно не ответил. Интеграция есть, "
                "но события сейчас прочитать не удалось.",
                "error_code": "temporarily_unavailable",
            }
        if result.get("error") == "calendar_permission":
            return {
                "error": "Apple Calendar подключён как возможность, но macOS не разрешила чтение. "
                "Проверь доступ к календарям для «Чушпан — утро» "
                "в настройках конфиденциальности.",
                "error_code": "requires_permission",
            }
        if result.get("error") or not isinstance(result.get("events"), list):
            return {
                "error": "Календарь временно недоступен; "
                "наличие и отсутствие планов не подтверждено.",
                "error_code": "temporarily_unavailable",
            }
        try:
            tz = ZoneInfo(result.get("timezone") or settings.location.tz_name)
            start = datetime.combine(selected, time.min, tz)
            end = start + timedelta(days=1)
            events = []
            for event in result["events"]:
                if not isinstance(event, dict) or not isinstance(event.get("title"), str):
                    raise ValueError("invalid calendar event")
                begins, ends = (
                    datetime.fromisoformat(event["start"]),
                    datetime.fromisoformat(event["end"]),
                )
                if begins.tzinfo is None or ends.tzinfo is None or ends <= begins:
                    raise ValueError("invalid calendar event")
                if begins.astimezone(tz) < end and ends.astimezone(tz) > start:
                    events.append(event)
        except (ValueError, TypeError, KeyError):
            return {
                "error": "Календарь вернул неполные данные. Планы не подтверждены.",
                "error_code": "execution_error",
            }
        return {
            "events": events,
            "timezone": str(tz),
            "day": selected.isoformat(),
            "label": "Сегодня"
            if selected == now.date()
            else "Завтра"
            if selected == now.date() + timedelta(days=1)
            else selected.strftime("%d.%m.%Y"),
            "ok": True,
        }

    def reply(result):
        if result.get("error"):
            return result["error"]
        start = datetime.combine(
            datetime.fromisoformat(result["day"]).date(), time.min, ZoneInfo(result["timezone"])
        )
        return calendar_sentences(result, now=start, days=1).replace("Сегодня", result["label"], 1)

    tool = Tool(
        "get_calendar",
        "Прочитать события Apple Calendar выбранного дня: today, tomorrow или YYYY-MM-DD. "
        "Только чтение, без запроса новых разрешений и без геолокации.",
        read,
        {"day": {"type": "string", "pattern": r"^(?:today|tomorrow|\d{4}-\d{2}-\d{2})$"}},
        risk=Risk.READ,
        reply=reply,
        capability={
            "ru": "читать события Apple Calendar через существующую локальную интеграцию; "
            "при отсутствии разрешения назвать причину"
        },
    )
    tool.required_capability = "mac.personal"
    return tool
