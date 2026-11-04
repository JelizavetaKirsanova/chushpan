"""Capability-backed permission catalog. Checks never request or exercise devices."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime

from backend.desktop import platform
from backend.mac import DesktopBridge
from backend.media.music_safari import SafariMusicTab
from backend.media.youtube_playback import SafariPlayback
from backend.native_identity import signing_identity
from backend.personal import PersonalContext

GROUPS = {
    "voice": "Голос и аудио",
    "system": "Система и управление",
    "personal": "Личные данные",
    "integrations": "Интеграции",
}
TARGETS = {
    "safari": "com.apple.Safari",
    "finder": "com.apple.finder",
    "music": "com.apple.Music",
    "system_events": "com.apple.systemevents",
    "spotify": "com.spotify.client",
}
STATES = {"granted", "denied", "not_determined", "check_needed", "unavailable"}


def row(
    key,
    name,
    group,
    purpose,
    features,
    *,
    mechanism="TCC",
    pane=None,
    request=False,
    supported=True,
):
    return {
        "id": key,
        "name": name,
        "group": group,
        "group_name": GROUPS[group],
        "icon": {"voice": "mic", "system": "settings", "personal": "user", "integrations": "link"}[
            group
        ],
        "purpose": purpose,
        "features": features,
        "mechanism": mechanism,
        "pane": pane or key,
        "can_request": request and supported,
        "supported": supported,
        "status": "check_needed" if supported else "unavailable",
        "note": purpose,
        "last_error": None,
        "details": {},
    }


def catalog(settings):
    items = [
        row(
            "microphone",
            "Микрофон",
            "voice",
            "Захват речи только после включения прослушивания.",
            ["Голосовые команды"],
            request=True,
        ),
        row(
            "speech",
            "Распознавание речи",
            "voice",
            "Локальный Whisper не использует Apple Speech API.",
            ["Локальное распознавание"],
            supported=False,
        ),
        row(
            "audio_devices",
            "Аудиоустройства",
            "voice",
            "Выбор входа и выхода — настройки звука. Для выхода отдельного TCC нет.",
            ["Голос и озвучка"],
            mechanism="Настройки звука",
            supported=False,
        ),
        row(
            "background_voice",
            "Фоновое прослушивание",
            "voice",
            (
                "Требуются микрофон и явное включение в разделе Приложение. Проверка не "
                "включает запись."
            ),
            ["Фоновый голос"],
            mechanism="Настройка Чушпана",
            pane="microphone",
        ),
        row(
            "accessibility",
            "Универсальный доступ",
            "system",
            "Управление окнами и элементами интерфейса через существующий AX-адаптер.",
            ["Окна", "YouTube Music Web App"],
            request=True,
        ),
        row(
            "automation",
            "Автоматизация / Apple Events",
            "system",
            (
                "Доступ выдаётся отдельно каждому исполнителю и целевому приложению. См. строки"
                " Safari и Spotify."
            ),
            ["Управление приложениями"],
            mechanism="TCC для каждой пары",
        ),
        row(
            "screen_recording",
            "Запись экрана",
            "system",
            "Только запрошенный пользователем снимок экрана. Диагностика снимков не делает.",
            ["Анализ экрана"],
            request=True,
        ),
        row(
            "system_audio",
            "Запись системного аудио",
            "system",
            "В этой сборке нет захвата системного аудио. Озвучка и музыка его не требуют.",
            [],
            supported=False,
            pane="screen_recording",
        ),
        row(
            "input_monitoring",
            "Мониторинг ввода",
            "system",
            "Глобальный перехват клавиатуры не используется.",
            [],
            supported=False,
        ),
        row(
            "files",
            "Файлы и папки",
            "system",
            (
                "Доступ зависит от выбранного файла и его папки. Единого статуса для всех "
                "файлов нет; выбирай файл системным диалогом."
            ),
            ["Вложения", "Импорт настроек"],
            mechanism="Выбор файла / TCC папки",
        ),
        row(
            "full_disk",
            "Полный доступ к диску",
            "system",
            "Существующие функции не требуют полного доступа к диску.",
            [],
            supported=False,
        ),
        row(
            "notifications",
            "Системные уведомления",
            "system",
            (
                "Таймеры и напоминания выводятся внутри Чушпана. Системная доставка уведомлений"
                " не подключена."
            ),
            [],
            supported=False,
        ),
        row(
            "login",
            "Запуск при входе",
            "system",
            "Включается явно в разделе Приложение → Запуск через SMAppService.",
            ["Автозапуск"],
            mechanism="SMAppService / настройки macOS",
        ),
        row(
            "location",
            "Геолокация",
            "personal",
            (
                "Город для погоды и профиля. Право проверяется у «Чушпан — утро», без получения"
                " координат."
            ),
            ["Погода", "Места"],
            request=True,
        ),
        row(
            "calendar",
            "Календарь",
            "personal",
            "Чтение событий существующим помощником «Чушпан — утро», без изменений событий.",
            ["Календарь", "Утренняя сводка"],
            request=True,
        ),
    ]
    for key, title in [
        ("reminders", "Напоминания macOS"),
        ("contacts", "Контакты"),
        ("photos", "Фото"),
        ("camera", "Камера"),
    ]:
        items.append(
            row(
                key,
                title,
                "personal",
                (
                    "Интеграция с этим системным хранилищем в текущей сборке не реализована; "
                    "доступ не запрашивается."
                ),
                [],
                supported=False,
            )
        )
    for key, title in [
        ("safari", "Safari"),
        ("finder", "Finder"),
        ("music", "Music"),
        ("system_events", "System Events"),
        ("spotify", "Spotify"),
    ]:
        supported = (
            key in {"safari", "system_events"}
            or key == "spotify"
            and settings.media.provider == "spotify"
        )
        items.append(
            row(
                "automation_" + key,
                "Управление " + title,
                "system",
                "Apple Events от подписанного адаптера Чушпана к " + title + "."
                if supported
                else (
                    "Нет активной интеграции Apple Events. Управление окнами Finder использует "
                    "AX, а не Apple Events."
                ),
                ["Музыка / автоматизация"] if supported else [],
                pane="automation",
                request=True,
                supported=supported,
            )
        )
        if key == "system_events" and settings.media.provider != "spotify":
            items[-1].update(
                can_request=False,
                purpose=(
                    "Команда питания использует системный osascript. macOS проверяет эту пару при "
                    "явной команде. Права музыкального адаптера не подтверждают доступ другого "
                    "отправителя."
                ),
            )
    items += [
        row(
            "shortcuts",
            "Apple Shortcuts",
            "integrations",
            (
                "Разрешения зависят от действий каждого сценария. Чушпан не может выдать их за "
                "Shortcuts. Проверь нужный сценарий в приложении Команды."
            ),
            ["Сценарии"],
            mechanism="Настройки конкретной команды",
            pane="automation",
        ),
        row(
            "youtube_music",
            "YouTube Music через Safari",
            "integrations",
            (
                "Нужны доступ Apple Events → Safari, вход на сайте и «Разрешить JavaScript из "
                "событий Apple» в Safari → Настройки → Разработчик. Вход и автовоспроизведение "
                "проверяются при музыкальной команде."
            ),
            ["Музыка", "Доброе утро"],
            mechanism="Apple Events + настройки Safari + вход на сайте",
            pane="automation",
        ),
        row(
            "bluetooth",
            "Локальные Bluetooth-устройства",
            "integrations",
            (
                "BLE запрашивает доступ штатно при явном подключении устройства. Проверка не "
                "сканирует эфир и не включает устройства."
            ),
            ["Подключённые BLE-устройства"],
            supported=True,
        ),
        row(
            "local_network",
            "Локальная сеть",
            "integrations",
            (
                "Настроенные HTTP-интеграции могут потребовать доступ macOS при первом "
                "соединении. Публичного общего API проверки этого TCC нет; проверяй конкретное "
                "подключение в разделе Устройства."
            ),
            ["Локальные интеграции"],
            mechanism="TCC при соединении / настройки интеграции",
        ),
        row(
            "services",
            "Другие сервисы",
            "integrations",
            (
                "Gemini, LiveKit и другие подключённые сервисы используют ключи и настройки "
                "интеграций. Наличие ключа не подтверждает работоспособность. Секреты здесь не "
                "выводятся."
            ),
            ["Подключённые сервисы"],
            mechanism="API-ключ / токен / собственные настройки",
        ),
    ]
    for key, title, pane in [
        ("video_accessibility", "Универсальный доступ — Safari Video", "accessibility"),
        ("video_automation", "Apple Events — Safari Video", "automation"),
    ]:
        items.append(
            row(
                key,
                title,
                "system",
                (
                    "Отдельный исполнитель Safari Video для вечернего видео. Права музыкального "
                    "адаптера не подтверждают его доступ."
                ),
                ["Видео", "Вечерний сценарий"],
                pane=pane,
                request=True,
            )
        )
    if settings.media.provider == "spotify":
        for key, title in [
            ("accessibility", "Универсальный доступ — Spotify"),
            ("screen_recording", "Запись экрана — Spotify"),
        ]:
            items.append(
                row(
                    "music_" + key,
                    title,
                    "system",
                    (
                        "Право подписанного музыкального адаптера для поиска Spotify. Проверка не "
                        "делает снимков и не нажимает кнопки."
                    ),
                    ["Поиск музыки Spotify"],
                    pane=key,
                    request=True,
                )
            )
    items.sort(key=lambda item: list(GROUPS).index(item["group"]))
    for item in items:
        try:
            item["url"] = platform.privacy_url(item["pane"])
        except ValueError:
            item["url"] = ""
    return items


class PermissionCenter:
    def __init__(self, settings, store=None):
        self.s, self.store = settings, store
        self.music, self.personal = SafariMusicTab(settings), PersonalContext(settings)
        self.desktop = DesktopBridge(settings, "Mac")
        self.video = SafariPlayback(settings)
        self.lock = asyncio.Lock()
        self.history = []
        self.checks = {}
        self.voice_state = None
        if store:
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS permission_checks (id INTEGER PRIMARY KEY, at "
                "TEXT NOT NULL, data TEXT NOT NULL)"
            )

    async def check(self, item):
        key = item["id"]
        if not item["supported"]:
            return item
        item["last_error"] = None
        if key == "automation_system_events" and self.s.media.provider != "spotify":
            return item
        if key == "background_voice":
            running = self.voice_state() if self.voice_state else None
            if "platform" not in self.checks:
                self.checks["platform"] = await asyncio.to_thread(platform.permission_status)
            granted = self.checks["platform"].get("microphone") == "granted"
            item["status"] = "granted" if running is True and granted else "check_needed"
            item["details"] = {"capture_loop_running": running, "microphone_granted": granted}
            state = (
                "активен"
                if running is True
                else "приостановлен"
                if running is False
                else "недоступен в этой сессии"
            )
            item["purpose"] += (
                " Голосовой цикл: " + state + ". Звук этой проверкой не захватывается."
            )
            return item
        if key.startswith("video_"):
            if "video" not in self.checks:
                self.checks["video"] = await self.video.request("youtube_permissions")
            details = self.checks["video"]
            value = details.get(key.removeprefix("video_"))
        elif key.startswith("music_"):
            details = await self.music.permission(target="com.spotify.client")
            value = details.get(key.removeprefix("music_"))
        elif key.startswith("automation_"):
            details = await self.music.permission(target=TARGETS[key.removeprefix("automation_")])
            value = details.get("automation")
        elif key in {"location", "calendar"}:
            if "personal" not in self.checks:
                self.checks["personal"] = await self.personal.permissions()
            details = self.checks["personal"]
            details = {
                **details,
                "identity": details.get("identities", {}).get(key, details.get("identity", {})),
            }
            value = details.get(key)
            if key == "calendar" and value == "write_only":
                item["last_error"] = (
                    "Разрешено только добавление событий. Для чтения календаря нужен полный "
                    "доступ EventKit; можно запросить его кнопкой ниже."
                )
            if key == "location" and details.get("location_services") is False:
                value = "denied"
                item["last_error"] = "Службы геолокации macOS выключены."
            if key == "location" and self.store:
                observation = self.store.preference("dashboard", "location") or {}
                if observation.get("status") == "error":
                    code = observation.get("error_code", "location_unavailable")
                    reason = {
                        "location_timeout": "Core Location не прислал свежую геопозицию вовремя.",
                        "location_disabled": "Службы геолокации macOS выключены.",
                        "location_permission": "macOS не разрешила геолокацию этому исполнителю.",
                        "location_city_unavailable": (
                            "Координаты получены, но город определить не удалось."
                        ),
                        "location_native_error": (
                            "Core Location вернул системную ошибку; код указан ниже."
                        ),
                    }.get(code, "Свежая геопозиция сейчас недоступна.")
                    item["last_error"] = (
                        reason + " Разрешение и получение координат — разные проверки."
                    )
                    details = {
                        **details,
                        "last_location_error": {
                            k: observation.get(k)
                            for k in (
                                "error_code",
                                "authorization",
                                "native_code",
                                "native_domain",
                                "helper_id",
                            )
                        },
                    }
        elif key in {"accessibility", "screen_recording"}:
            if "desktop" not in self.checks:
                self.checks["desktop"] = await self.desktop.request("permission_status")
            details = self.checks["desktop"]
            value = details.get(key)
        elif key in {"microphone", "bluetooth", "login"}:
            if "platform" not in self.checks:
                self.checks["platform"] = await asyncio.to_thread(platform.permission_status)
            details = self.checks["platform"]
            value = details.get(key)
        else:
            return item
        item["status"] = (
            value if value in STATES else "denied" if value == "restricted" else "check_needed"
        )
        if value == "denied":
            item["can_request"] = False
        item["details"] = details
        identity = details.get("identity") or details
        if identity.get("executable"):
            item["details"] = {**details, "signing": await signing_identity(identity["executable"])}
        if value == "target_not_running":
            item["last_error"] = (
                "Целевое приложение закрыто. Для запроса доступа оно будет открыто после "
                "нажатия «Запросить доступ»."
            )
        elif value == "restricted":
            item["can_request"] = False
            item["last_error"] = "Доступ ограничен политикой macOS или администратором."
        return item

    def record(self, item, action):
        # Only codes, state and executor identity. No raw errors, file contents,
        # calendar data, SSIDs, coordinates, scripts or credentials in the journal.
        details = item["details"]
        identity = details.get("identity") or {
            k: details[k]
            for k in ("bundle_id", "executable", "pid", "ppid", "target")
            if k in details
        }
        entry = {
            "at": datetime.now(UTC).isoformat(),
            "id": item["id"],
            "action": action,
            "status": item["status"],
            "error": item["last_error"],
            "identity": identity,
            "osstatus": details.get("osstatus"),
            "version": "0.2.4",
            "signing": details.get("signing", {}),
        }
        self.history = (self.history + [entry])[-100:]
        if self.store and self.store.memory_available:
            with self.store.memory_transaction():
                self.store.db.execute(
                    "INSERT INTO permission_checks(at,data) VALUES (?,?)",
                    (entry["at"], json.dumps(entry, ensure_ascii=False)),
                )
                self.store.db.execute(
                    "DELETE FROM permission_checks WHERE id NOT IN (SELECT id FROM "
                    "permission_checks ORDER BY id DESC LIMIT 200)"
                )

    async def report(self, action="check", identifier=None):
        if action not in {"check", "request", "open"}:
            raise ValueError("Неизвестное действие центра разрешений.")
        items = catalog(self.s)
        selected = next((i for i in items if i["id"] == identifier), None)
        if action != "check" and selected is None:
            raise ValueError("Неизвестное разрешение.")
        async with self.lock:
            self.checks = {}
            message = None
            if action == "open":
                if not selected["url"]:
                    raise ValueError("Используй настройки соответствующей интеграции.")
                result = await asyncio.to_thread(platform.open_privacy, selected["pane"])
                message = result["message"]
            elif action == "request":
                if not selected["can_request"]:
                    raise ValueError("Для этой возможности нет отдельного запроса macOS.")
                await self.check(selected)
                key = selected["id"]
                if selected["status"] != "granted":
                    if selected["status"] == "denied":
                        message = (
                            await asyncio.to_thread(platform.open_privacy, selected["pane"])
                        )["message"]
                    elif key.startswith("video_"):
                        await self.video.request("permission_request", permission=selected["pane"])
                    elif key.startswith("music_"):
                        await self.music._native(
                            {"op": "permission_request", "permission": selected["pane"]}
                        )
                    elif key.startswith("automation_"):
                        await self.music.permission(
                            request=True,
                            target=TARGETS[key.removeprefix("automation_")],
                            launch=True,
                        )
                    elif key in {"location", "calendar"}:
                        await self.personal.permissions(request=key)
                    elif key == "microphone":
                        await asyncio.to_thread(platform.request_microphone)
                    elif key in {"accessibility", "screen_recording"}:
                        await self.desktop.request("permission_request", permission=key)
            self.checks = {}
            # Sequential public preflights: no arbitrary integration probes or launches.
            for item in items:
                try:
                    await self.check(item)
                except Exception as exc:
                    item.update(
                        status="check_needed",
                        last_error="Проверка недоступна: " + type(exc).__name__,
                    )
                if identifier is None or item["id"] == identifier:
                    self.record(item, action)
            if (
                action == "request"
                and not message
                and (
                    selected["status"] == "denied"
                    or identifier == "calendar"
                    and selected["details"].get("calendar") == "write_only"
                )
            ):
                message = (await asyncio.to_thread(platform.open_privacy, selected["pane"]))[
                    "message"
                ]
            if self.store and self.store.memory_available:
                history = [
                    json.loads(r[0])
                    for r in self.store.db.execute(
                        "SELECT data FROM permission_checks ORDER BY id DESC LIMIT 40"
                    )
                ]
            else:
                history = list(reversed(self.history[-40:]))
            return {
                "items": items,
                "scope": (
                    "Статус относится к указанному исполнителю текущей сборки. Права Terminal, "
                    "другой копии или старой подписи не переносятся автоматически."
                ),
                "message": message,
                "history": history,
                "platform": sys.platform,
            }
