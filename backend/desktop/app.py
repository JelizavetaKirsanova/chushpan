"""Cocoa main process: one reusable WebKit HUD, menu bar and owned worker."""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import html
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from backend.desktop import BUNDLE_ID, VERSION
from backend.desktop import platform as macos
from backend.desktop.storage import load_preferences, migrate, save_preferences
from backend.hud.window import BACKGROUND, _Api
from backend.instance import InstanceLock
from backend.paths import USER_ROOT

log = logging.getLogger(__name__)
BACKEND_READY_TIMEOUT_S = 45
FRONTEND_LOAD_TIMEOUT_S = 30
SPLASH_LOAD_TIMEOUT_S = 15
HEALTH_TIMEOUT_S = 2
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


def startup_error_page(message: str) -> str:
    safe_message = html.escape(message)
    return (
        "<!doctype html><html lang='ru'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Чушпан · запуск интерфейса</title>"
        "<body style='margin:0;background:#15121f;color:#f5e9ec;font:17px system-ui'>"
        "<main style='max-width:720px;margin:12vh auto;padding:32px'>"
        "<h1>Не удалось подключить интерфейс</h1>"
        f"<p>{safe_message}</p>"
        "<p>Голосовой движок и пользовательские данные не удалены.</p>"
        "<button style='padding:12px 18px;border-radius:10px' "
        "onclick='window.pywebview.api.desktop_retry_frontend()'>"
        "Повторить подключение</button></main></body></html>"
    )


def local_urlopen(url: str | urllib.request.Request, *, timeout: float):
    """Access the private loopback service directly, ignoring system HTTP proxies."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(url, timeout=timeout)


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

    def desktop_retry_frontend(self):
        return self._shell.desktop_retry_frontend()


class Application(_Api):
    def __init__(
        self,
        *,
        smoke=False,
        no_auto_listen=False,
        developer_mode: bool | None = None,
        frontend_dev=False,
    ):
        super().__init__()
        if frontend_dev and getattr(sys, "frozen", False):
            raise ValueError("frontend-dev is available only when running from source")
        self.smoke = smoke
        self.no_auto_listen = no_auto_listen
        self.developer_mode = frontend_dev or (
            not getattr(sys, "frozen", False) if developer_mode is None else developer_mode
        )
        self.frontend_server = None
        if frontend_dev:
            from backend.desktop.frontend_dev import FrontendDevServer
            from backend.paths import RESOURCE_ROOT

            self.frontend_server = FrontendDevServer(RESOURCE_ROOT)
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
        self.hotkey_proc = None
        self.ambient_hotkey_status = "disabled"
        self._log_file = None
        self.url = None
        self.quitting = False
        self.hidden = False
        self.worker_state = {"state": "starting", "components": {}}
        self.restarts = 0
        self.loaded = False
        self.initial_splash_loaded = False
        self.webview_started_at = None
        self.frontend_load_pending = False
        self.frontend_load_started_at = None
        self.frontend_attempts = 0
        self.frontend_error_shown = False
        self.frontend_retry_after = 0.0
        self.frontend_retry_requested = threading.Event()
        self.worker_started_at = None
        self.expected_worker_pid = None
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
        with local_urlopen(request, timeout=3) as response:
            return json.load(response)

    def _configure_startup_logging(self) -> None:
        path = self.logs / "desktop.log"
        root = logging.getLogger()
        for handler in root.handlers:
            if getattr(handler, "_chushpan_desktop_log", False) and getattr(
                handler, "baseFilename", None
            ) == str(path):
                return
        handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler._chushpan_desktop_log = True
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        path.chmod(0o600)
        log.info("DESKTOP_BOOT stage=logger_ready")

    def start_worker(self):
        with self.worker_lock:
            if self.quitting or self.proc and self.proc.poll() is None:
                return
            self.url = None
            self.loaded = False
            self.frontend_load_pending = False
            self.frontend_load_started_at = None
            self.frontend_error_shown = False
            self.frontend_retry_after = 0.0
            self.worker_started_at = time.monotonic()
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
            self.expected_worker_pid = self.proc.pid
            log.info(
                "DESKTOP_BOOT stage=worker_started pid=%s version=%s",
                self.proc.pid,
                VERSION,
            )

    def stop_worker(self):
        with self.worker_lock:
            if self.frontend_server:
                self.frontend_server.stop()
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
            self.expected_worker_pid = None

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
                "ambient_hotkey": self.ambient_hotkey_status,
                "login": macos.login_status(),
                "permissions": macos.permissions(),
                "worker": self.worker_state,
                "smoke": self.smoke,
                "auto_listen_suppressed": self.no_auto_listen,
            }

    def desktop_retry_frontend(self):
        """Ask the monitor to re-probe the local service and reload only the WebView."""
        if self.quitting:
            return {"ok": False, "message": "Приложение завершает работу."}
        log.info("DESKTOP_BOOT stage=frontend_retry_requested")
        self.frontend_retry_requested.set()
        return {"ok": True, "message": "Повторно подключаю интерфейс к локальному серверу."}

    def desktop_preferences(self, value):
        with self.native_lock:
            previous = self.prefs
            self.prefs = save_preferences(self.root, value)
            if self.prefs.ambient_hotkey_enabled:
                if (
                    previous.ambient_hotkey_enabled
                    and previous.ambient_hotkey_key != self.prefs.ambient_hotkey_key
                ):
                    self.stop_global_hotkey()
                self.start_global_hotkey()
            else:
                self.stop_global_hotkey()
        message = (
            "Сохранено. Глобальная клавиша Ambient включена."
            if self.prefs.ambient_hotkey_enabled
            else "Настройки приложения сохранены."
        )
        if self.prefs.ambient_hotkey_enabled and self.ambient_hotkey_status == "conflict":
            message = (
                f"Настройки сохранены, но ⌃⌥⌘{self.prefs.ambient_hotkey_key.upper()} "
                "занята другим приложением. "
                "Выключи и включи параметр после освобождения клавиши."
            )
        elif self.prefs.ambient_hotkey_enabled and self.ambient_hotkey_status == "unavailable":
            message = (
                "Настройки сохранены, но помощник глобальной клавиши недоступен в этой сборке."
            )
        return {
            "ok": True,
            "message": message,
            "status": self.desktop_status(),
        }

    def _ambient_hotkey_binary(self) -> Path:
        from backend.paths import RESOURCE_ROOT, native_helper

        if helper := native_helper("ambient_hotkey"):
            return helper
        source = RESOURCE_ROOT / "backend/ambient_hotkey.swift"
        if not source.is_file() or not shutil.which("xcrun"):
            raise RuntimeError("Не найден помощник глобальной клавиши для macOS.")
        directory = self.root / "data/native"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        target = directory / f"ambient-hotkey-{digest}"
        temporary = directory / f".ambient-hotkey-{digest}-{os.getpid()}"
        try:
            subprocess.run(
                ["xcrun", "swiftc", "-O", str(source), "-o", str(temporary)],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=90,
            )
            temporary.chmod(0o700)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def start_global_hotkey(self):
        if self.smoke:
            self.ambient_hotkey_status = "disabled"
            return
        if self.hotkey_proc and self.hotkey_proc.poll() is None:
            return
        try:
            binary = self._ambient_hotkey_binary()
            process = subprocess.Popen(
                [str(binary), self.prefs.ambient_hotkey_key],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            self.hotkey_proc = process
            self.ambient_hotkey_status = "starting"
            threading.Thread(target=self._read_global_hotkey, args=(process,), daemon=True).start()
        except Exception as exc:
            self.ambient_hotkey_status = "unavailable"
            log.warning("Global Ambient hotkey could not start: %s", type(exc).__name__)

    def _read_global_hotkey(self, process):
        try:
            for line in process.stdout or ():
                message = line.strip()
                if message == "READY":
                    self.ambient_hotkey_status = "enabled"
                elif message == "TOGGLE":
                    try:
                        from PyObjCTools.AppHelper import callAfter

                        callAfter(self.toggle_ambient_hotkey)
                    except Exception:
                        log.exception("Could not schedule Ambient hotkey action")
                elif message.startswith("ERROR"):
                    self.ambient_hotkey_status = "conflict"
                    log.warning("Global Ambient hotkey registration failed")
        except (OSError, ValueError):
            if not self.quitting:
                self.ambient_hotkey_status = "unavailable"
        finally:
            if self.hotkey_proc is process:
                self.hotkey_proc = None
                if not self.quitting:
                    if self.prefs.ambient_hotkey_enabled:
                        if self.ambient_hotkey_status != "conflict":
                            self.ambient_hotkey_status = "unavailable"
                    else:
                        self.ambient_hotkey_status = "disabled"

    def toggle_ambient_hotkey(self):
        if self.quitting or not self.window:
            return
        self.hidden = False
        self.window.show()
        self.window.restore()
        if self.loaded:
            self.window.evaluate_js(
                "if (window.chushpanToggleAmbient) window.chushpanToggleAmbient(); "
                "else window.chushpanHotkeyPending = !window.chushpanHotkeyPending;"
            )
        else:
            self.window.evaluate_js("window.chushpanHotkeyPending = !window.chushpanHotkeyPending;")

    def stop_global_hotkey(self):
        process, self.hotkey_proc = self.hotkey_proc, None
        self.ambient_hotkey_status = "disabled"
        if not process:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if process.stdout:
            process.stdout.close()

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
        self.stop_global_hotkey()
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

    def _endpoint_url(self) -> str | None:
        if not self.endpoint.is_file():
            return None
        try:
            value = json.loads(self.endpoint.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                log.warning("DESKTOP_BOOT stage=endpoint_rejected")
                return None
            url = value.get("url")
            pid = value.get("pid")
            parsed = urlsplit(url) if isinstance(url, str) else None
            if (
                parsed is None
                or parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.port is None
                or parsed.path not in {"", "/"}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or (self.expected_worker_pid and pid != self.expected_worker_pid)
            ):
                log.warning("DESKTOP_BOOT stage=endpoint_rejected")
                return None
            return url.rstrip("/")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            log.warning("DESKTOP_BOOT stage=endpoint_invalid")
            return None

    @staticmethod
    def _probe_frontend(url: str, *, dev_url: str | None = None) -> tuple[bool, str, str]:
        """Require a responsive health route, real HTML, and the shipped static assets."""
        try:
            with local_urlopen(url + "/_desktop/health", timeout=HEALTH_TIMEOUT_S) as response:
                if response.status != 200:
                    return False, f"Локальный backend вернул HTTP {response.status}.", "unknown"
                health = json.loads(response.read(16_384))
            if not isinstance(health, dict):
                return False, "Локальный backend вернул некорректный health-ответ.", "unknown"
            voice_stage = str(health.get("voice", "unknown"))[:48]
            if health.get("backend") != "ready":
                return False, "Backend ещё не подтвердил готовность.", voice_stage
            frontend = health.get("frontend")
            if not dev_url and (
                not isinstance(frontend, dict) or frontend.get("ready") is not True
            ):
                return False, "В bundle не найдены index.html или frontend assets.", voice_stage
            with local_urlopen((dev_url or url) + "/", timeout=HEALTH_TIMEOUT_S) as response:
                content_type = response.headers.get("Content-Type", "")
                document = response.read(128_000)
                if response.status != 200 or "text/html" not in content_type:
                    return False, "Frontend не вернул HTML документа.", voice_stage
            if b"<html" not in document.lower() and b"<!doctype html" not in document.lower():
                return False, "Ответ frontend не содержит HTML-документ.", voice_stage
            assets = (
                ["/@vite/client", "/src/main.ts"] if dev_url else frontend.get("assets", [])[:8]
            )
            for asset in assets:
                if not isinstance(asset, str) or not asset.startswith("/"):
                    continue
                with local_urlopen(
                    urljoin((dev_url or url) + "/", asset.lstrip("/")), timeout=HEALTH_TIMEOUT_S
                ) as response:
                    if response.status != 200:
                        return False, "Один из frontend assets недоступен.", voice_stage
            return True, "", voice_stage
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return False, f"Локальный backend пока не отвечает ({type(exc).__name__}).", "unknown"

    def _load_url_on_gui_thread(self, url: str) -> None:
        try:
            self.window.load_url(url)
        except Exception as exc:
            self.frontend_load_pending = False
            self.frontend_retry_after = time.monotonic() + min(
                5 * max(self.frontend_attempts, 1), 30
            )
            log.exception("DESKTOP_BOOT stage=webview_load_failed type=%s", type(exc).__name__)
            self._show_startup_error(
                "WebView не принял адрес frontend. Нажми «Повторить подключение»."
            )

    def _on_frontend_loaded(self):
        if not self.frontend_load_pending:
            self.initial_splash_loaded = True
            log.info("DESKTOP_BOOT stage=splash_loaded")
            return
        try:
            current_url = self.window.get_current_url()
            expected = urlsplit(getattr(self, "frontend_url", None) or self.url or "")
            current = urlsplit(current_url or "")
        except Exception as exc:
            log.warning("DESKTOP_BOOT stage=loaded_event_unverified type=%s", type(exc).__name__)
            return
        if (
            not expected.netloc
            or current.scheme != expected.scheme
            or current.netloc != expected.netloc
        ):
            log.info("DESKTOP_BOOT stage=stale_loaded_event_ignored")
            return
        self.frontend_load_pending = False
        self.frontend_load_started_at = None
        self.loaded = True
        self.frontend_error_shown = False
        log.info("DESKTOP_BOOT stage=frontend_loaded attempt=%d", self.frontend_attempts)

    def _request_frontend_load(self, url: str, *, frontend_url: str | None = None) -> None:
        from PyObjCTools.AppHelper import callAfter

        self.url = url
        self.frontend_url = frontend_url or url
        self.frontend_attempts += 1
        self.frontend_load_pending = True
        self.frontend_load_started_at = time.monotonic()
        self.frontend_retry_after = 0.0
        self.frontend_error_shown = False
        self.loaded = False
        log.info("DESKTOP_BOOT stage=webview_load_requested attempt=%d", self.frontend_attempts)
        callAfter(self._load_url_on_gui_thread, self.frontend_url)

    def _show_startup_error(self, message: str) -> None:
        if self.frontend_error_shown or not self.window:
            return
        self.frontend_error_shown = True
        try:
            from PyObjCTools.AppHelper import callAfter

            log.error("DESKTOP_BOOT stage=startup_error message=%s", message)
            if self.loaded:
                callAfter(
                    self.window.evaluate_js,
                    "window.chushpanNativeNotice && window.chushpanNativeNotice("
                    + json.dumps(message, ensure_ascii=False)
                    + ");",
                )
            else:
                callAfter(self.window.load_html, startup_error_page(message))
        except Exception as exc:
            log.exception(
                "DESKTOP_BOOT stage=startup_error_display_failed type=%s", type(exc).__name__
            )

    def _handle_frontend_retry(self) -> None:
        self.frontend_retry_requested.clear()
        if self.frontend_server:
            self.frontend_server.stop()
        if self.proc is None or self.proc.poll() is not None:
            self.stop_worker()
            self.start_worker()
            return
        self.frontend_error_shown = False
        self.frontend_load_pending = False
        self.frontend_load_started_at = None
        self.frontend_retry_after = 0.0
        self.worker_started_at = time.monotonic()
        self.url = None
        self.loaded = False
        log.info("DESKTOP_BOOT stage=frontend_retry_started")

    def monitor(self):
        self.start_worker()
        log.info("DESKTOP_BOOT stage=monitor_started")
        while not self.quitting:
            try:
                if self.frontend_retry_requested.is_set():
                    self._handle_frontend_retry()
                if self.proc and self.proc.poll() is not None:
                    code = self.proc.returncode
                    log.error("DESKTOP_BOOT stage=worker_exited code=%s", code)
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
                    else:
                        self._show_startup_error(
                            "Голосовой backend завершился при запуске. "
                            "Открой журнал запуска из меню приложения."
                        )
                elif self.proc:
                    endpoint_url = self._endpoint_url()
                    if endpoint_url and self.loaded:
                        self.worker_state = self.control({"op": "status"})
                    elif (
                        endpoint_url
                        and not self.frontend_load_pending
                        and time.monotonic() >= self.frontend_retry_after
                    ):
                        dev_url = None
                        if self.frontend_server:
                            try:
                                dev_ready = self.frontend_server.start(endpoint_url, self._log_file)
                            except (OSError, ValueError, RuntimeError) as exc:
                                self._show_startup_error(str(exc))
                                time.sleep(1)
                                continue
                            if not dev_ready:
                                if (
                                    self.worker_started_at
                                    and time.monotonic() - self.worker_started_at
                                    > BACKEND_READY_TIMEOUT_S
                                ):
                                    self._show_startup_error(
                                        "Vite не подтвердил запуск. Проверь application.log."
                                    )
                                time.sleep(0.2)
                                continue
                            dev_url = self.frontend_server.url
                        if dev_url:
                            ready, reason, voice_stage = self._probe_frontend(
                                endpoint_url, dev_url=dev_url
                            )
                        else:
                            ready, reason, voice_stage = self._probe_frontend(endpoint_url)
                        if ready:
                            if not self.initial_splash_loaded:
                                reason = "Ожидаю загрузку стартового окна WebView."
                            elif self.url != endpoint_url or not self.loaded:
                                if dev_url:
                                    self._request_frontend_load(endpoint_url, frontend_url=dev_url)
                                else:
                                    self._request_frontend_load(endpoint_url)
                            self.worker_state = self.control({"op": "status"})
                            if voice_stage == "voice_error":
                                self._show_startup_error(
                                    "Голосовой движок не завершил инициализацию. "
                                    "Проверь журнал и нажми «Повторить подключение»."
                                )
                        elif (
                            self.worker_started_at
                            and time.monotonic() - self.worker_started_at > BACKEND_READY_TIMEOUT_S
                        ):
                            self._show_startup_error(reason)
                        if (
                            not self.initial_splash_loaded
                            and self.webview_started_at
                            and time.monotonic() - self.webview_started_at > SPLASH_LOAD_TIMEOUT_S
                        ):
                            self._show_startup_error(
                                "WebView не завершил загрузку стартового окна за 15 секунд. "
                                "Проверь журнал запуска."
                            )
                        if (
                            self.frontend_load_pending
                            and self.frontend_load_started_at
                            and time.monotonic() - self.frontend_load_started_at
                            > FRONTEND_LOAD_TIMEOUT_S
                        ):
                            self.frontend_load_pending = False
                            self.frontend_retry_after = time.monotonic() + min(
                                5 * max(self.frontend_attempts, 1), 30
                            )
                            self._show_startup_error(
                                "WebView не завершил загрузку интерфейса за 30 секунд. "
                                "Повтори подключение."
                            )
                    elif (
                        self.worker_started_at
                        and time.monotonic() - self.worker_started_at > BACKEND_READY_TIMEOUT_S
                    ):
                        self._show_startup_error(
                            "Backend не опубликовал готовый локальный endpoint за 45 секунд. "
                            "Повтори подключение."
                        )
                    if (
                        self.prefs.auto_listen
                        and not self.no_auto_listen
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
            except Exception as exc:
                log.exception("DESKTOP_BOOT stage=monitor_error type=%s", type(exc).__name__)
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

            def logs_(self, sender):
                threading.Thread(target=lambda: shell._menu_action("logs"), daemon=True).start()

            def reconnect_(self, sender):
                threading.Thread(target=shell.desktop_retry_frontend, daemon=True).start()

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
            ("Переподключить интерфейс", "reconnect:", ""),
            ("Журнал запуска", "logs:", ""),
            ("Завершить Чушпана", "quit:", "q"),
        ]:
            item = menu.addItemWithTitle_action_keyEquivalent_(title, selector, key)
            item.setTarget_(self.delegate)
            if selector == "listen:":
                self.listen_item = item
        self.status_item.setMenu_(menu)
        self.refresh_menu()
        if self.prefs.ambient_hotkey_enabled:
            self.start_global_hotkey()

    def _configure_web_inspector(self):
        """before_show runs synchronously on Cocoa's main thread, before navigation."""
        from webview.platforms.cocoa import BrowserView

        # window.native is an NSWindow; its contentView is only attached after
        # navigation. Use the WKWebView already owned by pywebview instead.
        view = BrowserView.instances[self.window.uid].webview
        if hasattr(view, "setInspectable_"):
            view.setInspectable_(self.developer_mode)
            log.info("DESKTOP_WEB_INSPECTOR enabled=%s", bool(view.isInspectable()))
        elif self.developer_mode:
            log.warning("Safari Web Inspector requires macOS 13.3 or newer")

    def run(self):
        import webview
        from PyObjCTools.AppHelper import callAfter

        if not self.singleton():
            return 0
        self._configure_startup_logging()
        try:
            from backend.storage_manager import cleanup_stale_builds

            cleanup_stale_builds()
        except Exception:
            log.exception("STORAGE_STARTUP stale build cleanup failed")
        atexit.register(self.stop_worker)
        bridge = Bridge(self)
        self.webview_started_at = time.monotonic()
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
        if self.frontend_server:
            # PyCharm Stop / Ctrl+C must unwind owned Node and worker processes.
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_args: callAfter(self.quit))
        self.window.events.before_show += self._configure_web_inspector
        self.window.events.closing += self.close_window
        self.window.events.loaded += self._on_frontend_loaded
        self.window.events.minimized += lambda: setattr(self, "minimized", True)
        self.window.events.restored += lambda: setattr(self, "minimized", False)

        def start():
            callAfter(self.cocoa)
            self.monitor()

        try:
            log.info("DESKTOP_BOOT stage=webview_start version=%s", VERSION)
            # Cocoa ignores storage_path. Its default WKWebsiteDataStore persists
            # localStorage/IndexedDB, isolated by this profile's stable HUD origin.
            # private_mode=True would delete ALL of that store on every launch.
            webview.start(start, gui="cocoa", private_mode=False)
        finally:
            self.quit()
        return 0
