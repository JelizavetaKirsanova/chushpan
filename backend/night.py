"""A fixed, confirmed night routine. No arbitrary BetterDisplay/Shortcuts commands."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import pty
import re
import sqlite3
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

from backend.config import Settings
from backend.context import request_context
from backend.mac import MacDesktop
from backend.memory.store import MemoryStore
from backend.security import Risk, command_text
from backend.shortcuts import ShortcutsService
from backend.tools.registry import Tool, ToolError
from backend.tools.system import set_volume

APP = "/Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay"
SHORTCUT = "Youtube sleep"
log = logging.getLogger(__name__)


def night_requested(text: str) -> bool:
    text = re.sub(r"^(?:эй[, .!]+)?чушпа[́]?н[, .!:]+", "", text.casefold().strip())
    return bool(
        re.fullmatch(
            r"(?:я спать|спокойной ночи|включи ночной режим|я пошла спать"
            r"|включи режим сна|запусти ночной режим)[.! ]*",
            command_text(text),
        )
    )


def night_addressed(text: str) -> bool:
    return bool(
        re.match(r"^(?:эй[, .!]+)?чушпа[́]?н[, .!:]+", text.casefold().strip())
    ) and night_requested(text)


async def display_command(*args: str) -> str:
    """The vendor binary only prints get results when stdout is a TTY."""
    master, slave = pty.openpty()
    proc = None
    try:
        env = {k: v for k, v in os.environ.items() if k in {"HOME", "PATH", "TMPDIR", "LANG"}}
        proc = await asyncio.create_subprocess_exec(
            APP, *args, stdin=asyncio.subprocess.DEVNULL, stdout=slave, stderr=slave, env=env
        )
        os.close(slave)
        slave = -1
        os.set_blocking(master, False)
        data = bytearray()
        async with asyncio.timeout(8):
            while True:
                try:
                    chunk = os.read(master, 4096)
                except BlockingIOError:
                    await asyncio.sleep(0.02)
                    continue
                except OSError as e:
                    if e.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 65536:
                    raise ToolError("BetterDisplay вернул слишком большой ответ.")
            await proc.wait()
        if proc.returncode:
            raise ToolError("BetterDisplay не подтвердил операцию.")
        return data.decode("utf-8", errors="replace").strip()
    except (OSError, TimeoutError):
        raise ToolError(
            "BetterDisplay недоступен или не ответил; автоматического повтора нет."
        ) from None
    finally:
        if slave >= 0:
            os.close(slave)
        os.close(master)
        if proc and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


class BetterDisplay:
    def __init__(self, desktop: MacDesktop):
        self.desktop = desktop

    async def inventory(self) -> list[dict]:
        # CG distinguishes the built-in panel independently of names or main-display status.
        physical = (await self.desktop.bridge.request("desktop_displays"))["displays"]
        raw = await display_command("get", "-identifiers")
        try:
            items = json.loads(raw if raw.startswith("[") else "[" + raw + "]")
            by_uuid = {
                str(UUID(i["UUID"])): i
                for i in items
                if i.get("deviceType") == "Display" and i.get("UUID")
            }
            return [
                {**d, "name": by_uuid[str(UUID(d["uuid"]))].get("name", "Дисплей")}
                for d in physical
                if str(UUID(d["uuid"])) in by_uuid
            ]
        except (ValueError, KeyError, TypeError):
            raise ToolError(
                "Не удалось однозначно сопоставить дисплеи BetterDisplay и macOS."
            ) from None

    async def read(self, uuid: str, control: str) -> float:
        if control not in {"hardwareBrightness", "softwareBrightness", "brightness"}:
            raise ToolError("Неизвестный способ яркости.")
        uid = str(UUID(uuid)).upper()
        try:
            value = float(await display_command("get", "-UUID=" + uid, "-" + control))
            if 0 <= value <= 1:
                return value
        except ValueError:
            pass
        raise ToolError("BetterDisplay не вернул проверяемую яркость.")

    async def set(self, uuid: str, control: str, level: int) -> dict:
        if control not in {
            "hardwareBrightness",
            "softwareBrightness",
            "brightness",
        } or level not in {10, 25}:
            raise ToolError("Ночной сценарий допускает только предусмотренные уровни яркости.")
        uid = str(UUID(uuid)).upper()
        previous = await self.read(uid, control)
        await display_command("set", "-UUID=" + uid, f"-{control}={level / 100:.2f}")
        actual = await self.read(uid, control)
        if abs(actual - level / 100) > 0.005:
            raise ToolError("Яркость отправлена, но заданный уровень не подтверждён.")
        return {"previous": previous, "actual": actual, "control": control, "uuid": uid}


class NightRoutine:
    def __init__(
        self, s: Settings, store: MemoryStore, desktop: MacDesktop | None = None, *, video=None
    ):
        self.s, self.store = s, store
        self.display = BetterDisplay(desktop or MacDesktop(s))
        self.shortcuts = ShortcutsService(s)
        self.video = video
        self.volume = set_volume
        from backend.night_preflight import NightPreflight

        self.preflight = NightPreflight(s, self.display, self.shortcuts)
        self.lock = asyncio.Lock()

    def summary(self, args: dict) -> str:
        context = request_context.get()
        if context is None or not {"mac.system", "mac.shortcuts"} <= context.device.capabilities:
            raise ToolError("Для ночного сценария нужны разрешения mac.system и mac.shortcuts.")
        entry = self.shortcuts.entry(SHORTCUT)
        if self.s.youtube_video.night_autoplay:
            if self.video is None or "mac.media" not in context.device.capabilities:
                raise ToolError("Автовыбор видео требует доступного YouTube-модуля и mac.media.")
            cfg = self.s.youtube_video
            video_summary = (
                "выбрать непросмотренное видео из "
                + self.video.pid()
                + ", запустить в Safari на внешнем экране "
                + str(cfg.external_uuid or self.s.night.external_uuid or "не выбран")
                + f", порядок {cfg.mode}, длительность {cfg.min_seconds}–{cfg.max_seconds} секунд, "
                + f"исключить последние {cfg.exclude_recent_hours} часов, "
                + "без повторов; затем "
            )
        else:
            video_summary = ""
        control_label = {
            "brightness": "общая шкала BetterDisplay",
            "softwareBrightness": "программное затемнение",
            "hardwareBrightness": "аппаратная яркость",
        }[self.s.night.external_control]
        return (
            video_summary
            + "встроенный экран 10% (аппаратная яркость); внешний "
            + str(self.s.night.external_uuid or "не выбран")
            + " 25% ("
            + control_label
            + "); системную громкость Mac 30%; последней запустить существующую "
            + SHORTCUT
            + " ("
            + entry.identifier
            + "): "
            + entry.description
            + ". Он может долго ждать видео в активной вкладке Safari, затем усыпить Mac. "
            "Пропущенные/ошибочные шаги будут названы; повторный запуск Shortcut блокируется."
        )

    async def run(self) -> dict:
        log.info("COMMAND_START night_execution")
        token = uuid4().hex
        claimed = False
        try:
            claimed = self.store.claim_operation("night", token, time.time())
            if not claimed:
                return {
                    "ok": False,
                    "shortcut_started": False,
                    "message": "Ночной сценарий уже выполняется. Повтор не запускал.",
                }
            report = await self.preflight.check()
            if not report["ok"]:
                return {**report, "shortcut_started": False, "steps": {}}
            return await self._run()
        except sqlite3.Error:
            raise ToolError(
                "Журнал SQLite недоступен; часть действий могла выполниться. "
                "Проверь состояние, автоматического повтора нет."
            ) from None
        finally:
            log.info("COMMAND_END night_execution")
            if claimed:
                with contextlib.suppress(sqlite3.Error):
                    self.store.release_operation("night", token)

    async def _run(self) -> dict:
        async with self.lock:
            if not self.store.memory_available:
                raise ToolError(
                    "SQLite недоступна; нельзя безопасно вести журнал ночного сценария."
                )
            now = time.time()
            state = self.store.preference("night", "last_run") or {}
            # A repeated utterance/restart cannot duplicate the long-running sleep watcher.
            status = state.get("steps", {}).get("shortcut", {})
            active = status.get("status") in {"started", "unknown"}
            if pid := status.get("pid"):
                try:
                    os.kill(pid, 0)
                    active = True
                except ProcessLookupError:
                    pass
                except PermissionError:
                    active = True
            fresh = not state or (now - state.get("started", 0) > 12 * 3600 and not active)
            if fresh:
                state = {"started": now, "date": datetime.now(UTC).isoformat(), "steps": {}}
            steps = state["steps"]
            if steps.get("shortcut", {}).get("status") in {"started", "done", "unknown"} and (
                "volume" not in steps
            ):
                return {
                    "ok": False,
                    "shortcut_started": False,
                    "steps": steps,
                    "message": "Сохранён старый запуск Youtube sleep. Настройки не менял; "
                    "сначала останови команду и сбрось журнал ночного сценария.",
                }
            outcomes = []
            try:
                displays = await self.display.inventory()
            except ToolError:
                displays = []
                outcomes.append("BetterDisplay недоступен: яркость не изменил")
            builtins = [d for d in displays if d["builtin"]]
            externals = [
                d
                for d in displays
                if not d["builtin"]
                and self.s.night.external_uuid
                and str(UUID(d["uuid"])) == str(UUID(self.s.night.external_uuid))
            ]
            if self.s.youtube_video.night_autoplay:
                video_screen = self.s.youtube_video.external_uuid or self.s.night.external_uuid
                if (
                    self.s.youtube_video.preferred_screen != "external"
                    or video_screen != self.s.night.external_uuid
                    or len(externals) != 1
                ):
                    return {
                        "ok": False,
                        "shortcut_started": False,
                        "steps": steps,
                        "message": "Выбранный внешний экран не подтверждён. "
                        "Видео, яркость, громкость и Youtube sleep не запускал.",
                    }
                video_step = steps.get("video", {})
                watcher_exists = steps.get("shortcut", {}).get("status") in {
                    "started",
                    "done",
                    "unknown",
                }
                if video_step.get("status") != "done":
                    if video_step or watcher_exists or self.video is None:
                        return {
                            "ok": False,
                            "shortcut_started": False,
                            "steps": steps,
                            "message": (
                                "Ночной сценарий частично: запуск видео не повторял; "
                                "проверь Safari и остановку Youtube sleep перед сбросом журнала."
                            ),
                        }
                    steps["video"] = {"status": "started"}
                    self.store.set_preference("night", "last_run", state)
                    try:
                        result = await self.video.play(
                            source="night", unwatched=True, allow_repeat=False
                        )
                        if not result.get("playing"):
                            raise ToolError(result["message"])
                        steps["video"] = {"status": "done", "video_id": result["video_id"]}
                        outcomes.append(result["message"])
                    except ToolError:
                        steps["video"] = {"status": "unknown"}
                        self.store.set_preference("night", "last_run", state)
                        return {
                            "ok": False,
                            "shortcut_started": False,
                            "steps": steps,
                            "message": (
                                "Ночной сценарий частично: видео не подтверждено. "
                                "Проверь Safari, API и плейлист. Youtube sleep не запускал; "
                                "повторный запуск видео "
                                "заблокирован журналом."
                            ),
                        }
                    self.store.set_preference("night", "last_run", state)
            for key, matches, level, control in [
                ("builtin", builtins, 10, "hardwareBrightness"),
                ("external", externals, 25, self.s.night.external_control),
            ]:
                if steps.get(key, {}).get("status") in {"done", "started", "unknown"}:
                    outcomes.append(
                        ("Встроенный" if key == "builtin" else "Внешний")
                        + " экран: повтор пропущен"
                    )
                    continue
                if len(matches) != 1:
                    outcomes.append(
                        ("Встроенный" if key == "builtin" else "Выбранный внешний")
                        + " экран не найден однозначно"
                    )
                    continue
                # Persist intent before the side effect, including across crashes.
                try:
                    previous = await self.display.read(matches[0]["uuid"], control)
                    components = {}
                    if control == "brightness":
                        for feature in ["hardwareBrightness", "softwareBrightness"]:
                            try:
                                components[feature] = await self.display.read(
                                    matches[0]["uuid"], feature
                                )
                            except ToolError:
                                components[feature] = None
                    steps[key] = {
                        "status": "started",
                        "previous": previous,
                        "previous_components": components,
                        "uuid": matches[0]["uuid"],
                        "control": control,
                    }
                    self.store.set_preference("night", "last_run", state)
                    saved = await self.display.set(matches[0]["uuid"], control, level)
                    steps[key] = {"status": "done", "previous_components": components, **saved}
                    outcomes.append(
                        ("Встроенный" if key == "builtin" else "Внешний") + f" экран {level}%"
                    )
                except ToolError:
                    steps[key] = {**steps.get(key, {}), "status": "unknown"}
                    outcomes.append(
                        ("Встроенный" if key == "builtin" else "Внешний")
                        + " экран: уровень не подтверждён"
                    )
                self.store.set_preference("night", "last_run", state)
            if steps.get("volume", {}).get("status") in {"done", "started", "unknown"}:
                outcomes.append("Громкость Mac: повтор пропущен")
            else:
                steps["volume"] = {"status": "started"}
                self.store.set_preference("night", "last_run", state)
                try:
                    saved = await self.volume(level=30)
                    if not saved.get("ok") or saved.get("volume_percent") != 30:
                        raise ToolError("Громкость Mac не подтверждена.")
                    steps["volume"] = {"status": "done", "actual": 30}
                    outcomes.append("Громкость Mac 30%")
                except ToolError:
                    steps["volume"] = {"status": "unknown"}
                    outcomes.append("Громкость Mac: уровень 30% не подтверждён")
                self.store.set_preference("night", "last_run", state)
            launched = False
            if steps.get("shortcut", {}).get("status") in {"started", "done", "unknown"}:
                outcomes.append("Youtube sleep повторно не запускал")
            elif not all(
                steps.get(k, {}).get("status") == "done" for k in ("builtin", "external", "volume")
            ):
                outcomes.append("Youtube sleep не запускал: обязательные шаги не подтверждены")
            else:
                try:
                    # Missing/unallowlisted commands can be fixed before any launch intent.
                    await self.shortcuts.verify_sleep()
                    if self.s.youtube_video.night_autoplay:
                        await self.video.verify_for_sleep()
                    steps["shortcut"] = {"status": "started"}
                    self.store.set_preference("night", "last_run", state)
                    result = await self.shortcuts.start_sleep()
                    steps["shortcut"] = {"status": "done", "pid": result.get("pid")}
                    launched = True
                    outcomes.append("Youtube sleep запущен; ждёт окончания видео в Safari")
                except ToolError:
                    if steps.get("shortcut", {}).get("status") == "started":
                        steps["shortcut"] = {"status": "unknown"}
                    outcomes.append("Youtube sleep: запуск не подтверждён; проверь Команды")
                self.store.set_preference("night", "last_run", state)
            complete = all(
                steps.get(k, {}).get("status") == "done"
                for k in ["builtin", "external", "volume", "shortcut"]
            )
            return {
                "ok": complete,
                "shortcut_started": launched,
                "steps": steps,
                "message": ("Ночной сценарий: " if complete else "Ночной сценарий частично: ")
                + "; ".join(outcomes)
                + ".",
            }

    def permissions_tool(self) -> Tool:
        tool = Tool(
            "check_night_permissions",
            "Проверить разрешения и компоненты ночного сценария без изменения настроек.",
            self.preflight.check,
            reply=lambda r: r["message"],
            slow=True,
        )
        tool.required_capability = "mac.system"
        return tool

    def tool(self) -> Tool:
        tool = Tool(
            "night_mode",
            "Ночной сценарий: видео для сна в Safari, встроенный 10%, внешний 25%, "
            "системная громкость Mac 30%, затем Youtube sleep. Явная просьба и свежий код.",
            self.run,
            risk=Risk.DANGEROUS,
            intent=lambda a, t: night_requested(t),
            summary=self.summary,
            reply=lambda r: r["message"],
            slow=True,
        )
        tool.required_capability = "mac.system"
        return tool


def run_night_cli(s: Settings, action: str = "status", confirm_stopped: bool = False) -> int:
    """Local journal inspection/reset only. Never launches the routine."""
    try:
        store = MemoryStore(s.db_path)
    except sqlite3.Error:
        print("SQLite недоступна; журнал не изменён.")
        return 1
    try:
        state = store.preference("night", "last_run")
        if action == "reset":
            if not confirm_stopped:
                print(
                    "Сначала останови Youtube sleep в Командах и проверь состояние Mac. "
                    "Затем night reset --confirm-stopped; это только сброс журнала."
                )
                return 1
            pid = (state or {}).get("steps", {}).get("shortcut", {}).get("pid")
            if pid:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    print("Процесс ещё существует или его состояние недоступно; сброс запрещён.")
                    return 1
                else:
                    print("Процесс Youtube sleep ещё существует; сначала останови его в Командах.")
                    return 1
            store.set_preference("night", "last_run", None)
            print("Журнал сброшен. Яркость и Shortcuts не запускались.")
        else:
            print(
                json.dumps(state, ensure_ascii=False, indent=2)
                if state
                else "Ночной сценарий ещё не запускался."
            )
        return 0
    except sqlite3.Error:
        print("SQLite недоступна; состояние действий проверь вручную.")
        return 1
    finally:
        store.close()
