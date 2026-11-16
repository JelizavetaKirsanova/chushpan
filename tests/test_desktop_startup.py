import asyncio
import json
import logging
import sys
import threading

import httpx

from backend.desktop import app as desktop_app
from backend.desktop import worker as desktop_worker
from backend.desktop.worker import DesktopEngine
from backend.hud.bus import HudBus
from backend.hud.server import create_app, frontend_readiness


def test_frontend_readiness_checks_index_and_local_assets(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<link rel="stylesheet" href="/assets/app.css">'
        '<script type="module" src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (dist / "assets/app.css").write_text("body {}", encoding="utf-8")
    (dist / "assets/app.js").write_text("console.log('ready')", encoding="utf-8")

    report = frontend_readiness(dist)
    assert report["ready"] is True
    assert report["assets_checked"] == 2

    (dist / "assets/app.js").unlink()
    report = frontend_readiness(dist)
    assert report["ready"] is False
    assert report["missing"] == ["app.js"]


def test_desktop_health_reports_backend_and_packaged_frontend(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<script src="/assets/app.js"></script>', encoding="utf-8")
    (dist / "assets/app.js").write_text("", encoding="utf-8")
    engine = DesktopEngine(None, "private-test-token")
    engine.boot_stage = "voice_importing"
    app = create_app(HudBus(), {"http://127.0.0.1:43210"}, dist=dist, native=engine)

    async def run():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 32100))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:43210"
        ) as client:
            response = await client.get("/_desktop/health")
        assert response.status_code == 200
        assert response.json() == {
            "backend": "ready",
            "frontend": {
                "ready": True,
                "assets_checked": 1,
                "assets": ["/assets/app.js"],
                "missing": [],
            },
            "voice": "voice_importing",
        }

    asyncio.run(run())


def test_frontend_error_endpoint_logs_only_safe_location(caplog, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    app = create_app(HudBus(), {"http://127.0.0.1:43210"}, dist=dist)

    async def run():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 32100))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:43210"
        ) as client:
            response = await client.post(
                "/_desktop/frontend-error",
                headers={"Origin": "http://127.0.0.1:43210"},
                json={
                    "type": "TypeError",
                    "source": "http://127.0.0.1:43210/assets/main.js?secret=private",
                    "line": 17,
                    "message": "conversation text must not be logged",
                },
            )
        assert response.status_code == 204

    with caplog.at_level(logging.ERROR, logger="backend.hud.server"):
        asyncio.run(run())
    assert "HUD_FRONTEND_JS_ERROR type=TypeError source=main.js line=17" in caplog.text
    assert "private" not in caplog.text
    assert "conversation text" not in caplog.text


def test_voice_runtime_import_runs_off_the_hud_event_loop(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    marker = object()

    def slow_prepare():
        started.set()
        assert release.wait(2)
        return marker

    monkeypatch.setattr(desktop_worker, "_prepare_desktop_voice", slow_prepare)

    async def run():
        load = asyncio.create_task(desktop_worker._load_desktop_voice())
        assert await asyncio.wait_for(asyncio.to_thread(started.wait), 1)
        await asyncio.sleep(0)
        assert not load.done()
        release.set()
        assert await asyncio.wait_for(load, 1) is marker

    asyncio.run(run())


def test_frontend_probe_requires_html_and_static_assets(monkeypatch):
    class Response:
        def __init__(self, payload, content_type="application/json"):
            self.payload = payload
            self.status = 200
            self.headers = {"Content-Type": content_type}

        def read(self, _limit=-1):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    responses = iter(
        [
            Response(
                json.dumps(
                    {
                        "backend": "ready",
                        "voice": "voice_importing",
                        "frontend": {"ready": True, "assets": ["/assets/app.js"]},
                    }
                ).encode()
            ),
            Response(b"<!doctype html><html></html>", "text/html"),
            Response(b"", "application/javascript"),
        ]
    )
    monkeypatch.setattr(desktop_app, "local_urlopen", lambda *_a, **_k: next(responses))

    ready, reason, voice = desktop_app.Application._probe_frontend("http://127.0.0.1:45678")
    assert (ready, reason, voice) == (True, "", "voice_importing")


def test_control_preserves_post_method_body_and_token(monkeypatch):
    observed = {}

    class Response:
        def read(self):
            return b'{"state":"ready"}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def open_local(request, *, timeout):
        observed["request"] = request
        observed["timeout"] = timeout
        return Response()

    monkeypatch.setattr(desktop_app, "local_urlopen", open_local)
    app = desktop_app.Application.__new__(desktop_app.Application)
    app.url = "http://127.0.0.1:43210"
    app.token = "private-test-token"

    assert app.control({"op": "status"}) == {"state": "ready"}
    request = observed["request"]
    headers = {name.lower(): value for name, value in request.header_items()}
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {"op": "status"}
    assert headers["x-chushpan-token"] == "private-test-token"
    assert headers["content-type"] == "application/json"
    assert observed["timeout"] == 3


def test_endpoint_accepts_only_current_local_worker(tmp_path):
    app = desktop_app.Application.__new__(desktop_app.Application)
    app.endpoint = tmp_path / "endpoint.json"
    app.expected_worker_pid = 42

    app.endpoint.write_text(json.dumps({"url": "http://127.0.0.1:45678", "pid": 42}))
    assert app._endpoint_url() == "http://127.0.0.1:45678"

    app.endpoint.write_text(json.dumps({"url": "http://127.0.0.1:45678", "pid": 41}))
    assert app._endpoint_url() is None

    app.endpoint.write_text(json.dumps({"url": "http://example.com:45678", "pid": 42}))
    assert app._endpoint_url() is None


def test_startup_error_page_escapes_backend_text_and_offers_retry():
    page = desktop_app.startup_error_page("<server unavailable>")
    assert "&lt;server unavailable&gt;" in page
    assert "window.pywebview.api.desktop_retry_frontend()" in page


def test_frozen_worker_dispatch_runs_before_argument_parser(monkeypatch):
    from backend.desktop import __main__ as desktop_main

    calls = []

    class App:
        def __init__(self, **_kwargs):
            pass

        def run(self):
            calls.append("run")
            return 0

    monkeypatch.setattr(sys, "argv", ["Chushpan"])
    monkeypatch.setattr(
        desktop_main.multiprocessing, "freeze_support", lambda: calls.append("freeze")
    )
    monkeypatch.setattr(desktop_app, "Application", App)

    assert desktop_main.main() == 0
    assert calls == ["freeze", "run"]


def test_manual_frontend_retry_only_sets_monitor_signal():
    app = desktop_app.Application.__new__(desktop_app.Application)
    app.quitting = False
    app.frontend_retry_requested = threading.Event()

    result = app.desktop_retry_frontend()
    assert result["ok"] is True
    assert app.frontend_retry_requested.is_set()


def test_stale_splash_load_event_does_not_mark_frontend_ready():
    app = desktop_app.Application.__new__(desktop_app.Application)
    app.window = type("Window", (), {"get_current_url": lambda _self: "about:blank"})()
    app.frontend_load_pending = True
    app.frontend_load_started_at = 10.0
    app.frontend_attempts = 1
    app.frontend_error_shown = False
    app.loaded = False
    app.url = "http://127.0.0.1:40000"

    app._on_frontend_loaded()
    assert app.frontend_load_pending is True
    assert app.loaded is False

    app.window.get_current_url = lambda: "http://127.0.0.1:40000/"
    app._on_frontend_loaded()
    assert app.frontend_load_pending is False
    assert app.loaded is True
