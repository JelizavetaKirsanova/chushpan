"""Development ownership, safe origins and production isolation."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.desktop import __main__ as desktop_main
from backend.desktop import app as desktop_app
from backend.desktop.frontend_dev import DEV_URL, FrontendDevServer


@pytest.mark.parametrize("frozen,worker", [(True, False), (False, True)])
def test_dev_cli_rejects_packaged_and_worker_launches(monkeypatch, frozen, worker):
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(
        sys, "argv", ["app", "--frontend-dev", *(["--desktop-worker"] if worker else [])]
    )
    with pytest.raises(SystemExit) as exc:
        desktop_main.main()
    assert exc.value.code == 2


@pytest.mark.parametrize("custom", [None, "/tmp/chushpan-explicit-dev-profile"])
def test_dev_cli_uses_separate_default_profile_and_real_backend(monkeypatch, custom):
    monkeypatch.setattr(sys, "argv", ["app", "--frontend-dev", "--no-auto-listen"])
    if custom:
        monkeypatch.setenv("CHUSHPAN_APP_DATA", custom)
    else:
        monkeypatch.delenv("CHUSHPAN_APP_DATA", raising=False)
    calls = []

    class App:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def run(self):
            import os

            calls.append(os.environ["CHUSHPAN_APP_DATA"])
            return 0

    monkeypatch.setattr(desktop_app, "Application", App)
    assert desktop_main.main() == 0
    assert calls[0] == {
        "frontend_dev": True,
        "smoke": False,
        "no_auto_listen": True,
        "developer_mode": None,
    }
    expected = custom or str(
        Path(desktop_main.__file__).resolve().parents[2] / "private/frontend-dev.noindex/profile"
    )
    assert calls[1] == expected


def test_frozen_constructor_cannot_start_vite_or_create_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(desktop_app, "USER_ROOT", tmp_path / "absent")
    with pytest.raises(ValueError, match="source"):
        desktop_app.Application(frontend_dev=True)
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8765",
        "http://127.0.0.1.evil:8765",
        "https://127.0.0.1:8765",
        "http://user:secret@127.0.0.1:8765",
        "http://127.0.0.1:8765/?token=secret",
        "http://127.0.0.1:8765/#hash",
        "http://127.0.0.1:8765/private",
    ],
)
def test_dev_server_never_proxies_non_owned_endpoints(url):
    with pytest.raises(ValueError):
        FrontendDevServer(Path(".")).start(url, None)


def test_node_is_owned_and_does_not_receive_credentials(monkeypatch, tmp_path):
    from backend.desktop import frontend_dev

    calls = []
    monkeypatch.setenv("CHUSHPAN_CONTROL_TOKEN", "test-only-control-value")
    monkeypatch.setenv("VITE_SECRET", "test-only-secret-value")
    monkeypatch.setattr(frontend_dev.shutil, "which", lambda *_a, **_k: "/node")
    proc = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(
        frontend_dev.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)) or proc
    )
    server = FrontendDevServer(tmp_path)
    server.start("http://127.0.0.1:43210", None)
    server.start("http://127.0.0.1:43210", None)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0][-2] == "http://127.0.0.1:43210"
    assert kwargs["stdin"] == subprocess.PIPE
    assert kwargs["start_new_session"] is True
    assert "VITE_SECRET" not in kwargs["env"]
    assert "CHUSHPAN_CONTROL_TOKEN" not in kwargs["env"]
    with pytest.raises(RuntimeError, match="изменился"):
        server.start("http://127.0.0.1:43211", None)
    assert not server.ready()  # No readiness byte from the mocked child.
    frontend_dev.os.close(server._ready_fd)
    server._ready_fd = None


def test_stopping_closes_parent_lifeline_and_waits():
    stdin = io.BytesIO()
    waits = []
    server = FrontendDevServer(Path("."))
    server.proc = SimpleNamespace(
        stdin=stdin, poll=lambda: None, wait=lambda **kw: waits.append(kw)
    )
    server.stop()
    server.stop()
    assert stdin.closed
    assert waits == [{"timeout": 5}]
    assert server.proc is None


def test_only_owned_child_readiness_allows_frontend_navigation():
    server = FrontendDevServer(Path("."))
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    server._ready_fd = read_fd
    server.proc = SimpleNamespace(poll=lambda: None)
    try:
        assert not server.ready()
        os.write(write_fd, b"R")
        assert server.ready()
        assert server.ready()
        server.proc.poll = lambda: 1
        with pytest.raises(RuntimeError, match="Vite"):
            server.ready()
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_dev_readiness_does_not_depend_on_dist_and_probes_vite(monkeypatch):
    urls = []

    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Type": "text/html"}

    def open_local(url, **_kwargs):
        urls.append(url)
        if url.endswith("/_desktop/health"):
            return Response(
                json.dumps(
                    {"backend": "ready", "voice": "voice_ready", "frontend": {"ready": False}}
                ).encode()
            )
        return Response(b"<!doctype html><html></html>")

    monkeypatch.setattr(desktop_app, "local_urlopen", open_local)
    assert desktop_app.Application._probe_frontend("http://127.0.0.1:43210", dev_url=DEV_URL) == (
        True,
        "",
        "voice_ready",
    )
    assert urls == [
        "http://127.0.0.1:43210/_desktop/health",
        DEV_URL + "/",
        DEV_URL + "/@vite/client",
        DEV_URL + "/src/main.ts",
    ]


def test_frontend_origin_does_not_replace_authenticated_control_origin(monkeypatch):
    loads = []
    monkeypatch.setitem(
        sys.modules, "PyObjCTools.AppHelper", SimpleNamespace(callAfter=lambda f, *a: f(*a))
    )
    app = desktop_app.Application.__new__(desktop_app.Application)
    app.frontend_attempts = 0
    app.window = SimpleNamespace(load_url=loads.append, get_current_url=lambda: DEV_URL + "/")
    app._request_frontend_load("http://127.0.0.1:43210", frontend_url=DEV_URL)
    assert app.url == "http://127.0.0.1:43210"
    assert loads == [DEV_URL]
    app._on_frontend_loaded()
    assert app.loaded
