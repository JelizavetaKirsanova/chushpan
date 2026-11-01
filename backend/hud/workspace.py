"""Read-only workspace data over the existing local WebSocket bus."""

import sqlite3


def bind_workspace(bus, settings, store):
    from backend.music import bind_settings

    bind_settings(bus, settings, store)

    def history(conversation_id):
        try:
            bus.publish(
                {
                    "type": "history",
                    "conversations": store.conversation_list(),
                    "selected": conversation_id,
                    "messages": store.conversation_page(conversation_id)
                    if conversation_id > 0
                    else [],
                }
            )
        except sqlite3.Error:
            bus.publish(
                {
                    "type": "history",
                    "conversations": [],
                    "messages": [],
                    "selected": 0,
                    "error": "История сейчас недоступна. Сохранённые данные не изменены.",
                }
            )

    bus.handle("history", history)
    bus.publish(
        {
            "type": "settings_info",
            "items": {
                "Микрофон": str(
                    settings.audio.input_device
                    if settings.audio.input_device is not None
                    else "Системный по умолчанию"
                ),
                "Распознавание": settings.stt.model,
                "Голос": f"{settings.tts.engine} · "
                + (
                    settings.tts.silero_speaker
                    if settings.tts.engine == "silero"
                    else settings.tts.say_voice or settings.voice_name
                ),
                "Активация": settings.wakeword.phrase or settings.wakeword.model,
                "Облачная модель": settings.gemini.model,
                "История Gemini": f"{settings.gemini.context_messages} сообщений",
                "Ночной режим": "Действует подтверждение и предварительная проверка разрешений",
                "Разрешения macOS": "Голосовая команда: проверь разрешения ночного режима",
                "Память": "Существующая SQLite · история только для чтения в интерфейсе",
                "Производительность": "Раздел «Диагностика» в боковой панели; latency-check в CLI",
                "Изменение параметров": "config.yaml; после изменения перезапусти Чушпана",
            },
        }
    )
