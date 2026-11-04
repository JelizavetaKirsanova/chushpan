"""Extensible, permission-aware public player interface over the existing Safari executor."""

import asyncio
import json
import time
from pathlib import Path
from typing import Protocol

from backend.cinema.models import local_video, web_url
from backend.media.music_safari import SafariMusicTab
from backend.media.safari import process
from backend.permission_policy import requests_allowed
from backend.tools.registry import ToolError

PLAYER = Path(__file__).with_name("player.js").read_text()
PLAYER_ERRORS = {
    "page_changed": "Страница изменилась. Выбери нужную серию в Чушпане заново.",
    "multiple_players": "На странице несколько плееров; автоматическое управление неоднозначно.",
    "no_player": "Доступный HTML5-плеер не найден. Возможно, видео находится в чужом iframe.",
    "protected_player": "Защищённый плеер: автоматическое управление не поддерживается.",
    "media_error": "Плеер сообщил об ошибке загрузки видео.",
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
            raise ToolError(messages.get(getattr(exc, "code", ""), str(exc))) from None
        except (ValueError, TypeError):
            raise ToolError("Safari не вернул состояние плеера.") from None
        finally:
            requests_allowed.reset(token)
        if not isinstance(result, dict):
            raise ToolError("Неверный ответ плеера.")
        if result.get("error"):
            raise ToolError(PLAYER_ERRORS.get(result["error"], "Плеер недоступен."))
        return result


class HTML5Adapter:
    def __init__(self, bridge, opener=open_page):
        self.bridge, self.opener = bridge, opener
        self.url = ""
        self.context = {}

    async def read(self, action="status", position=0):
        return await self.bridge.evaluate(
            self.url, {**self.context, "action": action, "position": position}
        )

    async def sample(self):
        return await self.read()

    async def open(self, target):
        self.url = target["link"]["url"]
        diagnostic = target.get("diagnostic")

        async def stage(name, status, message):
            if diagnostic:
                await diagnostic(name, status, message)

        self.context = target.get("tracking", {})
        # Reuse a matching open page if available, otherwise open the explicit URL once.
        try:
            await self.sample()
        except ToolError:
            await stage("OPEN_SAFARI", "running", "Открываем конкретную страницу в Safari.")
            await self.opener(self.url)
        await stage("DETECT_PLAYER", "running", "Ожидаем доступный HTML5-плеер и метаданные.")
        deadline = time.monotonic() + 15
        while True:
            try:
                await self.sample()
                await stage("DETECT_PLAYER", "success", "Найден один доступный HTML5-плеер.")
                break
            except ToolError as exc:
                if time.monotonic() >= deadline:
                    await stage("DETECT_PLAYER", "limited", str(exc))
                    return {
                        "page_opened": True,
                        "playback_confirmed": False,
                        "mode": "manual",
                        "message": str(exc)
                        + f" Сохранённый таймкод: {target['episode']['position']:.1f} с. "
                        "Установи его вручную.",
                    }
                await asyncio.sleep(0.5)
        position = target["episode"]["position"]
        current = await self.sample()
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
        if not target.get("autoplay", True):
            return {
                "page_opened": True,
                "mode": "html5",
                "playback_confirmed": False,
                "message": "Позиция подготовлена. Нажми ▶ в Safari; автозапуск отключён.",
            }
        await stage("PLAY", "running", "Ждём подтверждения движения времени.")
        try:
            result = await self.control("play")
        except ToolError as exc:
            await stage("PLAY", "limited", str(exc))
            return {
                "page_opened": True,
                "mode": "html5",
                "playback_confirmed": False,
                "message": "Safari не подтвердил запуск. Нажми ▶ в плеере; проверь "
                "разрешение автовоспроизведения для сайта.",
            }
        await stage("PLAY", "success", "Воспроизведение подтверждено движением времени.")
        return {**result, "mode": "html5", "page_opened": True}

    async def control(self, action, position=0):
        if action not in {"pause", "play", "seek"}:
            raise ToolError("Этот способ управления плеером не поддерживается.")
        before = await self.read(action, position)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            await asyncio.sleep(0.15)
            after = await self.sample()
            if action == "pause" and after.get("paused") is True:
                return {
                    **after,
                    "playback_confirmed": False,
                    "message": "Фильм на паузе.",
                    "result_status": "success",
                }
            if (
                action == "seek"
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
        raise ToolError("Плеер не подтвердил действие. Проверь воспроизведение на сайте.")

    async def detach(self):
        if self.url:
            try:
                await self.read("detach")
            except ToolError:
                pass
