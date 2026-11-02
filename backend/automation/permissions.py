"""Honest permission surface: unobserved and manual grants never become 'granted'."""

from backend.permissions import diagnose

SETTINGS = {
    "microphone": ("Микрофон", "Privacy_Microphone"),
    "speech": ("Распознавание речи", "Privacy_SpeechRecognition"),
    "accessibility": ("Универсальный доступ", "Privacy_Accessibility"),
    "automation": ("Automation / Apple Events", "Privacy_Automation"),
    "bluetooth": ("Bluetooth", "Privacy_Bluetooth"),
    "location": ("Геолокация", "Privacy_LocationServices"),
    "notifications": ("Уведомления", ""),
    "shortcuts": ("Apple Shortcuts", ""),
    "apps": ("Управление приложениями", "Privacy_Automation"),
    "screen_recording": ("Запись экрана", "Privacy_ScreenCapture"),
}


async def report(settings):
    result = await diagnose(("com.apple.Safari", "com.apple.systemevents"), settings=settings)
    rows = []
    for key, (name, pane) in SETTINGS.items():
        observed = result.get(key, "unknown")
        if isinstance(observed, dict):
            state = (
                "granted"
                if observed and all(v == "granted" for v in observed.values())
                else "unknown"
            )
        elif observed in {"granted", "denied", "not_granted", "restricted", "not_determined"}:
            state = (
                "granted"
                if observed == "granted"
                else "denied"
                if observed in {"denied", "not_granted", "restricted"}
                else "manual"
            )
        else:
            state = "unknown"
        note = (
            "Статус относится к диагностическому процессу в текущей цепочке запуска; "
            "права другой копии приложения не проверены."
        )
        if key == "speech":
            state, note = (
                "unsupported",
                "Используется локальный Whisper. Apple Speech API не используется.",
            )
        elif key in {"bluetooth", "location", "notifications", "shortcuts", "apps"}:
            state = "manual"
            note = (
                "Проверь доступ вручную в системных настройках и настройках интеграции. "
                "Наличие конфигурации не подтверждает разрешение."
            )
        rows.append(
            {
                "id": key,
                "name": name,
                "status": state,
                "note": note,
                "url": "x-apple.systempreferences:com.apple.preference.security?" + pane
                if pane
                else "x-apple.systempreferences:com.apple.Notifications-Settings.extension"
                if key == "notifications"
                else "shortcuts://",
            }
        )
    return {
        "items": rows,
        "error": result.get("error"),
        "scope": result.get("probe_identity", {}).get("note", "Текущий процесс запуска"),
    }
