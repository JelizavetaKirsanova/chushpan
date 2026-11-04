"""Cocoa main process: one reusable WebKit HUD, menu bar and owned worker."""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from backend.desktop import BUNDLE_ID, VERSION
from backend.desktop import platform as macos
from backend.desktop.storage import load_preferences, migrate, save_preferences
from backend.hud.window import BACKGROUND, _Api
from backend.instance import InstanceLock
from backend.paths import USER_ROOT

log = logging.getLogger(__name__)
STATE_LABELS = {
    "starting": "Запуск",
    "loading": "Загрузка компонентов",
    "ready": "Готов к работе",
    "listening": "Слушает",
    "processing": "Обрабатывает команду",
    "answering": "Отвечает",
    "paused": "Приостановлен",
    "error": "Ошибка",
}


def worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--desktop-worker"]
    return [sys.executable, "-m", "backend.desktop", "--desktop-worker"]


class Bridge(_Api):
    def __init__(self, shell):
        super().__init__()
        self._shell = shell

    def desktop_status(self):
        return self._shell.desktop_status()

    def desktop_preferences(self, value):
        return self._shell.desktop_preferences(value)

    def desktop_action(self, action, value=None):
        return self._shell.desktop_action(action, value)


class Application(_Api):
    def __init__(self, *, smoke=False):
        super().__init__()
        self.smoke = smoke
        self.root = USER_ROOT
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(exist_ok=True, mode=0o700)
        self.logs = self.root / "data/logs"
        self.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.prefs = load_preferences(self.root)
        self.lock = InstanceLock(self.runtime / "desktop.lock")
        socket_key = hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
        self.socket_directory = Path(f"/private/tmp/chushpan-{os.getuid()}-{socket_key}")
        self.socket_directory.mkdir(exist_ok=True, mode=0o700)
        if self.socket_directory.is_symlink() or self.socket_directory.stat().st_uid != os.getuid():
            raise RuntimeError("Небезопасная папка локального управления.")
        self.socket_directory.chmod(0o700)
        self.socket_path = self.socket_directory / "open.sock"
        self.endpoint = self.runtime / "endpoint.json"
        self.token = secrets.token_urlsafe(32)
        self.proc = None
        self._log_file = None
        self.url = None
        self.quitting = False
        self.hidden = False
        self.worker_state = {"state": "starting", "components": {}}
        self.restarts = 0
        self.loaded = False
        self.native_lock = threading.RLock()
        self.worker_lock = threading.RLock()
        self.listener = None
        self.delegate = self.status_item = self.menu_state = self.listen_item = None

    def control(self, value):
        if not self.url:
            raise RuntimeError("Компоненты ещё запускаются.")
        request = urllib.request.Request(
            self.url + "/_desktop",
            data=json.dumps(value).encode(),
            headers={"Content-Type": "application/json", "X-Chushpan-Token": self.token},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    def start_worker(self):
        with self.worker_lock:
            if self.quitting or self.proc and self.proc.poll() is None:
                return
            self.url = None
            self.loaded = False
            self.endpoint.unlink(missing_ok=True)
            from backend.autostart import rotate_log

            path = self.logs / "application.log"
            rotate_log(path)
            self._log_file = path.open("a")
            path.chmod(0o600)
            env = {
                **os.environ,
                "CHUSHPAN_DESKTOP": "1",
                "CHUSHPAN_CONTROL_TOKEN": self.token,
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                "PYTHONUNBUFFERED": "1",
            }
            env.pop("PYTHONHOME", None)
            env.pop("PYTHONPATH", None)
            if not getattr(sys, "frozen", False):
                from backend.paths import RESOURCE_ROOT

                env["PYTHONPATH"] = str(RESOURCE_ROOT)
            args = [
                *worker_command(),
                "--endpoint",
                str(self.endpoint),
                "--parent",
                str(os.getpid()),
            ]
            if self.smoke:
                args.append("--smoke")
            self.proc = subprocess.Popen(
                args,
                env=env,
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                stdout=self._log_file,
                stderr=self._log_file,
                start_new_session=True,
            )

    def stop_worker(self):
        with self.worker_lock:
            proc = self.proc
            if proc and proc.poll() is None:
                # Only this child's session; never pkill Python, Ollama or other apps.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=3)
            if self._log_file:
                self._log_file.close()
                self._log_file = None
            self.proc = None
            self.url = None

    def singleton(self):
        if not self.lock.acquire():
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(3)
                client.connect(str(self.socket_path))
                client.sendall(b"open")
            return False
        self.socket_path.unlink(missing_ok=True)
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(self.socket_path))
        self.socket_path.chmod(0o600)
        self.listener.listen(4)
        self.listener.settimeout(1)

        def serve():
            while not self.quitting:
                try:
                    connection, _ = self.listener.accept()
                    with connection:
                        connection.settimeout(1)
                        if connection.recv(16) == b"open":
                            self.show()
                except TimeoutError:
                    continue
                except OSError:
                    return

        threading.Thread(target=serve, daemon=True).start()
        return True

    def show(self, view="home"):
        if self.window:
            self.hidden = False
            self.window.show()
            self.window.restore()
            if self.loaded:
                self.window.evaluate_js("window.chushpanNavigate?.(" + json.dumps(view) + ")")

    def close_window(self):
        if self.quitting:
            return True
        if self.prefs.close_to_menu:
            self.hidden = True
            self.window.hide()
            if not self.prefs.background_voice:
                threading.Thread(target=lambda: self.desktop_action("mute"), daemon=True).start()
        else:
            threading.Thread(target=self.quit, daemon=True).start()
        return False

    def desktop_status(self):
        with self.native_lock:
            from backend import autostart

            return {
                "legacy_autostart": autostart.enabled(),
                "available": True,
                "version": VERSION,
                "bundle_id": BUNDLE_ID,
                "data_path": str(self.root),
                "preferences": self.prefs.model_dump(),
                "login": macos.login_status(),
                "permissions": macos.permissions(),
                "worker": self.worker_state,
                "smoke": self.smoke,
            }

    def desktop_preferences(self, value):
        with self.native_lock:
            self.prefs = save_preferences(self.root, value)
        return {
            "ok": True,
            "message": "Сохранено. Параметры запуска применятся при следующем запуске.",
            "status": self.desktop_status(),
        }

    def desktop_action(self, action, value=None):
        try:
            if self.smoke and action in {"listen", "login", "migrate", "disable_legacy"}:
                raise ValueError("Это действие отключено в проверочном режиме.")
            if action == "listen":
                from backend import autostart

                if autostart.status().pid:
                    raise ValueError(
                        "Сначала отключи прежний автозапуск в разделе Приложение → Запуск."
                    )
                if not macos.request_microphone():
                    macos.open_privacy("microphone")
                    raise ValueError(
                        "Разреши микрофон: Системные настройки → "
                        "Конфиденциальность и безопасность → Микрофон."
                    )
                self.control({"op": "listen", "enabled": True})
            elif action == "mute":
                self.control({"op": "listen", "enabled": False})
            elif action == "restart":
                self.stop_worker()
                self.restarts = 0
                self.start_worker()
            elif action == "login":
                if type(value) is not bool:
                    raise ValueError("Нужен переключатель автозапуска.")
                from backend import autostart

                if value and autostart.enabled():
                    raise ValueError(
                        "Обнаружен прежний LaunchAgent. Отключи его командой "
                        "autostart uninstall из проекта перед новым автозапуском."
                    )
                macos.set_login(value)
            elif action == "disable_legacy":
                from backend import autostart

                autostart.stop()
                path = autostart.plist_path()
                parked = self.root / "legacy-autostart.disabled.plist"
                if path.exists():
                    if parked.exists():
                        raise ValueError(
                            "Копия прежнего автозапуска уже существует; "
                            "проверь её перед отключением."
                        )
                    path.rename(parked)
            elif action == "logs":
                macos.open_path(self.logs)
            elif action == "data":
                macos.open_path(self.root)
            elif action == "privacy":
                result = macos.open_privacy(value)
                return {"ok": result["opened"], **result}
            elif action == "updates":
                from backend.desktop.updates import check

                return check()
            elif action == "changelog":
                self.open_url("https://github.com/JelizavetaKirsanova/chushpan/releases")
            elif action == "migrate":
                if not isinstance(value, str) or not value:
                    raise ValueError("Укажи путь к старому проекту.")
                self.stop_worker()
                try:
                    files = migrate(Path(value), self.root)
                finally:
                    self.start_worker()
                return {
                    "ok": True,
                    "message": f"Перенесено файлов: {len(files)}. Исходники сохранены.",
                }
            elif action == "quit":
                threading.Thread(target=self.quit, daemon=True).start()
            else:
                raise ValueError("Неизвестное действие приложения.")
            return {"ok": True, "message": "Готово."}
        except Exception as exc:
            log.warning("Desktop action %s failed: %s", action, type(exc).__name__)
            return {"ok": False, "message": str(exc)}

    def _menu_action(self, action):
        result = self.desktop_action(action)
        if not result.get("ok"):
            self.show("application")
            if self.loaded:
                self.window.evaluate_js(
                    "window.chushpanNativeNotice?.(" + json.dumps(result["message"]) + ")"
                )

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        self.stop_worker()
        if self.listener:
            self.listener.close()
        self.socket_path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            self.socket_directory.rmdir()
        self.endpoint.unlink(missing_ok=True)
        if self.window:
            self.window.destroy()
        self.lock.release()

    def monitor(self):
        self.start_worker()
        while not self.quitting:
            try:
                if self.proc and self.proc.poll() is not None:
                    code = self.proc.returncode
                    self.stop_worker()
                    self.worker_state = {
                        "state": "error",
                        "error": f"Движок завершился ({code}). Журнал: {self.logs}",
                        "components": {},
                    }
                    if self.prefs.recover_engine and self.restarts < 3:
                        self.restarts += 1
                        time.sleep(min(2**self.restarts, 8))
                        self.start_worker()
                elif self.endpoint.exists():
                    if self.url is None:
                        self.url = json.loads(self.endpoint.read_text())["url"]
                        self.window.load_url(self.url)
                        self.loaded = True
                    self.worker_state = self.control({"op": "status"})
                    if (
                        self.prefs.auto_listen
                        and self.worker_state.get("state") == "paused"
                        and not getattr(self, "_auto_attempted", False)
                    ):
                        self._auto_attempted = True
                        # Automatic startup never displays a permission prompt.
                        if macos.permissions()["microphone"] == "granted" and not self.smoke:
                            from backend import autostart

                            if not autostart.status().pid:
                                self.control({"op": "listen", "enabled": True})
                if self.menu_state:
                    from PyObjCTools.AppHelper import callAfter

                    callAfter(self.refresh_menu)
            except Exception:
                log.exception("Desktop status refresh failed")
            time.sleep(2)

    def refresh_menu(self):
        state = self.worker_state
        self.menu_state.setTitle_("Чушпан · " + STATE_LABELS.get(state.get("state"), "Запуск"))
        self.listen_item.setTitle_(
            "Выключить прослушивание" if state.get("requested") else "Включить прослушивание"
        )

    def cocoa(self):
        import AppKit

        # Called on the Cocoa main thread after pywebview creates its NSApplication.
        from webview.platforms.cocoa import BrowserView

        shell = self

        class Delegate(BrowserView.AppDelegate):
            def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
                threading.Thread(target=shell.show, daemon=True).start()
                return True

            def applicationShouldTerminate_(self, app):
                threading.Thread(target=shell.quit, daemon=True).start()
                return AppKit.NSTerminateCancel

            def open_(self, sender):
                threading.Thread(target=shell.show, daemon=True).start()

            def listen_(self, sender):
                action = "mute" if shell.worker_state.get("requested") else "listen"
                threading.Thread(target=lambda: shell._menu_action(action), daemon=True).start()

            def mute_(self, sender):
                threading.Thread(target=lambda: shell._menu_action("mute"), daemon=True).start()

            def scenarios_(self, sender):
                threading.Thread(target=lambda: shell.show("automations"), daemon=True).start()

            def history_(self, sender):
                threading.Thread(target=lambda: shell.show("history"), daemon=True).start()

            def settings_(self, sender):
                threading.Thread(target=lambda: shell.show("application"), daemon=True).start()

            def restart_(self, sender):
                threading.Thread(target=lambda: shell._menu_action("restart"), daemon=True).start()

            def quit_(self, sender):
                threading.Thread(target=shell.quit, daemon=True).start()

        self.delegate = Delegate.alloc().init()
        AppKit.NSApplication.sharedApplication().setDelegate_(self.delegate)
        self.status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength
        )
        from backend.app_bundle import ICON_SOURCE

        symbol = AppKit.NSImage.alloc().initWithContentsOfFile_(str(ICON_SOURCE))
        symbol.setSize_((18, 18))
        symbol.setTemplate_(False)
        self.status_item.button().setAccessibilityLabel_("Чушпан")
        self.status_item.button().setImage_(symbol)
        menu = AppKit.NSMenu.alloc().init()
        self.menu_state = menu.addItemWithTitle_action_keyEquivalent_("Чушпан · Запуск", None, "")
        for title, selector, key in [
            ("Открыть Чушпана", "open:", ""),
            ("Включить прослушивание", "listen:", ""),
            ("Отключить микрофон", "mute:", ""),
            ("Запустить сценарий…", "scenarios:", ""),
            ("История диалогов", "history:", ""),
            ("Настройки…", "settings:", ","),
            ("Перезапустить голосовой движок", "restart:", ""),
            ("Завершить Чушпана", "quit:", "q"),
        ]:
            item = menu.addItemWithTitle_action_keyEquivalent_(title, selector, key)
            item.setTarget_(self.delegate)
            if selector == "listen:":
                self.listen_item = item
        self.status_item.setMenu_(menu)
        self.refresh_menu()

    def run(self):
        import webview
        from PyObjCTools.AppHelper import callAfter

        if not self.singleton():
            return 0
        atexit.register(self.stop_worker)
        bridge = Bridge(self)
        self.window = webview.create_window(
            "Чушпан",
            html=(
                "<html lang='ru'><body style='background:#15121f;color:#f5e9ec;"
                "font:20px system-ui;padding:60px'><h1>Чушпан</h1>"
                "<p>Запускаю интерфейс…</p>"
                "<p>Микрофон выключен до включения прослушивания.</p></body></html>"
            ),
            js_api=bridge,
            width=1440,
            height=900,
            min_size=(720, 540),
            background_color=BACKGROUND,
            hidden=self.prefs.start_hidden or not self.prefs.show_window,
        )
        bridge.window = self.window
        self.window.events.closing += self.close_window
        self.window.events.minimized += lambda: setattr(self, "minimized", True)
        self.window.events.restored += lambda: setattr(self, "minimized", False)

        def start():
            callAfter(self.cocoa)
            self.monitor()

        try:
            webview.start(start, gui="cocoa", private_mode=True)
        finally:
            self.quit()
        return 0
