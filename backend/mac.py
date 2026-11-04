"""Addressed desktop operations using the existing native AX bridge; no shell interface."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import stat
import struct
import time
from dataclasses import dataclass
from pathlib import Path

from backend.config import Settings
from backend.context import request_context
from backend.media.safari import AccessibilityApp, process
from backend.permissions import instruction
from backend.security import app_mentioned, command_text, explicit_request
from backend.tools.registry import DeviceUnavailable, ToolError


class DesktopBridge(AccessibilityApp):
    @property
    def bundle_id(self) -> str:
        return "com.apple.finder"  # Global desktop queries do not use a target application.


@dataclass
class WindowReference:
    snapshot: dict
    app: dict
    binding: tuple[str, str]
    expires: float


class MacDesktop:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.bridge = DesktopBridge(s, "Mac")
        self.windows: dict[str, WindowReference] = {}
        self.clock = time.monotonic
        self._lock = asyncio.Lock()

    async def apps(self, scope: str = "running") -> dict:
        return await self.bridge.request("desktop_apps", scope=scope)

    async def _running(self, name: str) -> dict:
        apps = (await self.apps())["apps"]
        name = name.casefold().removesuffix(".app").strip()
        matches = [a for a in apps if name in (a["name"].casefold(), a["bundle"].casefold())]
        if len(matches) != 1:
            raise ToolError("Приложение не запущено или имя неоднозначно. Уточни имя из списка.")
        return matches[0]

    async def app(self, name: str, action: str) -> dict:
        async with self._lock:
            app = await self._running(name)
            result = await self.bridge.request(
                "desktop_app", target_bundle=app["bundle"], pid=app["pid"], action=action
            )
            result["message"] = (
                f"Переключился на {app['name']}."
                if action == "switch"
                else f"{app['name']} закрыт."
            )
            return result

    async def list_windows(self, name: str = "") -> dict:
        apps = await self.apps("active") if not name else {"apps": [await self._running(name)]}
        if not apps["apps"]:
            raise ToolError("Активное приложение не найдено.")
        app = apps["apps"][0]
        data = await self.bridge.request(
            "desktop_windows", target_bundle=app["bundle"], pid=app["pid"]
        )
        context = request_context.get()
        if context is None:
            raise ToolError("Окнам нужен контекст текущей сессии.")
        self.windows = {k: v for k, v in self.windows.items() if v.expires > self.clock()}
        out = []
        for window in data["windows"]:
            token = secrets.token_hex(16)
            self.windows[token] = WindowReference(window, app, context.binding, self.clock() + 120)
            out.append({"window_id": token, **window})
        # Bound cache growth across repeated clients. Old references fail closed.
        while len(self.windows) > 256:
            self.windows.pop(next(iter(self.windows)))
        return {
            "app": app["name"],
            "windows": out,
            "screens": data["screens"],
            "valid_seconds": 120,
        }

    def _window(self, window_id: str) -> WindowReference:
        reference = self.windows.get(window_id)
        context = request_context.get()
        if (
            reference is None
            or reference.expires <= self.clock()
            or context is None
            or reference.binding != context.binding
        ):
            raise ToolError(
                "Окно устарело или принадлежит другой сессии. Получи список окон заново."
            )
        return reference

    def window_intent(self, args: dict, text: str) -> bool:
        reference = self._window(args.get("window_id", ""))
        plain_current = re.fullmatch(
            r"(?:сверни|разверни|восстанови|покажи|выбери) "
            r"(?:(?:активное|текущее|это) )?окно[.!?]*",
            command_text(text),
        )
        return explicit_request("manage_window", args, text) and (
            app_mentioned(reference.app["name"], text)
            or bool(
                (
                    plain_current
                    or re.search(r"(?:активн|текущ|это|это[гм]о|данн).{0,12}окн", text.casefold())
                )
                and reference.snapshot.get("focused")
                and reference.app.get("active")
            )
        )

    async def window(self, window_id: str, action: str, **geometry: int) -> dict:
        if action == "move_space":
            raise ToolError(
                "Перенос между рабочими столами надёжно не поддерживается. "
                "Открой Mission Control вручную."
            )
        async with self._lock:
            reference = self._window(window_id)
            context = request_context.get()
            if not self.window_intent({"window_id": window_id, "action": action}, context.text):
                raise ToolError("Операция или приложение не совпадает с текущей просьбой.")
            implicit = not app_mentioned(reference.app["name"], context.text)
            # Each observed window reference can mutate at most once, even on failure.
            self.windows.pop(window_id)
            result = await self.bridge.request(
                "desktop_window",
                target_bundle=reference.app["bundle"],
                pid=reference.app["pid"],
                expected=reference.snapshot,
                action=action,
                implicit=implicit,
                **geometry,
            )
            labels = {
                "close": "Окно закрыто",
                "focus": "Окно выбрано",
                "minimize": "Окно свёрнуто",
                "restore": "Окно восстановлено",
                "maximize": "Окно развёрнуто",
                "left": "Окно слева",
                "right": "Окно справа",
                "move": "Окно перемещено",
                "resize": "Размер окна изменён",
                "fullscreen": "Полноэкранный режим включён",
                "exit_fullscreen": "Полноэкранный режим выключен",
            }
            result["message"] = labels[action] + "."
            return result

    async def brightness(self, level: int | None = None) -> dict:
        async with self._lock:
            result = await self.bridge.request(
                "desktop_brightness", **({"level": level} if level is not None else {})
            )
            result["message"] = f"Яркость {result['level']} процентов."
            return result

    async def external_brightness(self, level: int) -> dict:
        from backend.night import BetterDisplay

        service = BetterDisplay(self)
        async with self._lock:
            displays = [d for d in await service.inventory() if not d.get("builtin")]
            selected = self.s.night.external_uuid
            from backend.context import device_endpoint

            if (target := device_endpoint.get()) and target[0] == "external_display":
                selected = target[1]
            if selected:
                displays = [
                    d for d in displays if d.get("uuid", "").casefold() == selected.casefold()
                ]
            if not displays:
                raise DeviceUnavailable(
                    "Выбранный внешний дисплей не подключён. Команда не отправлялась."
                )
            if len(displays) != 1:
                raise ToolError(
                    "Внешний дисплей не найден или выбор неоднозначен. Укажи night.external_uuid."
                )
            result = await service.set_level(
                displays[0]["uuid"], self.s.night.external_control, level
            )
            return {
                **result,
                "level": round(result["actual"] * 100),
                "device_state_confirmed": True,
                "message": f"Яркость внешнего дисплея {level} процентов.",
            }

    async def power(self, action: str) -> dict:
        from backend.tools.system import run

        if action == "lock":
            result = await self.bridge.request("desktop_lock")
        else:
            command = (
                ("/usr/bin/pmset", "sleepnow")
                if action == "sleep"
                else ("/usr/bin/osascript", "-e", 'tell application "System Events" to shut down')
            )
            code, _ = await run(*command)
            if code:
                raise ToolError(
                    "macOS отклонила команду питания. Для выключения проверь Automation; "
                    "принудительного завершения нет."
                )
            result = {"submitted": True}
        result["message"] = {
            "lock": "Команда блокировки отправлена. Состояние экрана после неё не проверено.",
            "sleep": "Команда сна отправлена. Переход в сон из этого процесса не проверен.",
            "shutdown": "Запрос выключения отправлен macOS. "
            "Несохранённые документы могут его остановить.",
        }[action]
        return result

    async def screenshot(self, mode: str = "screen") -> dict:
        if mode not in {"screen", "region", "window"}:
            raise ToolError("Выбери экран, активное окно или область.")
        if not (await self.bridge.request("desktop_capture_status"))["granted"]:
            from backend.permission_policy import requests_allowed

            if requests_allowed.get():
                await self.bridge.request("permission_request", permission="screen_recording")
            if not (await self.bridge.request("desktop_capture_status"))["granted"]:
                raise ToolError(instruction("screen_recording"))
        directory = self.s.data_path / "screenshots"
        if directory.is_symlink():
            raise ToolError("Каталог снимков не должен быть симлинком.")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        path = directory / (secrets.token_hex(16) + ".png")
        # Reserve an exclusive private regular file; the LLM never chooses the destination.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            selection = ["-D", "1"]
            if mode == "region":
                selection = ["-i", "-s"]
            elif mode == "window":
                window = await self.bridge.request("desktop_capture_window")
                selection = ["-l", str(window["window_id"])]
            await process("/usr/sbin/screencapture", "-x", "-t", "png", *selection, str(path),
                          **({"timeout": 25} if mode == "region" else {}))
            path.chmod(0o600)
            with path.open("rb") as stream:
                header = stream.read(24)
            if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
                raise ToolError("macOS не вернула корректный снимок экрана.")
            width, height = struct.unpack(">II", header[16:24])
            return {
                "ok": True,
                "path": str(path),
                "mime_type": "image/png",
                "width": width,
                "height": height,
                "agent_id": "local-mac",
                "message": f"Снимок сохранён локально: {path}.",
            }
        except BaseException:
            path.unlink(missing_ok=True)  # Only this newly reserved artifact, never a user file.
            raise

    def allowed_path(self, root: str, path: str = ".") -> Path:
        base = self.s.mac.file_roots.get(root)
        if base is None:
            raise ToolError("Это место не разрешено настройками Mac Agent.")
        relative = Path(path)
        if (
            relative.is_absolute()
            or "\\" in path
            or any(p == ".." or p.startswith(".") for p in relative.parts if p != ".")
        ):
            raise ToolError("Нужен относительный путь без скрытых файлов и переходов наверх.")
        base = base.expanduser().resolve(strict=True)
        if base == Path("/"):
            raise ToolError("Корень диска нельзя использовать как разрешённое место.")
        target = (base / relative).resolve(strict=True)
        if not target.is_relative_to(base) or any(
            p.startswith(".") for p in target.relative_to(base).parts
        ):
            raise ToolError("Ссылка ведёт за пределы разрешённого места или в скрытый файл.")
        if not (target.is_file() or target.is_dir()):
            raise ToolError("Разрешены только обычные файлы и папки.")
        if any(
            p.casefold().endswith((".app", ".bundle", ".framework", ".photoslibrary"))
            for p in target.relative_to(base).parts
        ):
            raise ToolError("Пакеты приложений и медиатеки не открываются файловым инструментом.")
        return target

    def path_intent(self, args: dict, text: str) -> bool:
        command = command_text(text)
        if not re.match(r"открой|покажи|open|show", command):
            return False
        path = args.get("path", ".")
        if path != ".":
            return Path(path).name.casefold() in command
        names = {
            "documents": r"документ|documents",
            "downloads": r"загруз|downloads",
            "desktop": r"рабоч.{0,4} стол|desktop",
        }
        return bool(
            re.search(names.get(args.get("root"), re.escape(args.get("root", "\x00"))), command)
        )

    async def open_path(self, root: str, path: str = ".", finder: bool = False) -> dict:
        target = self.allowed_path(root, path)
        if target.is_file():
            allowed = {
                ".txt",
                ".md",
                ".pdf",
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".webp",
                ".heic",
                ".csv",
                ".mp3",
                ".wav",
                ".mp4",
                ".mov",
                ".docx",
                ".xlsx",
                ".pptx",
                ".rtf",
            }
            if target.suffix.casefold() not in allowed or target.stat().st_mode & (
                stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            ):
                raise ToolError(
                    "Этот тип файла нельзя безопасно открыть через ассистента. "
                    "Скрипты и исполняемые файлы запрещены."
                )
        if target.is_dir():
            await process("/usr/bin/open", "-a", "Finder", str(target))
        elif finder:
            await process("/usr/bin/open", "-R", str(target))
        else:
            await process("/usr/bin/open", str(target))
        return {
            "submitted": True,
            "path": str(target),
            "message": f"Передал Finder путь {target}."
            if finder or target.is_dir()
            else f"Передал файл приложению по умолчанию: {target.name}.",
        }

    async def find_files(self, root: str, name: str) -> dict:
        base = self.allowed_path(root)

        def scan() -> dict:
            matches, visited, truncated = [], 0, False
            stack = [(base, 0)]
            while stack:
                directory, depth = stack.pop()
                try:
                    entries = sorted(directory.iterdir())
                except OSError:
                    continue
                for entry in entries:
                    visited += 1
                    if visited > 5000:
                        return {"matches": matches, "truncated": True}
                    if entry.name.startswith(".") or entry.is_symlink():
                        continue
                    try:
                        target = self.allowed_path(root, str(entry.relative_to(base)))
                    except (OSError, ToolError):
                        continue
                    if name.casefold() in entry.name.casefold():
                        matches.append(
                            {
                                "root": root,
                                "path": str(target.relative_to(base)),
                                "kind": "folder" if target.is_dir() else "file",
                            }
                        )
                        if len(matches) >= 20:
                            return {"matches": matches, "truncated": True}
                    if target.is_dir():
                        if depth < 8:
                            stack.append((target, depth + 1))
                        else:
                            truncated = True
            return {"matches": matches, "truncated": truncated}

        return await asyncio.to_thread(scan)
