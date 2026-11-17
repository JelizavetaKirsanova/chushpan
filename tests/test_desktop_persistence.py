"""Native browser data survives restarts without silently changing its origin."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from backend.desktop import app as desktop_app
from backend.desktop.storage import load_web_port, save_web_port
from backend.desktop.worker import start_desktop_server


class FakeServer:
    def __init__(self, assigned_port=43210, available=True):
        self.s = SimpleNamespace(server=SimpleNamespace(host="ignored", port=0))
        self.assigned_port = assigned_port
        self.available = available
        self.requested = []
        self.stopped = False

    async def start(self):
        self.requested.append((self.s.server.host, self.s.server.port))
        if self.available and self.s.server.port == 0:
            self.s.server.port = self.assigned_port
        return self.available

    async def stop(self):
        self.stopped = True


def test_profile_reuses_first_bound_origin_and_isolates_another_profile(tmp_path):
    first_root, other_root = tmp_path / "first", tmp_path / "other"
    first = FakeServer()
    asyncio.run(start_desktop_server(first, first_root))
    assert first.requested == [("127.0.0.1", 0)]
    assert load_web_port(first_root) == 43210
    assert (first_root / "web-origin.json").stat().st_mode & 0o777 == 0o600

    restarted = FakeServer(assigned_port=54321)
    asyncio.run(start_desktop_server(restarted, first_root))
    assert restarted.requested == [("127.0.0.1", 43210)]
    assert restarted.s.server.port == 43210

    other = FakeServer(assigned_port=54321)
    asyncio.run(start_desktop_server(other, other_root))
    assert other.requested == [("127.0.0.1", 0)]
    assert load_web_port(other_root) == 54321
    assert load_web_port(first_root) == 43210


def test_busy_saved_port_never_falls_back_or_overwrites_the_origin(tmp_path):
    save_web_port(tmp_path, 43210)
    original = (tmp_path / "web-origin.json").read_bytes()
    busy = FakeServer(available=False)
    with pytest.raises(RuntimeError, match="43210 занят"):
        asyncio.run(start_desktop_server(busy, tmp_path))
    assert busy.requested == [("127.0.0.1", 43210)]
    assert (tmp_path / "web-origin.json").read_bytes() == original


@pytest.mark.parametrize("raw", ["{broken", "{}", '{"port":true}', '{"port":0}', '{"port":70000}'])
def test_corrupt_origin_stops_before_binding_and_preserves_file(tmp_path, raw):
    path = tmp_path / "web-origin.json"
    path.write_text(raw)
    server = FakeServer()
    with pytest.raises(RuntimeError, match="Сохранённый адрес"):
        asyncio.run(start_desktop_server(server, tmp_path))
    assert server.requested == []
    assert path.read_text() == raw


def test_cannot_replace_existing_origin_or_follow_a_symlink(tmp_path):
    save_web_port(tmp_path, 43210)
    with pytest.raises(RuntimeError, match="Адрес интерфейса изменился"):
        save_web_port(tmp_path, 54321)
    assert load_web_port(tmp_path) == 43210
    other = tmp_path / "other"
    other.mkdir()
    (other / "web-origin.json").symlink_to(tmp_path / "web-origin.json")
    with pytest.raises(RuntimeError, match="символической ссылкой"):
        load_web_port(other)


def test_failure_to_persist_origin_stops_unpublished_server(tmp_path, monkeypatch):
    from backend.desktop import worker

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(worker, "save_web_port", fail)
    server = FakeServer()
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(start_desktop_server(server, tmp_path))
    assert server.stopped
    assert not (tmp_path / "web-origin.json").exists()


def test_native_start_preserves_webkit_store_including_smoke_launch(tmp_path, monkeypatch):
    class Event:
        def __iadd__(self, _callback):
            return self

    events = SimpleNamespace(
        **{name: Event() for name in ("closing", "loaded", "minimized", "restored")}
    )
    calls = []
    fake_webview = SimpleNamespace(
        create_window=lambda *_args, **_kwargs: SimpleNamespace(events=events),
        start=lambda *_args, **kwargs: calls.append(kwargs),
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setitem(
        sys.modules, "PyObjCTools.AppHelper", SimpleNamespace(callAfter=lambda *_a: None)
    )
    monkeypatch.setattr(desktop_app, "USER_ROOT", tmp_path)
    monkeypatch.setattr(desktop_app.atexit, "register", lambda *_args: None)
    app = desktop_app.Application(smoke=True, no_auto_listen=True)
    monkeypatch.setattr(app, "singleton", lambda: True)
    monkeypatch.setattr(app, "_configure_startup_logging", lambda: None)
    monkeypatch.setattr(app, "quit", lambda: None)
    assert app.run() == 0
    assert calls == [{"gui": "cocoa", "private_mode": False}]
    assert not (tmp_path / "web-origin.json").exists()  # Worker has not run.
