"""Owned, loopback-only Vite process for explicit source development launches."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

DEV_URL = "http://127.0.0.1:5173"


class FrontendDevServer:
    url = DEV_URL

    def __init__(self, root: Path):
        self.root = root
        self.proc = None
        self.backend_url = None
        self._ready_fd = None
        self._ready = False

    def start(self, backend_url: str, output) -> bool:
        parsed = urlsplit(backend_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or not parsed.port
            or parsed.netloc != f"127.0.0.1:{parsed.port}"
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Vite requires an owned loopback backend")
        if self.proc is not None:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    "Vite завершился. Проверь application.log и повтори подключение."
                )
            if self.backend_url != backend_url:
                raise RuntimeError("Адрес backend изменился; перезапусти frontend-dev.")
            return self.ready()
        node = shutil.which(
            "node",
            path=os.pathsep.join(
                [os.environ.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"]
            ),
        )
        if not node:
            raise RuntimeError("Для frontend-dev нужен Node.js в PATH PyCharm.")
        # No application credentials or VITE_* variables reach the dev server.
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG"}}
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        try:
            self.proc = subprocess.Popen(
                [
                    node,
                    str(self.root / "frontend/scripts/desktop-dev.mjs"),
                    backend_url,
                    str(write_fd),
                ],
                cwd=self.root / "frontend",
                env=env,
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(write_fd,),
            )
        except BaseException:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        self._ready_fd = read_fd
        self.backend_url = backend_url
        return self.ready()

    def ready(self) -> bool:
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError("Vite завершился. Проверь application.log и повтори подключение.")
        if not self._ready and self._ready_fd is not None:
            with contextlib.suppress(BlockingIOError):
                # Only our child can signal that it successfully bound 5173.
                self._ready = os.read(self._ready_fd, 1) == b"R"
        return self._ready

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        self.backend_url = None
        self._ready = False
        if self._ready_fd is not None:
            os.close(self._ready_fd)
            self._ready_fd = None
        if proc is None:
            return
        # EOF also stops Node when the IDE kills Python without running finally.
        if proc.stdin:
            with contextlib.suppress(OSError):
                proc.stdin.close()
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=3)
