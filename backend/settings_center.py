"""Settings commands over the existing local HUD, SQLite and scenario engine."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sqlite3
import time

from backend.brain.privacy import contains_secret
from backend.context import RequestContext, request_scope
from backend.personality import TRAITS, PersonalityProfile, personalities
from backend.settings_models import (
    CATEGORIES,
    AboutMe,
    CommandControls,
    Models,
    Studio,
    apply_persisted,
)
from backend.tools.registry import ToolError


class SettingsCenter:
    def __init__(self, settings, store, conv, bus, *, loop=None, startup_permissions=False):
        self.s, self.store, self.conv, self.bus, self.loop = settings, store, conv, bus, loop
        self.profiles = personalities(store)
        self.jobs, self.capture_target = set(), None
        self.wake_test_until = None
        self.capture_timer = None
        self.permissions, self.model_status, self.last_preview = None, {}, None
        self.permission_onboarding = None
        self.startup_permissions = startup_permissions
        self.permission_job = None
        from backend.permission_center import PermissionCenter

        if getattr(conv.tools, "permission_center", None) is None:
            conv.tools.permission_center = PermissionCenter(settings, store)
        conv.tools.permission_center.voice_state = lambda: (
            bool(self.loop.microphone_running) if self.loop else None
        )
        self.input_devices = None
        self.pending_import = None
        self.original_client = conv.llm
        self._publish_scheduled = False
        self.closed = False
        self.clients = []
        self.defaults = {
            "commands": CommandControls(
                context_s=settings.tools.control_context_s,
                default_brightness_device=settings.tools.default_brightness_device,
                ambiguity_rule=settings.tools.ambiguity_rule,
            ).model_dump(),
            "studio": Studio(
                voice=settings.voice_name,
                input_device=settings.audio.input_device,
                pronunciations=dict(settings.tts.pronunciations),
                wake_phrase=settings.wakeword.phrase or "эй чушпан",
                silence_ms=settings.vad.silence_ms,
                adaptive_endpoint=settings.vad.adaptive_endpoint,
            ).model_dump(),
            "models": Models(
                provider=settings.llm.provider,
                local_model=settings.llm.model,
                cloud_model=settings.gemini.model,
                cloud_fallback=settings.gemini.fallback_model or "",
                fallback=settings.llm.fallback,
                streaming=settings.llm.streaming and settings.gemini.streaming,
                cloud_thinking=settings.gemini.thinking_level,
                cloud_timeout_s=settings.gemini.timeout_s,
                cloud_first_result_s=None,  # Empty means the selected processing mode.
                cloud_idle_s=settings.gemini.stream_idle_timeout_s,
                local_first_result_s=settings.llm.first_result_timeout_s,
                local_idle_s=settings.llm.stream_idle_timeout_s,
            ).model_dump(),
            "about": AboutMe(
                name=settings.user.name or "", address=settings.user.form_of_address or ""
            ).model_dump(),
        }
        conv.tools.personality_profiles = self.profiles
        conv.tools.settings_store = store
        for tool in conv.tools._tools.values():
            tool.settings_store = store
        bus.handle("settings_center", self.request)
        self.profiles.on_change = self.changed
        if loop is not None:
            loop.settings_center = self
            self.apply_studio(self.value("studio"))
        self.publish()
        if startup_permissions:
            self.request({"op": "permissions"})

    def value(self, category):
        value = (
            self.store.preference("settings", category)
            or self.defaults.get(category)
            or CATEGORIES[category]().model_dump()
        )
        return {**self.defaults[category], **value} if category == "models" else value

    def snapshot(self):
        automations = self.conv.tools.automations
        return {
            "type": "settings_center",
            "version": 1,
            "values": {key: self.value(key) for key in CATEGORIES},
            "profiles": [p.model_dump() for p in self.profiles.list()],
            "active": self.profiles.selection(),
            "traits": {k: {"name": v[0], "group": v[1]} for k, v in TRAITS.items()},
            "memory": self.store.memories(),
            "memory_pending": self.store.preference("memory", "pending") or [],
            "training": automations.store.training() if automations else [],
            "habits_opt_in": bool(self.store.preference("memory", "habits_opt_in")),
            "engine": self.s.tts.engine,
            "voices": ["eugene", "aidar", "baya", "kseniya", "xenia"]
            if self.s.tts.engine == "silero"
            else [self.s.voice_name],
            "voice_support": {
                "rate": self.s.tts.engine in {"silero", "piper"},
                "pitch": self.s.tts.engine == "silero",
                "expression": False,
            },
            "permissions": self.permissions,
            "permission_onboarding": self.permission_onboarding,
            "music_application": getattr(self.conv.tools, "media", None).music_application
            if getattr(self.conv.tools, "media", None)
            else None,
            "input_devices": self.input_devices,
            "models": self.model_status,
            "location": automations.locations.current() if automations else {},
            "places": [p.model_dump() for p in automations.locations.list()] if automations else [],
            "signals": automations.locations.signal_observation if automations else {},
            "rules": [
                p.model_dump()
                for p in automations.store.list()
                if p.description.startswith("Характер: ")
            ]
            if automations
            else [],
            "integrations": self.integrations(),
            "actions": [
                {
                    "id": t.name,
                    "description": t.description[:220],
                    "risk": (
                        t.risk
                        or __import__("backend.security", fromlist=["risk_for"]).risk_for(
                            t.name, {}
                        )
                    ).value,
                    "enabled": t.name not in self.value("access")["disabled_tools"],
                }
                for t in self.conv.tools._tools.values()
            ],
            "recent_actions": automations.store.history()[:20] if automations else [],
            "last_errors": self.errors(),
            "preview": self.last_preview,
            "command_history": self.conv.controls.history,
        }

    async def storage_action(self, value):
        from backend import storage_manager

        op = value.get("op")
        if op == "storage_scan":
            report = await asyncio.to_thread(storage_manager.storage_report)
            self.publish(storage_report=report)
        elif op == "storage_preview":
            preview = await asyncio.to_thread(storage_manager.preview_cleanup)
            self.publish(storage_preview=preview)
        elif op == "storage_clean":
            result = await asyncio.to_thread(
                storage_manager.apply_cleanup, None, None, value.get("token")
            )
            report = await asyncio.to_thread(storage_manager.storage_report)
            self.publish(
                storage_result=result,
                storage_report=report,
                message="Безопасная очистка завершена.",
            )
        elif op == "storage_pin":
            await asyncio.to_thread(
                storage_manager.set_backup_pinned,
                value.get("path", ""),
                value.get("pinned") is True,
            )
            report = await asyncio.to_thread(storage_manager.storage_report)
            self.publish(storage_report=report, message="Закрепление резервной копии обновлено.")
        elif op == "storage_auto":
            dirs = storage_manager.roots()
            policy = dirs["backups"].parent / "storage-policy.json"
            try:
                doc = json.loads(policy.read_text())
            except (OSError, ValueError):
                doc = {"pinned": []}
            doc["auto_cleanup"] = value.get("enabled") is True
            temporary = policy.with_name(policy.name + ".tmp")
            temporary.write_text(json.dumps(doc, indent=2))
            temporary.chmod(0o600)
            temporary.replace(policy)
            report = await asyncio.to_thread(storage_manager.storage_report)
            self.publish(
                storage_report=report,
                storage_auto_cleanup=doc["auto_cleanup"],
                message="Настройка автоматической очистки сохранена.",
            )

    def publish(self, **extra):
        self._publish_scheduled = False
        if self.closed:
            return
        try:
            self.bus.publish({**self.snapshot(), **extra})
        except sqlite3.Error:
            self.bus.publish(
                {"type": "notice", "message": "Хранилище настроек недоступно.", "error": True}
            )

    def changed(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.publish()
            return
        if not self._publish_scheduled:
            self._publish_scheduled = True
            loop.call_soon(self.publish)

    def errors(self):
        rows = self.store.db.execute(
            "SELECT data FROM request_traces WHERE status='failed' ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        return [
            {
                "created_at": (v := json.loads(r[0]))["created_at"],
                "errors": v.get("errors", []),
                "model": v.get("metadata", {}).get("model"),
            }
            for r in rows
        ]

    def integrations(self):
        groups = {
            "shortcuts": ("Apple Shortcuts", ["run_shortcut", "list_shortcuts"]),
            "led": ("Bluetooth LED", ["led_control"]),
            "tv": ("BetterDisplay", ["set_external_brightness"]),
            "music": (
                "Музыка",
                [
                    "music_command",
                    "play_music",
                    "now_playing",
                    "music_playlist",
                    "music_search",
                    "media_control",
                    "media_seek",
                    "media_volume",
                ],
            ),
            "calendar": ("Apple Calendar", ["get_calendar"]),
            "mac": ("macOS", ["open_app", "set_brightness", "set_volume"]),
        }
        disabled = self.value("access")["disabled_tools"]
        observed = (
            self.conv.tools.automations.engine.device_state if self.conv.tools.automations else {}
        )

        def status_for(key, names):
            if (
                key == "calendar"
                and self.store.preference("dashboard", "calendar_enabled") is False
            ):
                return "not_configured"
            if not any(self.conv.tools.enabled(name) for name in names):
                return "not_configured"
            checks = [
                self.conv.tools.availability[n] for n in names if n in self.conv.tools.availability
            ]
            if checks:
                return max(checks, key=lambda v: v["updated_at"])["status"]
            return observed.get(key, {}).get("status", "unknown")

        return [
            {
                "id": key,
                "name": name,
                "tools": [t for t in tools if t in self.conv.tools],
                "enabled": any(t not in disabled and t in self.conv.tools for t in tools)
                and (
                    key != "calendar"
                    or self.store.preference("dashboard", "calendar_enabled") is not False
                ),
                "supported": key == "calendar" or any(t in self.conv.tools for t in tools),
                "status": status_for(key, tools),
                "error": observed.get(key, {}).get("error"),
                "details": list(self.s.shortcuts.allowed) if key == "shortcuts" else [],
            }
            for key, (name, tools) in groups.items()
        ]

    def request(self, value):
        if (
            value.get("op") == "permissions"
            and self.permission_job
            and not self.permission_job.done()
        ):
            return
        task = asyncio.create_task(self.handle(value))
        if value.get("op") == "permissions":
            self.permission_job = task
        self.jobs.add(task)
        task.add_done_callback(self.jobs.discard)

    def update_permission_onboarding(self):
        if not self.startup_permissions or not self.permissions:
            return
        required = [] if self.value("studio").get("text_only") else ["microphone"]
        missing = sorted(
            i["id"]
            for i in self.permissions["items"]
            if i["id"] in required and i["status"] != "granted"
        )
        fingerprint = hashlib.sha256("|".join(missing).encode()).hexdigest()[:16]
        dismissed = self.store.preference("permissions", "onboarding_dismissed") or []
        self.permission_onboarding = {
            "visible": bool(missing) and fingerprint not in dismissed,
            "required": required,
            "missing": missing,
            "fingerprint": fingerprint,
        }

    async def music_application(self, value):
        from backend.media.safari import SafariMusic

        media = getattr(self.conv.tools, "media", None)
        if media is None:
            raise ValueError("Музыкальная интеграция недоступна.")
        config = self.s.media.youtube_music
        action = value.get("action")
        if action == "launch":
            if not media.youtube:
                raise ValueError("Сначала выбери YouTube Music как музыкальный сервис.")
            result = await media.youtube.app.launch()
            if not result.get("ok"):
                raise ToolError("Не удалось подтвердить запуск YouTube Music.")
            self.publish(message="YouTube Music открыт. Воспроизведение не запускалось.")
            return
        if action in {"detect", "select"}:
            if action == "detect":
                # Explicit rediscovery also clears a stale, previously selected path.
                discovery_settings = self.s.model_copy(deep=True)
                discovery_settings.media.youtube_music.app_path = None
                discovery_settings.media.youtube_music.bundle_id = None
                discovery = SafariMusic(discovery_settings)
                path, _ = await asyncio.to_thread(discovery.discover)
                app = SafariMusic.application(path)
            else:
                app = SafariMusic.application(value.get("path", ""), selected=True)
            from pathlib import Path

            config.app_path, config.bundle_id = Path(app["path"]), app["bundle_id"]
            config.transport = "auto"
        elif action == "browser":
            if type(value.get("enabled")) is not bool:
                raise ValueError("Неверное состояние браузерного режима.")
            config.allow_browser = value["enabled"]
            config.transport = "auto"
        else:
            raise ValueError("Неизвестная настройка YouTube Music.")
        self.store.set_preference(
            "media",
            "youtube_application",
            {
                "transport": config.transport,
                "app_path": str(config.app_path) if config.app_path else None,
                "bundle_id": config.bundle_id,
                "allow_browser": config.allow_browser,
            },
        )
        await asyncio.to_thread(media.refresh_youtube)
        self.publish(message="Настройка YouTube Music сохранена.")

    def require_idle(self):
        if self.loop and self.loop.busy:
            raise ValueError("Дождись окончания ответа или останови его.")
        if not self.conv.device.authenticated:
            raise ValueError("Настройки доступны только авторизованной сессии.")

    async def handle(self, value):
        try:
            if not self.conv.device.authenticated:
                raise ValueError("Настройки доступны только авторизованной сессии.")
            op = value.get("op")
            if op == "preview_cancel":
                await self.cancel_previews()
                self.stop_capture()
                self.publish(message="Предпросмотр остановлен.")
                return
            if op == "listen_stop":
                self.stop_capture()
                self.publish(message="Тест микрофона остановлен.")
                return
            if op == "refresh":
                self.publish()
                return
            if op in {
                "storage_scan",
                "storage_preview",
                "storage_clean",
                "storage_pin",
                "storage_auto",
            }:
                await self.storage_action(value)
                return
            if op in {"permissions", "permission_action"}:
                self.permissions = await self.conv.tools.permission_center.report(
                    action=value.get("action", "check") if op == "permission_action" else "check",
                    identifier=value.get("id"),
                )
                self.update_permission_onboarding()
                self.publish(
                    message=self.permissions.get("message")
                    or (
                        "Проверка разрешений завершена. Запись и управление устройствами не "
                        "выполнялись."
                    )
                )
                return
            if op == "permissions_dismiss":
                if self.permission_onboarding:
                    values = self.store.preference("permissions", "onboarding_dismissed") or []
                    values = list(
                        dict.fromkeys([*values, self.permission_onboarding["fingerprint"]])
                    )[-20:]
                    self.store.set_preference("permissions", "onboarding_dismissed", values)
                    self.update_permission_onboarding()
                    self.publish()
                return
            self.require_idle()
            if op == "music_application":
                await self.music_application(value)
                return
            if op == "save":
                await self.save(value["category"], value["value"])
            elif op == "profile_save":
                profile = self.profiles.save(PersonalityProfile.model_validate(value["profile"]))
                self.publish(
                    selected=profile.id,
                    message="Профиль сохранён. Изменения действуют со следующего ответа.",
                )
                return
            elif op == "profile_copy":
                profile = self.profiles.copy(value["id"], value.get("name"))
                self.publish(
                    selected=profile.id, message="Копия создана. Активный профиль не изменён."
                )
                return
            elif op == "profile_select":
                if type(value.get("locked", False)) is not bool:
                    raise ValueError("Неверное состояние блокировки.")
                result = self.profiles.select(value["id"], locked=value.get("locked", False))
                self.publish(message=result["message"])
                return
            elif op == "profile_delete":
                if value.get("confirm") is not True:
                    raise ValueError("Подтверди удаление профиля.")

                def references(blocks):
                    return any(
                        (
                            b.capability == "assistant.personality"
                            and b.parameters.get("profile_id") == value["id"]
                        )
                        or references(b.children)
                        or references(b.otherwise)
                        for b in blocks
                    )

                if self.conv.tools.automations and any(
                    references(s.actions) or any(references(v.actions) for v in s.variants)
                    for s in self.conv.tools.automations.store.list()
                ):
                    raise ValueError("Профиль используется сценарием. Сначала измени его блок.")
                self.profiles.delete(value["id"], value["revision"])
            elif op == "preview":
                await self.cancel_previews()
                asyncio.current_task().set_name("settings-preview")
                await self.preview(value)
                return
            elif op == "microphones":
                from backend.audio.capture import input_devices

                self.input_devices = await asyncio.to_thread(input_devices)
            elif op == "wake_test":
                if not self.loop or not self.loop.microphone_running:
                    raise ValueError("Сначала включи микрофон приложения.")
                if self.loop.busy:
                    raise ValueError("Дождись окончания ответа перед тестом активации.")
                self.capture_target = None
                self.wake_test_until = time.monotonic() + 20
                self.loop.command("sleep")
                if self.capture_timer:
                    self.capture_timer.cancel()
                self.capture_timer = asyncio.create_task(self.expire_capture())
                self.jobs.add(self.capture_timer)
                self.capture_timer.add_done_callback(self.jobs.discard)
                self.publish(
                    message="Произнеси wake-фразу в течение 20 секунд. Команды не выполняются."
                )
                return
            elif op == "listen":
                if not self.loop or not self.loop.microphone_running:
                    raise ValueError("Микрофон приложения не запущен.")
                self.capture_target = time.monotonic() + 20
                self.loop.command("wake")
                if self.capture_timer:
                    self.capture_timer.cancel()
                self.capture_timer = asyncio.create_task(self.expire_capture())
                self.jobs.add(self.capture_timer)
                self.capture_timer.add_done_callback(self.jobs.discard)
                self.publish(
                    message=(
                        "Одна тестовая реплика, до 20 секунд. "
                        "Запись в файл и выполнение команд выключены."
                    )
                )
                return
            elif op == "models_status":
                await self.inspect_models()
            elif op == "voice_test":
                await self.cancel_previews()
                asyncio.current_task().set_name("settings-preview")
                timing = await self.speak(value.get("text", "Привет. Это мой текущий голос."))
                first = timing.get("first_sound_s")
                self.publish(
                    message=(
                        f"Голос проверен. Первый содержательный звук: {first:.2f} с."
                        if first is not None
                        else "Тест завершён. Первый звук не измерен."
                    )
                )
                return
            elif op == "speed_test":
                await self.cancel_previews()
                asyncio.current_task().set_name("settings-preview")
                models = value.get("models", [self.s.llm.model])
                if (
                    not isinstance(models, list)
                    or not 1 <= len(models) <= 2
                    or any(not isinstance(m, str) for m in models)
                ):
                    raise ValueError("Для сравнения выбери одну или две локальные модели.")
                for model in models:
                    await self.preview(
                        {
                            "text": "Почему небо голубое? Ответь одним предложением.",
                            "ids": [self.profiles.active().id],
                            "speak": value.get("speak") is True,
                            "speed": True,
                            "model": model,
                        }
                    )
                return
            elif op == "rule":
                self.save_rule(value)
            elif op == "memory_approve":
                self.approve_memory(value)
            elif op == "memory_clear":
                self.clear_memory(value)
            elif op == "memory_edit":
                row = next((r for r in self.store.memories() if r["id"] == value["id"]), None)
                if row is None or row["revision"] != value.get("revision"):
                    raise ValueError("Воспоминание изменилось. Обнови данные.")
                self.store.replace_fact(row["id"], value["content"])
            elif op == "habits":
                if type(value.get("enabled")) is not bool:
                    raise ValueError("Неверное состояние обучения.")
                self.store.set_preference("memory", "habits_opt_in", value["enabled"])
            elif op == "signals":
                if not self.conv.tools.automations:
                    raise ValueError("Редактор мест недоступен.")
                await self.conv.tools.automations.locations.refresh_signals(force=True)
            elif op == "probe":
                if value["id"] == "calendar":
                    self.bus.request("dashboard", {"op": "calendar_refresh"})
                    self.publish(message="Проверяю календарь через существующее подключение.")
                    return
                if not self.conv.tools.automations:
                    raise ValueError("Интеграции недоступны.")
                await self.conv.tools.automations.probe(value["id"])
            elif op == "calendar":
                self.bus.request(
                    "dashboard",
                    {
                        "op": "calendar_connect"
                        if value.get("enabled") is True
                        else "calendar_disable"
                    },
                )
                self.publish(message="Запрос передан разделу календаря.")
            elif op == "reset":
                if value.get("confirm") is not True:
                    raise ValueError("Подтверди сброс выбранной категории.")
                category = value["category"]
                if category == "personality":
                    from backend.personality import DEFAULT_TRAITS, Communication

                    profile = self.profiles.active()
                    if profile.builtin:
                        profile = self.profiles.copy(profile.id, "Мой характер")
                    self.profiles.save(
                        profile.model_copy(
                            update={
                                "traits": DEFAULT_TRAITS.copy(),
                                "communication": Communication(),
                                "instructions": "",
                            }
                        )
                    )
                    self.profiles.select(profile.id, reason="Сброшены параметры характера")
                elif category == "places":
                    locations = self.conv.tools.automations.locations
                    locations.select("auto")
                    for place in locations.list():
                        locations.save(place.model_copy(update={"signals": []}))
                    self.conv.tools.automations.publish()
                elif category == "integrations":
                    access = self.value("access")
                    tools = {t for group in self.integrations() for t in group["tools"]}
                    access["disabled_tools"] = [
                        t for t in access["disabled_tools"] if t not in tools
                    ]
                    await self.save("access", access)
                    self.bus.request("dashboard", {"op": "calendar_disable"})
                elif category == "advanced":
                    from backend.hud.dashboard import DEFAULT_SETTINGS

                    await self.save("quiet", CATEGORIES["quiet"]().model_dump())
                    await self.save("notifications", CATEGORIES["notifications"]().model_dump())
                    self.store.set_preference("dashboard", "settings", DEFAULT_SETTINGS)
                    self.bus.request("dashboard", {"op": "settings", "value": DEFAULT_SETTINGS})
                else:
                    from backend.config import Settings

                    fresh = Settings()
                    self.defaults.update(
                        studio=Studio(
                            voice=fresh.voice_name,
                            pronunciations=dict(fresh.tts.pronunciations),
                            wake_phrase=fresh.wakeword.phrase or "эй чушпан",
                            silence_ms=fresh.vad.silence_ms,
                        ).model_dump(),
                        models=Models(
                            provider=fresh.llm.provider,
                            local_model=fresh.llm.model,
                            cloud_model=fresh.gemini.model,
                            cloud_fallback=fresh.gemini.fallback_model or "",
                            fallback=fresh.llm.fallback,
                        ).model_dump(),
                    )
                    await self.save(
                        category, self.defaults.get(category) or CATEGORIES[category]().model_dump()
                    )
            elif op in {"export", "import_preview", "import_apply"}:
                await self.backup(value)
                return
            else:
                raise ValueError("Неизвестная операция настроек.")
            self.publish(message="Сохранено. История разговоров не изменена.")
        except (ValueError, TypeError, KeyError, sqlite3.Error, ToolError) as exc:
            self.publish(
                error=str(exc)
                if not contains_secret(str(exc))
                else "Проверь настройки: секреты не принимаются."
            )
        except Exception:
            self.publish(error="Компонент недоступен. Изменение не подтверждено.")

    async def save(self, category, values):
        if category not in CATEGORIES:
            raise ValueError("Неизвестная категория.")
        selected = CATEGORIES[category].model_validate(values)
        if category == "commands" and self.loop and self.loop.busy:
            raise ValueError("Дождись завершения текущей команды перед изменением контекста.")
        if category == "studio":
            self.validate_studio(selected.model_dump())
        if category == "access" and set(selected.disabled_tools) - set(self.conv.tools.names):
            raise ValueError("Неизвестное действие.")
        self.store.set_preference("settings", category, selected.model_dump())
        if category in {"studio", "models", "about", "commands"}:
            apply_persisted(self.s, self.store)
        if category == "commands":
            self.conv.controls.ttl_s = self.s.tools.control_context_s
            self.conv.controls.default_brightness_device = self.s.tools.default_brightness_device
            self.conv.controls.ambiguity_rule = self.s.tools.ambiguity_rule
            self.conv.controls.clear()
        if category == "studio":
            self.apply_studio(selected.model_dump())
        if category == "models":
            from backend.brain.providers import create_llm

            await self.conv.settle()
            client = create_llm(self.s)
            if hasattr(client, "on_status"):
                client.on_status = self.bus.publish
            self.clients.append(client)
            self.conv.llm = client

    def validate_studio(self, values):
        selected = Studio.model_validate(values)
        if selected.voice not in self.snapshot()["voices"]:
            raise ValueError("Голос не установлен для текущего движка.")
        if selected.input_device != self.s.audio.input_device:
            import sounddevice as sd

            sd.check_input_settings(
                device=selected.input_device, channels=1, dtype="int16", samplerate=16000
            )
        if self.s.tts.engine != "silero" and selected.pitch != "medium":
            raise ValueError("Текущий движок не поддерживает высоту голоса.")
        if self.s.tts.engine not in {"silero", "piper"} and selected.rate != "medium":
            raise ValueError("Текущий движок не поддерживает скорость речи.")

    def apply_studio(self, values):
        if not self.loop:
            return
        selected = Studio.model_validate(values)
        speaker = self.loop.speaker
        change_input = getattr(self.loop, "change_input_device", None)
        if change_input:
            change_input(selected.input_device)
        if hasattr(speaker, "speaker"):
            speaker.speaker = selected.voice
        if hasattr(speaker, "pronunciations"):
            speaker.pronunciations = dict(selected.pronunciations)
            speaker.rate, speaker.pitch = selected.rate, selected.pitch
            speaker.volume, speaker.pause_ms = selected.volume / 100, selected.pause_ms
            speaker.normalize_numbers, speaker.normalize_english = (
                selected.normalize_numbers,
                selected.normalize_english,
            )
            speaker.length_scale = self.s.tts.length_scale
            for name in ("_ready", "wait_audio"):
                if cache := getattr(speaker, name, None):
                    cache.clear()
        self.loop.wake_phrase = selected.wake_phrase
        self.loop.segmenter = __import__(
            "backend.audio.vad", fromlist=["UtteranceSegmenter"]
        ).UtteranceSegmenter(self.s.vad)
        self.loop._publish_voice()

    def capture(self, text, confident):
        if not self.capture_target:
            return False
        until, self.capture_target = self.capture_target, None
        if self.capture_timer:
            self.capture_timer.cancel()
        if time.monotonic() > until or not confident:
            self.publish(error="Тестовая речь распознана неуверенно или время истекло.")
        else:
            self.publish(captured=text, message="Тестовая речь распознана. Команды не выполнялись.")
        return True

    def capture_wake(self, text, *, accepted, reason):
        if self.wake_test_until is None:
            return False
        active = time.monotonic() <= self.wake_test_until
        self.wake_test_until = None
        if self.capture_timer:
            self.capture_timer.cancel()
        self.publish(
            captured=text,
            message="Wake-фраза подтверждена. Действия не выполнялись."
            if active and accepted
            else "Wake-фраза отклонена: " + reason,
        )
        return True

    def stop_capture(self):
        active = self.capture_target or self.wake_test_until
        self.capture_target = self.wake_test_until = None
        if self.capture_timer:
            self.capture_timer.cancel()
        if active and self.loop:
            self.loop.command("sleep")

    async def expire_capture(self):
        await asyncio.sleep(20)
        if self.capture_target or self.wake_test_until:
            self.capture_target = self.wake_test_until = None
            if self.loop:
                self.loop.command("sleep")
            self.publish(message="Время теста микрофона истекло.")

    async def preview(self, value):
        from urllib.parse import urlparse

        from backend.brain.llm import OllamaClient
        from backend.brain.prompts import build_system_prompt

        if urlparse(self.s.llm.host).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Для приватного предпросмотра нужен локальный адрес Ollama.")

        text = value.get("text", "").strip()
        if not 1 <= len(text) <= 1000 or contains_secret(text):
            raise ValueError("Введи тестовую реплику до 1000 символов, без секретов.")
        profiles = (
            [PersonalityProfile.model_validate(p) for p in value["drafts"]]
            if value.get("drafts")
            else [self.profiles.get(i) for i in value.get("ids", [])]
        )
        if not 1 <= len(profiles) <= 2:
            raise ValueError("Выбери один или два профиля.")
        model = value.get("model", self.s.llm.model)
        if model not in {self.s.llm.model, *self.model_status.get("installed", [])}:
            raise ValueError("Модель не найдена среди установленных. Обнови проверку моделей.")
        client = OllamaClient(
            self.s.llm.model_copy(update={"model": model, "temperature": 0, "think": False})
        )
        results = []
        try:
            for p in profiles:
                settings = self.s.model_copy(
                    update={
                        "user": self.s.user.model_copy(
                            update={"address_mode": p.communication.address}
                        ),
                        "persona": self.s.persona.model_copy(
                            update={"file": None, "style": self.profiles.note(p)}
                        ),
                    }
                )
                messages = [
                    {
                        "role": "system",
                        "content": build_system_prompt(settings, text_only=True),
                    },
                    {"role": "user", "content": text},
                ]
                started, first, pieces = time.perf_counter(), None, []
                async with asyncio.timeout(90):
                    with request_scope(RequestContext(self.conv.device, text)):
                        async for chunk in client.chat_stream(messages, []):
                            if chunk.tool_calls:
                                raise ValueError("Предпросмотр не выполняет действий.")
                            if chunk.content:
                                first = first or time.perf_counter() - started
                                pieces.append(chunk.content)
                                self.bus.publish(
                                    {
                                        "type": "settings_preview",
                                        "id": p.id + ":" + model if value.get("speed") else p.id,
                                        "name": p.name,
                                        "text": "".join(pieces),
                                        "done": False,
                                        "model": model,
                                    }
                                )
                answer = "".join(pieces)
                if not answer.strip():
                    raise ValueError("Модель вернула пустой ответ.")
                result = {
                    "id": p.id + ":" + model if value.get("speed") else p.id,
                    "name": p.name,
                    "text": answer,
                    "first_token_s": first,
                    "total_s": time.perf_counter() - started,
                    "model": model,
                }
                if value.get("speak") is True:
                    result.update(await self.speak(answer))
                results.append(result)
                self.bus.publish({"type": "settings_preview", **result, "done": True})
        finally:
            await client.aclose()
        self.last_preview = results
        self.publish(
            message=(
                "Проверено локально, без инструментов, истории и облачных API. "
                "Активный профиль не изменён."
            )
        )

    async def speak(self, text):
        if not self.loop or not isinstance(text, str) or not 0 < len(text) <= 5000:
            raise ValueError("Голосовой тракт недоступен или текст слишком длинный.")
        from backend.audio.sentences import SentenceSplitter
        from backend.latency import Profile, current
        from backend.state import State

        self.require_idle()
        quiet = __import__("backend.settings_models", fromlist=["silent_reply"]).silent_reply(
            self.store, self.s
        )
        if quiet:
            raise ValueError("Озвучка отключена тихими часами или текстовым режимом.")
        timing = Profile(kind="voice")
        token = current.set(timing)

        async def sentences():
            splitter = SentenceSplitter(protect_literals=True)
            for sentence in splitter.feed(text + " "):
                yield sentence
            if rest := splitter.flush():
                yield rest

        self.loop.state.to(State.SPEAKING)
        old_caption = getattr(self.loop.speaker, "on_sentence", None)
        self.loop.speaker.on_sentence = None
        task = asyncio.create_task(self.loop.speaker.speak(sentences()))
        self.loop.task = task
        try:
            completed = await task
            if not completed:
                raise ValueError("Тестовое воспроизведение прервано.")
            return {"first_sound_s": timing.marks.get("first_content_audio")}
        finally:
            self.loop.speaker.on_sentence = old_caption
            current.reset(token)
            if self.loop.task is task:
                self.loop.task = None
                self.loop._go_to_sleep()

    async def inspect_models(self):
        import httpx

        self.model_status = {
            "ollama": "not_checked",
            "installed": [],
            "gemini": self.conv.llm.snapshot() if hasattr(self.conv.llm, "snapshot") else None,
            "tts": "ready"
            if self.loop and getattr(self.loop.speaker, "_voice", None) is not None
            else "not_checked",
            "streaming": True,
        }
        async with httpx.AsyncClient(timeout=3) as client:
            try:
                response = await client.get(self.s.llm.host.rstrip("/") + "/api/tags")
                response.raise_for_status()
                self.model_status.update(
                    ollama="ready",
                    installed=[r["name"] for r in response.json().get("models", [])],
                )
            except httpx.HTTPError:
                self.model_status["ollama"] = "error"

    def save_rule(self, value):
        from backend.automation.models import Block, Condition, Scenario, Trigger

        service = self.conv.tools.automations
        profile = self.profiles.get(value["profile_id"])
        if not service:
            raise ValueError("Движок сценариев недоступен.")
        kind = value.get("kind", "manual")
        conditions = []
        if kind == "location":
            place = value["place_id"]
            if place != "general":
                service.locations.get(place)
            conditions = [Condition(source="location", key="place", value=place)]
        elif kind == "time":
            conditions = [
                Condition(source="time", key="hour", operator="gt", value=int(value["hour"]) - 1)
            ]
        elif kind not in {"manual", "schedule"}:
            raise ValueError("Выбери ручное включение, время или место.")
        trigger = Trigger(
            kind="condition" if conditions else "schedule" if kind == "schedule" else "manual",
            at=value.get("at", "22:00"),
        )
        scenario = Scenario(
            name="Характер: " + profile.name,
            description="Характер: правило из настроек",
            phrases=[value["phrase"]] if value.get("phrase") else [],
            triggers=[trigger],
            conditions=conditions,
            actions=[
                Block(capability="assistant.personality", parameters={"profile_id": profile.id})
            ],
        )
        service.validate_document(scenario)
        service.store.save(scenario)
        service.publish()

    def approve_memory(self, value):
        pending = self.store.preference("memory", "pending") or []
        item = next((r for r in pending if r["id"] == value["id"]), None)
        if not item:
            raise ValueError("Предложение памяти устарело.")
        if value.get("approve") is True:
            previous = self.store.db.execute(
                "SELECT revision FROM facts WHERE semantic_key=? AND project_id IS ?",
                (item["key"], item.get("project_id")),
            ).fetchone()
            if (previous[0] if previous else None) != item.get("base_revision"):
                raise ValueError(
                    "Воспоминание изменилось после предложения. Проверь текущую версию."
                )
            fact_id, _ = self.store.save_memory(
                item["content"],
                category=item["category"],
                key=item["key"],
                source="ui_confirmed",
                project_id=item.get("project_id"),
                expected_revision=item.get("base_revision"),
            )
            from backend.memory.intelligence import evidence

            evidence(self.store, fact_id, item.get("message_id"))
        else:
            with self.store.memory_transaction():
                self.store._suppress(item["content"], item["key"])
        self.store.set_preference(
            "memory", "pending", [r for r in pending if r["id"] != item["id"]]
        )

    def clear_memory(self, value):
        if value.get("confirm") is not True or not isinstance(value.get("ids"), list):
            raise ValueError("Выбери записи и подтверди очистку.")
        existing = {r["id"] for r in self.store.memories()}
        if any(type(i) is not int or i not in existing for i in value["ids"]):
            raise ValueError("Список памяти изменился.")
        for identifier in value["ids"]:
            self.store.forget_fact(identifier)

    async def backup(self, value):
        from backend.settings_backup import backup_request

        await backup_request(self, value)

    async def close(self):
        self.closed = True
        self.profiles.on_change = None
        for task in list(self.jobs):
            task.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        for client in self.clients:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def cancel_previews(self):
        tasks = [
            t
            for t in self.jobs
            if t is not asyncio.current_task() and t.get_name() == "settings-preview"
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
