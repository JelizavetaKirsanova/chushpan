"""Read-only workspace data over the existing local WebSocket bus."""

import sqlite3


def bind_workspace(bus, settings, store):
    from backend.music import bind_settings

    bind_settings(bus, settings, store)
    store.performance.bind(bus, settings.location.tz_name)

    search = {"query": "", "trash": False}

    def history(conversation_id):
        try:
            bus.publish(
                {
                    "type": "history",
                    "conversations": store.conversation_list(
                        search["query"], trash=search["trash"]
                    ),
                    "query": search["query"],
                    "trash": search["trash"],
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

    def history_search(value):
        if set(value) <= {"query", "trash", "selected"} and isinstance(value.get("query", ""), str):
            search.update(query=value.get("query", "")[:200], trash=value.get("trash") is True)
            selected = value.get("selected", 0)
            history(selected if type(selected) is int else 0)

    bus.handle("history_search", history_search)
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
                "Память": "Локальная SQLite · история, заметки и подтверждаемые изменения фактов",
                "Производительность": "Постоянная история трасс в разделе «Производительность»",
                "Изменение параметров": (
                    "Голос и модели — config.yaml; параметры комнаты сохраняются здесь"
                ),
            },
        }
    )
