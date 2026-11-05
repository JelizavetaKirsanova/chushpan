"""Deterministic voice music commands over the existing media integrations."""

import asyncio
import re
import sqlite3
from dataclasses import dataclass, replace

from backend.security import command_text
from backend.tools.registry import ToolError

FAVOURITE_NAME = r"(?:favou?rite songs|феврит сонгс|фейворит сонгс|фэйворит сонгс)"


def load_provider(settings, store) -> None:
    if store is None:
        return
    try:
        application = store.preference("media", "youtube_application")
        if isinstance(application, dict):
            from backend.config import YouTubeMusicConfig

            config = YouTubeMusicConfig.model_validate(
                {**settings.media.youtube_music.model_dump(), **application}
            )
            settings.media.youtube_music = config
        choice = store.preference("media", "provider")
        if (
            isinstance(choice, dict)
            and "provider" in choice
            and choice.get("provider")
            in {
                None,
                "spotify",
                "youtube_music",
            }
        ):
            settings.media.provider = choice["provider"]
    except (sqlite3.Error, ValueError):
        pass  # keep the configured provider if SQLite cannot be read


def bind_settings(bus, settings, store) -> None:
    def publish(error=None):
        try:
            choice = store.preference("media", "provider")
        except sqlite3.Error:
            choice = None
        saved = choice.get("provider") if isinstance(choice, dict) else settings.media.provider
        bus.publish(
            {
                "type": "music_settings",
                "current": settings.media.provider,
                "saved": saved,
                "restart_required": saved != settings.media.provider,
                "error": error,
            }
        )

    def save(value):
        if value not in ("spotify", "youtube_music", "unselected"):
            publish(error="Выбери YouTube Music или Spotify.")
            return
        try:
            selected = None if value == "unselected" else value
            store.set_preference("media", "provider", {"provider": selected})
            bus.publish(
                {
                    "type": "music_settings",
                    "current": settings.media.provider,
                    "saved": selected,
                    "restart_required": selected != settings.media.provider,
                    "error": None,
                }
            )
        except (sqlite3.Error, OSError):
            publish(error="Не удалось сохранить сервис. Текущие настройки сохранены.")

    bus.handle("music_settings", save)
    publish()


@dataclass(frozen=True)
class MusicIntent:
    action: str
    query: str = ""
    kind: str = "auto"
    requested_provider: str | None = None


def parse(text: str) -> MusicIntent | None:
    command = command_text(text).strip(".!? ")
    command = re.sub(r"^включай\b", "включи", command)
    if re.fullmatch(
        rf'(?:включи|поставь) (?:плейлист )?(?:мой )?[«"]?{FAVOURITE_NAME}[»"]?(?: тогда)?',
        command,
    ):
        return MusicIntent("play", "Favourite Songs", "playlist")
    service = re.fullmatch(r"(.+) (?:в|через) (spotify|youtube music)", command)
    if service:
        raw = re.search(r"(.+) (?:в|через) (Spotify|YouTube Music)[.!? ]*$", text, re.I)
        intent = parse(raw[1] if raw else service[1])
        if intent:
            return replace(
                intent, requested_provider="spotify" if service[2] == "spotify" else "youtube_music"
            )
    exact = {
        "включи музыку": MusicIntent("play"),
        "поставь музыку": MusicIntent("play"),
        "включи что-нибудь спокойное": MusicIntent("calm"),
        "включи что нибудь спокойное": MusicIntent("calm"),
        "включи последнюю песню": MusicIntent("resume"),
        "поставь мой плейлист": MusicIntent("playlist"),
        "включи мой плейлист": MusicIntent("playlist"),
        "включи мой любимый плейлист": MusicIntent("playlist"),
        "открой мой любимый плейлист": MusicIntent("open_playlist"),
        "открой мой плейлист": MusicIntent("open_playlist"),
        "покажи мои плейлисты": MusicIntent("list_playlists"),
        "посмотри доступные плейлисты": MusicIntent("list_playlists"),
        "следующий трек": MusicIntent("next"),
        "предыдущий трек": MusicIntent("previous"),
        "пауза": MusicIntent("pause"),
        "пауза музыки": MusicIntent("pause"),
        "продолжай": MusicIntent("resume"),
        "продолжи музыку": MusicIntent("resume"),
        "сделай потише": MusicIntent("quieter"),
        "сделай громче": MusicIntent("louder"),
        "что сейчас играет": MusicIntent("status"),
        "проверь youtube music": MusicIntent("diagnose"),
        "проверь разрешения youtube music": MusicIntent("diagnose"),
        "разреши управление youtube music": MusicIntent("request_access"),
    }
    if re.match(r"включи|поставь", command) and (
        re.search(r"(?:в|через) (?:apple music|эпл мьюзик)$", command)
        or command in {"включи apple music", "поставь apple music"}
    ):
        return MusicIntent("apple_music")
    if command in exact:
        return exact[command]
    match = re.fullmatch(r"(?:включи|поставь) (?:песню|трек) (.+)", command)
    if match:
        original = re.search(r"(?:песню|трек)\s+(.+)", text, re.I)
        return MusicIntent("play", original[1].strip(".!? ") if original else match[1], "song")
    match = re.fullmatch(r"(?:включи|поставь) плейлист (.+)", command)
    if match:
        original = re.search(r"плейлист\s+(.+)", text, re.I)
        return MusicIntent("play", original[1].strip(".!? ") if original else match[1], "playlist")
    match = re.fullmatch(r"(?:включи|поставь) (?:группу|исполнителя) (.+)", command)
    if match:
        original = re.search(r"(?:группу|исполнителя)\s+(.+)", text, re.I)
        return MusicIntent("play", original[1].strip(".!? ") if original else match[1], "artist")
    match = re.fullmatch(r"(?:включи|поставь) (.+)", command)
    if match:
        # A bare artist name is accepted only from a small known set; other queries need
        # an explicit song/artist/playlist marker rather than guessing what should play.
        if match[1] in {"нервы", "adele", "queen", "rammstein", "stray kids", "the weeknd"}:
            original = re.search(r"(?:включи|поставь)\s+(.+)", text, re.I)
            return MusicIntent(
                "play", original[1].strip(".!? ") if original else match[1], "artist"
            )
    return None


def music_context(text: str) -> bool:
    intent = parse(text)
    return bool(
        re.search(r"музык|плейлист|трек|песн|spotify|youtube music", text, re.I)
        or (intent and intent.action != "resume")
    )


def route(text: str, *, context: bool = False) -> tuple[str, dict] | None:
    intent = parse(text)
    if intent and (command_text(text).strip(".!? ") != "продолжай" or context):
        return "music_command", {"command": text}
    return None


def message(result: dict) -> str:
    if result.get("error"):
        return str(result.get("error") or result.get("message") or "Действие завершилось ошибкой.")
    if result.get("status") == "not_found":
        if result.get("offer_playlist_list") is True:
            return "Плейлист не удалось найти. Могу посмотреть доступные плейлисты."
        return "Музыка не найдена. Уточни название песни, исполнителя или плейлиста."
    if result.get("status") in {"choose", "ambiguous"}:
        choices = result.get("candidates", result.get("playlists", []))
        titles = [str(item["title"]) for item in choices[:5] if item.get("title")]
        return (
            "Выбери плейлист или композицию: " + ", ".join(titles) + "."
            if titles
            else "Не выбран плейлист для этого запроса. Назови нужный плейлист."
        )
    if result.get("execution_status") == "failed":
        return str(result.get("message") or "Действие завершилось ошибкой.")
    volume = result.get("spotify_volume", result.get("volume"))
    if result.get("ok") and result.get("action") == "volume" and isinstance(volume, (int, float)):
        return f"Громкость музыки: {volume} процентов."
    if (
        result.get("message")
        and result.get("state") != "playing"
        and (
            result.get("execution_status")
            or result.get("code")
            or result.get("diagnostics")
            or result.get("action") in {"opened", "volume"}
        )
    ):
        return str(result["message"])
    if result.get("error"):
        return str(result["error"])
    if result.get("state") == "playing" and result.get("ok"):
        if result.get("playback_confirmed") is not True:
            return "Команда отправлена, но запуск воспроизведения подтвердить не удалось."
        track, artist = result.get("track", ""), result.get("artist", "")
        return (
            "Играет: " + " — ".join(str(x) for x in (artist, track) if x) + "."
            if track or artist
            else "Воспроизведение подтверждено."
        )
    if result.get("state") == "paused" and result.get("ok"):
        title = " — ".join(
            str(result.get(key, "")) for key in ("artist", "track") if result.get(key)
        )
        return "Воспроизведение на паузе" + (": " + title if title else "") + "."
    if result.get("status") in {"choose", "ambiguous"} or result.get("results"):
        return "Найдено несколько вариантов. Уточни исполнителя, песню или название плейлиста."
    if result.get("state") == "closed":
        return "Музыкальное приложение закрыто; воспроизведение не подтверждено."
    return "Результат воспроизведения не подтверждён."


def classify_error(error: Exception) -> tuple[str, str]:
    from backend.media.music_safari import MusicBrowserError

    if isinstance(error, MusicBrowserError):
        return error.code, str(error)
    if isinstance(error, TimeoutError):
        return "service_timeout", "Музыкальный сервис не ответил вовремя; запуск не подтверждён."
    # No raw exception, query, URL, login or token goes to diagnostics/UI.
    text = str(error).casefold()
    if re.search(r"не найден safari web app|not installed|не установ", text):
        return (
            "app_not_installed",
            "Музыкальное приложение не найдено. Установи Spotify или Safari Web App YouTube Music.",
        )
    if "screen recording" in text or "запись экрана" in text:
        return "permission_denied", (
            "Нужна Запись экрана для поиска Spotify. Разреши её процессу запуска Чушпана "
            "в Конфиденциальность и безопасность → Запись экрана."
        )
    if "automation" in text or "-1743" in text:
        return "permission_denied", (
            "Нужна Автоматизация → Spotify / System Events для процесса запуска Чушпана. "
            "Настройка находится в Конфиденциальность и безопасность."
        )
    if re.search(r"accessibility|разреш|специальные возможности", text):
        return (
            "permission_denied",
            "Нужны Специальные возможности для процесса запуска Чушпана. "
            "Открой Конфиденциальность и безопасность → Специальные возможности.",
        )
    if re.search(r"авторизац|войд|sign.in|login|401|403|unauthor", text):
        return (
            "authorization_required",
            "Нужна авторизация в музыкальном приложении. Войди вручную; "
            "логины и токены в чат не присылай.",
        )
    if re.search(r"no.active.device|device.not.found|не выбрано.{0,20}устройств", text):
        return (
            "device_required",
            "Выбери доступное устройство воспроизведения в музыкальном сервисе.",
        )
    if re.search(r"не найден|found nothing|no results|not found", text):
        return "track_not_found", "Музыка не найдена однозначно. Уточни название или исполнителя."
    if re.search(r"каталог|catalog|библиотек", text):
        return (
            "catalog_unavailable",
            "Музыкальный каталог или библиотека недоступны. Проверь доступ в приложении.",
        )
    if re.search(r"не подтвержд|blocked|заблокир|кнопк|playback", text):
        return (
            "playback_unconfirmed",
            "Воспроизведение не подтверждено. Проверь плеер вручную; "
            "автоматически повторно не запускаю.",
        )
    if re.search(r"not support|не поддерж|unsupported", text):
        return "unsupported", "Выбранный сервис не поддерживает эту команду."
    return "service_unavailable", "Музыкальный сервис недоступен. Проверь приложение и подключение."


class MusicCommands:
    def __init__(self, settings, media):
        self.s, self.media = settings, media
        self.lock = asyncio.Lock()
        self._last_play: tuple | None = None
        self._last_result: dict | None = None
        self._uncertain: tuple | None = None

    def _receipt(self, result: dict) -> dict:
        app = getattr(getattr(self.media, "youtube", None), "app", None)
        sent = bool(
            result.get("playback_confirmed")
            or result.get("execution_status") == "submitted"
            or getattr(app, "stages", {}).get("play_submitted")
        )
        status = "confirmed" if result.get("ok") else "failed"
        if result.get("deduplicated") or result.get("action") == "status":
            sent = False
        return {
            **result,
            "ok": bool(result.get("ok")),
            "playback_confirmed": bool(
                result.get("ok")
                and result.get("state") == "playing"
                and result.get("playback_confirmed") is True
            ),
            "execution_status": status,
            "execution_events": ["requested", *(["submitted"] if sent else []), status],
        }

    async def execute(self, command: str) -> dict:
        result = await self._execute_command(command)
        if "execution_status" not in result:
            result = {
                **result,
                "ok": False,
                "execution_status": "failed",
                "playback_confirmed": False,
                "execution_events": ["requested", "failed"],
            }
        return result

    async def _execute_command(self, command: str) -> dict:
        intent = parse(command)
        if intent is None:
            return {"error": "Музыкальная команда не поддерживается.", "code": "unsupported"}
        if intent.action == "apple_music":
            return {
                "code": "unsupported",
                "message": "Apple Music пока не подключён. Поддерживаются Spotify и YouTube Music; "
                "другой сервис не запускаю без выбора.",
            }
        provider = self.s.media.provider
        if intent.requested_provider and intent.requested_provider != provider:
            return {
                "code": "provider_mismatch",
                "message": "Запрошен другой музыкальный сервис. "
                "Выбери его в настройках и перезапусти Чушпана; текущий сервис не запускаю.",
            }
        intent = replace(intent, requested_provider=None)
        if provider not in {"youtube_music", "spotify"}:
            return {
                "code": "provider_required",
                "message": "Выбери музыкальный сервис в настройках: YouTube Music или Spotify. "
                "Apple Music пока не поддерживается.",
            }
        if provider == "youtube_music" and not self.s.media.youtube_music.enabled:
            return {
                "code": "service_disabled",
                "message": "YouTube Music отключён в настройках. "
                "Другой сервис автоматически не выбираю.",
            }
        async with self.lock:
            app = getattr(getattr(self.media, "youtube", None), "app", None)
            if isinstance(getattr(app, "stages", None), dict):
                for key in app.stages:
                    app.stages[key] = False
            try:
                return self._receipt(await self._execute(intent, provider))
            except (ToolError, OSError, TimeoutError) as exc:
                code, text = classify_error(exc)
                if code in {
                    "playback_unconfirmed",
                    "playback_blocked",
                    "service_unavailable",
                    "service_timeout",
                }:
                    self._uncertain = (provider, intent)
                if (
                    intent.action in {"playlist", "open_playlist"}
                    and code
                    not in {
                        "automation_denied",
                        "automation_unknown",
                        "javascript_disabled",
                        "permission_denied",
                        "authorization_required",
                        "safari_closed",
                    }
                    and provider == "youtube_music"
                ):
                    text += " Могу посмотреть доступные плейлисты."
                return self._receipt({"ok": False, "code": code, "error": text})

    async def _execute(self, intent: MusicIntent, provider: str) -> dict:
        if intent.action == "list_playlists":
            if provider != "youtube_music":
                return {"ok": False, "code": "unsupported", "message": "Нужен YouTube Music."}
            from backend.brain.followups import playlist_reply

            result = await self.media.youtube.playlist(action="list")
            return {
                **result,
                "ok": not bool(result.get("error")),
                "message": playlist_reply(result),
            }
        if intent.action in {"diagnose", "request_access"}:
            if provider != "youtube_music" or not hasattr(self.media.youtube.app, "diagnose"):
                return {
                    "ok": False,
                    "code": "unsupported",
                    "message": "Диагностика выбранного приложения недоступна.",
                }
            app = self.media.youtube.app
            if intent.action == "request_access":
                await app.request_access()
            report = await app.diagnose()
            summary = report.get("message") or (
                "Automation Safari разрешена. "
                + (
                    "Вкладка YouTube Music найдена. "
                    if report.get("tab_found")
                    else "Вкладка YouTube Music не найдена. "
                )
                + "Воспроизведение "
                + ("подтверждено." if report.get("playback_confirmed") else "не подтверждено.")
            )
            return {
                "ok": not bool(report.get("error_code")),
                "code": report.get("error_code"),
                "message": summary,
                "diagnostics": report,
            }
        signature = (provider, intent)
        if signature == self._uncertain:
            return {
                "ok": False,
                "code": "playback_unconfirmed",
                "error": "Предыдущий запуск не подтверждён. "
                "Проверь плеер; повторный запуск пропущен.",
            }
        if intent.action in {"play", "playlist", "open_playlist", "calm"}:
            if signature == self._last_play and self._last_result:
                now = await self.media.now_playing()
                before = self._last_result
                if (
                    now.get("state") == "playing"
                    and now.get("track")
                    and (now.get("track"), now.get("artist"))
                    == (before.get("track"), before.get("artist"))
                ):
                    app = getattr(getattr(self.media, "youtube", None), "app", None)
                    if provider == "youtube_music" and hasattr(app, "wait_playing"):
                        if self.s.media.youtube_music.transport == "safari":
                            now = await app.wait_playing(
                                title=before.get("track", ""),
                                playlist=before.get("playlist_id", ""),
                                playlist_title=before.get("playlist_title", ""),
                            )
                        else:
                            now = await app.wait_playing(before.get("track", ""))
                    return {**now, "ok": True, "playback_confirmed": True, "deduplicated": True}
                if now.get("state") == "paused" and now.get("track") == before.get("track"):
                    return await self._execute(MusicIntent("resume"), provider)
                if now.get("state") == "unknown":
                    raise ToolError("Воспроизведение не подтверждено")
            if provider == "spotify" and (
                intent.action in {"playlist", "calm"} or intent.kind == "playlist"
            ):
                return {
                    "code": "playlist_required",
                    "message": "Точный запуск своего плейлиста в Spotify пока не реализован. "
                    "Для своих и спокойных плейлистов выбери YouTube Music.",
                }
            kwargs = {"kind": intent.kind} if provider == "youtube_music" else {}
            if intent.action in {"playlist", "open_playlist"} and provider == "youtube_music":
                kwargs["kind"] = "playlist"
            if intent.action == "open_playlist":
                kwargs["open_only"] = True
            if intent.action == "calm":
                kwargs["situation"] = "rest"
            result = await self.media.play_music(intent.query, **kwargs)
            if (
                intent.action == "open_playlist"
                and result.get("ok")
                and result.get("action") == "opened"
            ):
                return {
                    **result,
                    "message": "Открыл страницу сохранённого плейлиста. "
                    "Воспроизведение не запускал.",
                }
            if (
                intent.kind == "playlist" or intent.action in {"playlist", "open_playlist"}
            ) and result.get("status") in {
                "not_found",
                "unavailable",
            }:
                return {
                    **result,
                    "offer_playlist_list": True,
                    "message": "Плейлист не удалось найти. Могу посмотреть доступные плейлисты.",
                }
        elif intent.action == "status":
            result = await self.media.now_playing()
            app = getattr(getattr(self.media, "youtube", None), "app", None)
            if (
                result.get("state") == "playing"
                and provider == "youtube_music"
                and hasattr(app, "wait_playing")
            ):
                result = await app.wait_playing(result.get("track", ""))
            return {
                **result,
                "action": "status",
                "ok": result.get("state") in {"playing", "paused"},
                "playback_confirmed": bool(
                    result.get("track") and result.get("state") == "playing"
                ),
            }
        elif intent.action in {"quieter", "louder"}:
            return {
                **await self.media.media_volume(
                    direction="down" if intent.action == "quieter" else "up"
                ),
                "action": "volume",
            }
        else:
            # These are music commands: they must not accidentally control the night video.
            target = "music"
            before_control = (
                await self.media.now_playing() if intent.action in {"next", "previous"} else {}
            )
            result = await self.media.media_control(intent.action, target=target)
            if provider == "spotify" and intent.action == "resume" and result.get("ok"):
                # Spotify's command receipt has no state; read its scripting API
                # before allowing a playback confirmation.
                observed = await self.media.now_playing()
                result = {
                    **result,
                    **observed,
                    "playback_confirmed": observed.get("state") == "playing",
                }
        if (
            intent.action in {"play", "playlist", "calm", "resume"}
            and (result.get("status") not in {"not_found", "choose", "ambiguous", "unavailable"})
            and not (
                result.get("ok")
                and result.get("state") == "playing"
                and result.get("playback_confirmed") is True
            )
        ):
            raise ToolError("Запуск воспроизведения не подтверждён доступным API")
        if result.get("ok"):
            state = result.get("state")
            if state not in {"playing", "paused"}:
                now = await self.media.now_playing()
                result = {**result, **now}
            expected = {"paused"} if intent.action == "pause" else {"playing"}
            if intent.action in {"next", "previous"}:
                expected = {"playing", "paused"}
                before_id = (before_control.get("track"), before_control.get("artist"))
                after_id = (result.get("track"), result.get("artist"))
                if not after_id[0] or before_id == after_id:
                    raise ToolError("Переключение воспроизведения не подтверждено")
            if result.get("state") not in expected:
                raise ToolError("Воспроизведение не подтверждено")
            if intent.action in {"play", "playlist", "calm"}:
                self._last_play, self._last_result = signature, result
        return result


def music_tool(settings, media):
    from backend.tools.registry import Tool

    commands = MusicCommands(settings, media)
    return Tool(
        "music_command",
        "Локальное управление музыкой по текущей явной команде.",
        commands.execute,
        {"command": {"type": "string"}},
        ["command"],
        intent=lambda args, text: (
            command_text(args["command"]) == command_text(text) and parse(text) is not None
        ),
        reply=message,
    )
