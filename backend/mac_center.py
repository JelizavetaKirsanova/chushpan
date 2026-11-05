"""On-demand native desktop UI over the existing guarded tool registry."""

import asyncio
import base64
import io
import json
from pathlib import Path

from PIL import Image

from backend.brain.privacy import redact
from backend.center import Center, rows
from backend.context import DeviceContext, RequestContext, request_scope
from backend.memory.store import _now
from backend.vision import load_image

WINDOW_COMMANDS = {
    "focus": "выбери",
    "minimize": "сверни",
    "restore": "восстанови",
    "maximize": "разверни",
    "left": "размести слева",
    "right": "размести справа",
    "close": "закрой",
}


class MacCenter(Center):
    channel = "mac_center"
    timeout = 35

    def __init__(self, settings, store, registry, bus):
        self.s, self.store, self.tools = settings, store, registry
        self.mac = registry.desktop
        self.device = DeviceContext.local_mac("desktop-mac-center")
        self.preview, self.captured_at, self.analysis = None, None, None
        self.apps, self.windows, self.permissions = [], [], {}
        store.db.execute(
            "CREATE TABLE IF NOT EXISTS mac_action_log (id INTEGER PRIMARY "
            "KEY, at TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT "
            "NULL)"
        )
        self.bind(bus)

    def request(self, value):
        if isinstance(value, dict) and value.get("op") == "cancel":
            self.preview, self.captured_at, self.analysis = None, None, None
            self.publish(data=self.snapshot())
        super().request(value)

    def snapshot(self):
        pending = self.tools.confirmations.pending.get(
            (self.device.device_id, self.device.session_id)
        )
        confirmation = None
        if pending and pending.expires > self.tools.confirmations.clock():
            confirmation = {"summary": pending.summary, "code": pending.code}
        return {
            "apps": self.apps,
            "windows": self.windows,
            "permissions": self.permissions,
            "preview": self.preview,
            "captured_at": self.captured_at,
            "analysis": self.analysis,
            "file_roots": list(self.s.mac.file_roots),
            "confirmation": confirmation,
            "actions": [
                {
                    "id": tool.name,
                    "description": tool.description,
                    "arguments": tool.parameters,
                    "risk": str(tool.risk or "by_arguments"),
                    "capability": tool.required_capability,
                    "enabled": self.tools.enabled(tool.name),
                }
                for tool in self.tools._tools.values()
                if tool.required_capability and tool.required_capability.startswith("mac.")
            ],
            "history": rows(
                self.store.db, "SELECT * FROM mac_action_log ORDER BY id DESC LIMIT 100"
            ),
        }

    async def invoke(self, name, args, command):
        with request_scope(RequestContext(self.device, command)):
            result = json.loads(await self.tools.call(name, args))
        with self.store.memory_transaction():
            self.store.db.execute(
                "INSERT INTO mac_action_log(at,action,status) VALUES(?,?,?)",
                (_now(), name, result.get("result_status", "unverified")),
            )
            self.store.db.execute(
                "DELETE FROM mac_action_log WHERE id NOT IN (SELECT id FROM "
                "mac_action_log ORDER BY id DESC LIMIT 1000)"
            )
        if result.get("error"):
            raise ValueError(result["error"])
        return result

    def guard_capture(self):
        excluded = (self.store.preference("settings", "access") or {}).get("blocked_apps", [])
        forbidden = {s.strip().casefold().removesuffix(".app") for s in excluded}
        forbidden.update({"1password", "passwords", "пароли", "keychain access", "связка ключей"})
        if any(
            a.get("name", "").casefold() in forbidden or a.get("bundle", "").casefold() in forbidden
            for a in self.apps
        ):
            raise ValueError(
                "Закрой исключённые приложения и менеджеры паролей перед снимком экрана."
            )

    async def capture(self, value):
        self.apps = (await self.invoke("get_apps", {}, "покажи запущенные приложения"))["apps"]
        self.guard_capture()
        self.publish(capturing=True)
        artifact = None
        try:
            result = await self.invoke(
                "take_screenshot", {"mode": value.get("mode", "screen")}, "сделай скриншот"
            )
            artifact = Path(result["path"])
            image = await asyncio.to_thread(load_image, artifact)

            def thumbnail():
                with Image.open(io.BytesIO(image.data)) as picture:
                    picture.thumbnail((1100, 750))
                    output = io.BytesIO()
                    picture.convert("RGB").save(output, "JPEG", quality=75)
                    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode()

            self.preview = await asyncio.to_thread(thumbnail)
            self.captured_at, self.analysis = _now(), None
            if value.get("analyze") is True:
                data = await self.mac.bridge.request("desktop_ocr", path=str(artifact))
                self.analysis = {
                    "mode": "local_ocr",
                    "text": redact("\n".join(r["text"] for r in data.get("lines", [])))[:12000],
                    "message": "Локально распознан только текст. Визуальное содержимое моделью не"
                    " анализировалось.",
                }
        finally:
            if artifact:
                artifact.unlink(missing_ok=True)
            self.publish(capturing=False)

    async def handle(self, value):
        op, message = value.get("op", "snapshot"), None
        if op == "refresh":
            self.apps = (await self.invoke("get_apps", {}, "покажи запущенные приложения"))["apps"]
            status = await self.mac.bridge.request("permission_status")
            capture = await self.mac.bridge.request("desktop_capture_status")
            self.permissions = {
                "accessibility": status.get("accessibility", "check_needed"),
                "screen_recording": "granted" if capture.get("granted") else "denied",
            }
        elif op == "windows":
            name = value.get("name", "")
            result = await self.invoke("get_windows", {"name": name}, "покажи окна " + name)
            self.windows = [{**w, "app": result["app"]} for w in result.get("windows", [])]
        elif op == "app":
            name = value.get("name", "")
            if value.get("action") == "open":
                result = await self.invoke("open_app", {"name": name}, "открой " + name)
            else:
                result = await self.invoke(
                    "manage_app", {"name": name, "action": "switch"}, "переключись на " + name
                )
            message = result.get("message") or "Запрос передан приложению; статус смотри в истории."
        elif op == "open_file":
            root, path = value.get("root", ""), value.get("path", ".")
            result = await self.invoke(
                "open_path",
                {"root": root, "path": path, "finder": value.get("finder") is True},
                "открой " + str(root) + " " + str(path),
            )
            message = result.get("message", "Открытие файла не подтверждено.")
        elif op == "window":
            action = value.get("action")
            if action not in WINDOW_COMMANDS:
                raise ValueError("Неизвестное действие окна.")
            window = next(
                (w for w in self.windows if w["window_id"] == value.get("window_id")), None
            )
            if not window:
                raise ValueError("Обнови список окон: идентификатор устарел.")
            command = WINDOW_COMMANDS[action] + " окно " + window["app"]
            result = await self.invoke(
                "manage_window", {"window_id": window["window_id"], "action": action}, command
            )
            if result.get("status") != "confirmation_required":
                self.windows = []
            message = result.get("message", "Результат не подтверждён.")
        elif op == "confirm":
            context = RequestContext(self.device, "Подтверждаю " + str(value.get("code", "")))
            message = await self.tools.confirmations.respond(
                context, self.tools.call, self.tools.direct_reply
            )
            self.windows = []
        elif op == "capture":
            await self.capture(value)
            message = "Снимок показан локально. Файл удалён после обработки."
        elif op in {"clear_capture", "cancel"}:
            self.preview, self.captured_at, self.analysis = None, None, None
        elif op != "snapshot":
            raise ValueError("Неизвестное действие Mac.")
        return {"data": self.snapshot(), "message": message}

    async def close(self):
        await super().close()
        self.preview, self.analysis = None, None
