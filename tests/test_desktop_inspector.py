"""Inspection is automatic in source launches and opt-in in packaged releases."""

import sys
from types import SimpleNamespace

import pytest

from backend.desktop import __main__ as desktop_main
from backend.desktop import app as desktop_app


@pytest.mark.parametrize(
    ("frozen", "explicit", "expected"),
    [(False, None, True), (True, None, False), (True, True, True), (False, False, False)],
)
def test_inspection_policy_is_per_launch(tmp_path, monkeypatch, frozen, explicit, expected):
    monkeypatch.setattr(desktop_app, "USER_ROOT", tmp_path)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    app = desktop_app.Application(developer_mode=explicit)
    assert app.developer_mode is expected
    assert not (tmp_path / "application.json").exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_native_wkwebview_receives_explicit_inspectability(monkeypatch, enabled):
    calls = []
    view = SimpleNamespace(setInspectable_=calls.append, isInspectable=lambda: calls[-1])
    monkeypatch.setitem(
        sys.modules,
        "webview.platforms.cocoa",
        SimpleNamespace(
            BrowserView=SimpleNamespace(instances={"hud": SimpleNamespace(webview=view)})
        ),
    )
    app = desktop_app.Application.__new__(desktop_app.Application)
    app.window = SimpleNamespace(uid="hud")
    app.developer_mode = enabled
    app._configure_web_inspector()
    assert calls == [enabled]


def test_older_webkit_does_not_break_startup(monkeypatch, caplog):
    monkeypatch.setitem(
        sys.modules,
        "webview.platforms.cocoa",
        SimpleNamespace(
            BrowserView=SimpleNamespace(instances={"hud": SimpleNamespace(webview=object())})
        ),
    )
    app = desktop_app.Application.__new__(desktop_app.Application)
    app.window = SimpleNamespace(uid="hud")
    app.developer_mode = True
    app._configure_web_inspector()
    assert "requires macOS 13.3" in caplog.text


@pytest.mark.parametrize("arguments, expected", [([], None), (["--developer-mode"], True)])
def test_developer_flag_reaches_shell(monkeypatch, arguments, expected):
    calls = []

    class App:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def run(self):
            return 0

    monkeypatch.setattr(sys, "argv", ["Chushpan", *arguments])
    monkeypatch.setattr(desktop_main.multiprocessing, "freeze_support", lambda: None)
    monkeypatch.setattr(desktop_app, "Application", App)
    assert desktop_main.main() == 0
    assert calls == [{"smoke": False, "no_auto_listen": False, "developer_mode": expected}]
