"""Extensible, permission-aware public player interface over the existing Safari executor."""

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Protocol

from backend.cinema.classification import LABELS
from backend.cinema.models import local_video, web_url
from backend.cinema.playback import SourceValidator, playback_error
from backend.media.music_safari import SafariMusicTab
from backend.media.safari import process
from backend.permission_policy import requests_allowed
from backend.tools.registry import ToolError

PLAYER = Path(__file__).with_name("player.js").read_text()
PLAYER_ERRORS = {
    "player_changed": "Плеер или видео изменилось. Соответствие фильму нужно проверить заново.",
    "page_changed": "Страница изменилась или сайт открыл новую вкладку. "
    "Добавь точный адрес плеера и выбери нужную серию заново; управление другой вкладкой отменено.",
    "multiple_players": "На странице несколько плееров; автоматическое управление неоднозначно.",
    "no_player": "Доступный HTML5-плеер не найден. Возможно, видео находится в чужом iframe.",
    "player_loading": "HTML5-плеер найден, но источник не загрузил метаданные и длительность. "
    "Проверь загрузку видео на сайте или выбери другой плеер.",
    "protected_player": "Защищённый плеер: автоматическое управление не поддерживается.",
    "media_error": "Плеер сообщил об ошибке загрузки видео.",
    "cross_origin_player": "Видео находится в междоменном iframe. Safari не разрешает "
    "читать этот плеер: запусти его и fullscreen кнопкой самого сайта.",
}


class PlayerAdapter(Protocol):
    async def open(self, target: dict) -> dict: ...
    async def sample(self) -> dict: ...
    async def control(self, action: str, position: float = 0) -> dict: ...
    async def detach(self) -> None: ...


class CatalogueAdapter(Protocol):
    """Future official API integrations return metadata, never executable page scripts."""

    async def search(self, query: str) -> list[dict]: ...
    async def episodes(self, catalogue_key: str) -> list[dict]: ...


async def open_page(url):
    await process("/usr/bin/open", "-a", "Safari", web_url(url), timeout=10)


class ManualAdapter:
    def __init__(self, opener=open_page):
        self.opener = opener

    async def open(self, target):
        await self.opener(target["link"]["url"])
        return {
            "page_opened": True,
            "playback_confirmed": False,
            "mode": "manual",
            "manual_required": True,
            "message": "Страница открыта. Запусти плеер и укажи прогресс вручную.",
        }

    async def sample(self):
        raise ToolError("Источник работает в ручном режиме; время плеера недоступно.")

    async def control(self, action, position=0):
        raise ToolError("Управляй плеером на сайте; прогресс можно изменить в карточке.")

    async def detach(self):
        pass


class LocalFileAdapter(ManualAdapter):
    async def open(self, target):
        path = Path(local_video(target["link"]["url"]))
        if not path.is_file():
            raise ToolError("Видеофайл недоступен. Проверь сохранённый путь.")
        await process("/usr/bin/open", str(path), timeout=10)
        return {
            "file_opened": True,
            "playback_confirmed": False,
            "mode": "manual",
            "message": "Файл открыт во внешнем плеере. Автовосстановление позиции недоступно; "
            "укажи прогресс вручную в карточке.",
        }


class SafariBridge:
    def __init__(self, settings, executor=None):
        self.executor = executor or SafariMusicTab(settings)

    async def ensure_page(self, url, opener):
        web_url(url)
        script = f"""
tell application "Safari"
    set matches to {{}}
    repeat with w in windows
        repeat with n from 1 to count of tabs of w
            if URL of tab n of w is {json.dumps(url)} then set end of matches to {{id of w, n}}
        end repeat
    end repeat
    if (count of matches) > 1 then return "multiple"
    if (count of matches) is 0 then return "missing"
    set pair to item 1 of matches
    set targetWindow to window id (item 1 of pair)
    set current tab of targetWindow to tab (item 2 of pair) of targetWindow
    set index of window id (item 1 of pair) to 1
    activate
    return "found"
end tell
"""
        token = requests_allowed.set(False)
        try:
            try:
                result = await self.executor._run(script)
            except ToolError as exc:
                if getattr(exc, "code", None) != "safari_closed":
                    raise
                result = "missing"
            if result == "multiple":
                raise ToolError("Открыто несколько одинаковых страниц. Выбери одну в Safari.")
            if result == "missing":
                await opener(url)
            elif result != "found":
                raise ToolError("Safari не подтвердил выбор страницы.")
        finally:
            requests_allowed.reset(token)

    async def evaluate(self, url, payload):
        web_url(url)
        js = PLAYER + "(" + json.dumps({**payload, "url": url}, ensure_ascii=True) + ")"
        # Never use current tab or front window: bind every call to the exact saved URL.
        script = f"""
tell application "Safari"
    set matches to {{}}
    repeat with w in windows
        repeat with t in tabs of w
            if URL of t is {json.dumps(url, ensure_ascii=False)} then set end of matches to t
        end repeat
    end repeat
    if (count of matches) is not 1 then error "tab changed"
    set t to item 1 of matches
    return do JavaScript {json.dumps(js, ensure_ascii=False)} in t
end tell
"""
        # A background poll must never trigger permission dialogs or open Settings.
        token = requests_allowed.set(False)
        try:
            result = json.loads(await self.executor._run(script))
        except ToolError as exc:
            messages = {
                "tab_changed": PLAYER_ERRORS["page_changed"],
                "automation_unknown": "Разреши управление Safari в медиатеке → Поиск и Safari.",
                "safari_closed": "Safari закрыт или недоступен этому процессу.",
            }
            raise playback_error(
                getattr(exc, "code", "safari_unavailable"),
                messages.get(getattr(exc, "code", ""), str(exc)),
            ) from None
        except (ValueError, TypeError):
            raise ToolError("Safari не вернул состояние плеера.") from None
        finally:
            requests_allowed.reset(token)
        if not isinstance(result, dict):
            raise ToolError("Неверный ответ плеера.")
        if result.get("error"):
            raise playback_error(
                result["error"], PLAYER_ERRORS.get(result["error"], "Плеер недоступен.")
            )
        return result


class HTML5Adapter:
    def __init__(self, bridge, opener=open_page):
        self.bridge, self.opener = bridge, opener
        self.url = ""
        self.context = {}
        self.voice = ""
        self.expected = None
        self.binding = None
        self.content_verified = False
        self.direct_verification = None

    async def read(self, action="status", position=0):
        payload = {**self.context, "action": action, "position": position, "voice": self.voice}
        if self.expected and action in {
            "play",
            "seek",
            "fullscreen",
            "voice",
            "offer_gesture",
            "mute",
        }:
            before = await self.read()
            if not before.get("content_verified"):
                raise ToolError(before["classification_reason"])
            payload["binding"] = before.get("binding")
        state = await self.bridge.evaluate(self.url, payload)
        if self.expected and action == "status":
            proof = SourceValidator.player(
                self.expected,
                state.get("media_evidence") or {"live": True, "page_url": self.url},
                self.url if self.direct_verification else "",
                self.direct_verification,
            )
            state.update(proof)
            self.binding = state.get("binding")
            self.content_verified = proof["content_verified"]
        return state

    async def sample(self):
        return await self.read()

    async def open(self, target):
        self.url = target["link"]["url"]
        self.voice = target["link"].get("voice", "")
        self.direct_verification = target.get("direct_verification")
        self.expected = (
            {
                **target["item"],
                "season": target["episode"]["season"],
                "episode": target["episode"]["episode"],
            }
            if target.get("item")
            else None
        )
        diagnostic = target.get("diagnostic")

        async def stage(name, status, message):
            if diagnostic:
                await diagnostic(name, status, message)

        self.context = target.get("tracking", {})
        # Reuse a matching open page if available, otherwise open the explicit URL once.
        if hasattr(self.bridge, "ensure_page"):
            await self.bridge.ensure_page(self.url, self.opener)
        else:
            try:
                await self.sample()
            except ToolError:
                await self.opener(self.url)
        await stage("OPEN_SAFARI", "success", "Страница открыта или выбрана в Safari.")
        await stage("DETECT_PLAYER", "running", "Ожидаем доступный HTML5-плеер и метаданные.")
        deadline = time.monotonic() + 15
        while True:
            try:
                await self.sample()
                await stage("DETECT_PLAYER", "success", "Найден один доступный HTML5-плеер.")
                break
            except ToolError as exc:
                if getattr(exc, "code", "") in {"no_player", "player_loading"}:
                    with contextlib.suppress(ToolError):
                        await self.bridge.evaluate(self.url, {"action": "prepare"})
                if getattr(exc, "code", "") in {
                    "automation_denied",
                    "automation_unknown",
                    "javascript_denied",
                    "javascript_disabled",
                    "protected_player",
                }:
                    raise
                if time.monotonic() >= deadline:
                    await stage("DETECT_PLAYER", "limited", str(exc))
                    return {
                        "page_opened": True,
                        "playback_confirmed": False,
                        "mode": "manual",
                        "manual_required": True,
                        "message": str(exc)
                        + f" Сохранённый таймкод: {target['episode']['position']:.1f} с. "
                        "Установи его вручную.",
                    }
                await asyncio.sleep(0.5)
        position = target["episode"]["position"]
        current = await self.sample()
        if self.expected:
            await stage(
                "VERIFY",
                "success" if current.get("content_verified") else "rejected",
                f"Видео: {current['video_type']}. {current['classification_reason']}",
            )
            if not current.get("content_verified"):
                await stage(
                    "DETECT_PLAYER",
                    "rejected",
                    LABELS[current["video_type"]] + ". Автоматический запуск отменён.",
                )
                with contextlib.suppress(ToolError):
                    await self.read("pause")
                self.context = {}
                await self.bridge.evaluate(self.url, {"action": "detach"})
                return {
                    "page_opened": True,
                    "mode": "manual",
                    "manual_required": True,
                    "playback_confirmed": False,
                    "video_type": current["video_type"],
                    "content_verified": False,
                    "message": "Страница доступна, но автоматический просмотр не подтверждён. "
                    + current["classification_reason"]
                    + " Прогресс сохранён; автоматический запуск отменён.",
                }
        if position >= current["duration"] and position > 0:
            await stage(
                "RESTORE_POSITION",
                "limited",
                "Сохранённый таймкод превышает длительность видео. Прогресс сохранён.",
            )
            return {
                "page_opened": True,
                "mode": "manual",
                "playback_confirmed": False,
                "message": f"Сохранённый таймкод {position:.1f} с превышает длительность видео. "
                "Возможно, это другое издание. Прежний прогресс сохранён; выбери позицию вручную.",
            }
        if position or target.get("restart"):
            await stage("RESTORE_POSITION", "running", f"Восстанавливаем {position:.1f} с.")
            try:
                await self.control("seek", position)
            except ToolError as exc:
                await stage("RESTORE_POSITION", "limited", str(exc))
                return {
                    "page_opened": True,
                    "mode": "html5",
                    "playback_confirmed": False,
                    "message": "Safari не подтвердил восстановление. "
                    f"Сохранённый таймкод: {position:.1f} с; установи его вручную.",
                }
            await stage(
                "RESTORE_POSITION", "success", f"Плеер подтвердил позицию около {position:.1f} с."
            )
        else:
            await stage("RESTORE_POSITION", "success", "Новый просмотр с начала.")
        voice_note = ""
        if self.voice:
            voice_state = await self.read("voice")
            confirmed = voice_state.get("voice_confirmed") is True
            voice_note = (
                ""
                if confirmed
                else (
                    f" Сохранена озвучка «{self.voice}», но плеер не подтвердил её выбор. "
                    "Проверь звуковую дорожку на сайте."
                )
            )
            await stage(
                "RESTORE_POSITION",
                "success" if confirmed else "limited",
                f"Озвучка «{self.voice}» подтверждена." if confirmed else voice_note.strip(),
            )
        if not target.get("autoplay", True):
            return {
                "page_opened": True,
                "mode": "html5",
                "playback_confirmed": False,
                "message": "Позиция подготовлена. Нажми ▶ в Safari; автозапуск отключён."
                + voice_note,
            }
        await stage("PLAY", "running", "Ждём подтверждения движения времени.")
        try:
            result = await self.control("play")
        except ToolError as exc:
            await stage("PLAY", "limited", str(exc))
            with contextlib.suppress(ToolError):
                await self.read("offer_gesture")
            return {
                "page_opened": True,
                "mode": "html5",
                "playback_confirmed": False,
                "message": str(exc) + " Нажми «Чушпан: запустить видео на весь экран» "
                "на странице или кнопку ▶ самого плеера. Сохранённый прогресс не изменён.",
            }
        await stage("PLAY", "success", "Воспроизведение подтверждено движением времени.")
        if not target.get("auto_fullscreen", True):
            return {
                **result,
                "mode": "html5",
                "page_opened": True,
                "message": result.get("message", "") + voice_note,
            }
        await stage("FULLSCREEN", "running", "Запрашиваем полноэкранный режим у видеоплеера.")
        try:
            await self.control("fullscreen")
            await stage("FULLSCREEN", "success", "Плеер подтвердил полноэкранный режим.")
            return {
                **result,
                "fullscreen_confirmed": True,
                "mode": "html5",
                "page_opened": True,
                "message": result.get("message", "") + voice_note,
                "result_status": "partial_success" if voice_note else "success",
            }
        except ToolError as exc:
            await stage("FULLSCREEN", "limited", str(exc))
            with contextlib.suppress(ToolError):
                await self.read("offer_gesture")
            return {
                **result,
                "fullscreen_confirmed": False,
                "mode": "html5",
                "page_opened": True,
                "result_status": "partial_success",
                "message": "Видео воспроизводится. "
                + str(exc)
                + " Нажми «Чушпан: запустить видео на весь экран» на странице."
                + voice_note,
            }

    async def control(self, action, position=0):
        if action not in {"pause", "play", "seek", "fullscreen"}:
            raise ToolError("Этот способ управления плеером не поддерживается.")
        before = await self.read(action, position)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            await asyncio.sleep(0.15)
            after = await self.sample()
            if self.expected and action != "pause" and not after.get("content_verified"):
                raise ToolError(after["classification_reason"])
            if action == "play" and after.get("play_error"):
                raise playback_error(
                    "gesture_required",
                    "Safari заблокировал автозапуск: требуется нажатие пользователя "
                    "или разрешение автовоспроизведения для сайта.",
                )
            if action == "fullscreen":
                if after.get("fullscreen_error"):
                    raise ToolError(
                        "Safari не разрешил fullscreen без действия пользователя "
                        "или плеер его не поддерживает."
                    )
                if after.get("fullscreen") is True and after.get("fullscreen_pending") is not True:
                    return {**after, "fullscreen_confirmed": True}
            if action == "pause" and after.get("paused") is True:
                return {
                    **after,
                    "playback_confirmed": False,
                    "message": "Фильм на паузе.",
                    "result_status": "success",
                }
            if (
                action == "seek"
                and after.get("seeking") is not True
                and abs(after["position"] - min(position, after["duration"] - 0.1)) < 2
            ):
                return {
                    **after,
                    "playback_confirmed": False,
                    "message": "Позиция плеера восстановлена.",
                    "result_status": "success",
                }
            if (
                action == "play"
                and after.get("paused") is False
                and after["position"] > before["position"] + 0.05
            ):
                return {
                    **after,
                    "playback_confirmed": True,
                    "message": "Воспроизведение подтверждено.",
                    "result_status": "success",
                }
        if action == "seek" and after.get("seeking") is True:
            raise ToolError(
                "Safari ещё не завершил перемотку. Прежний прогресс сохранён. "
                "Дождись загрузки или запусти видео кнопкой самого плеера и повтори команду."
            )
        raise ToolError("Плеер не подтвердил действие. Проверь воспроизведение на сайте.")

    async def set_voice(self, voice):
        self.voice = voice
        state = await self.read("voice")
        if not state.get("voice_confirmed"):
            raise playback_error("voice_unavailable", "Плеер не подтвердил выбор озвучки.")
        return state

    async def detach(self):
        if self.url:
            try:
                await self.read("detach")
            except ToolError:
                pass
