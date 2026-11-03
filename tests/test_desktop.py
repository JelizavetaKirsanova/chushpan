"""Desktop boundaries: tests never open audio, devices or real user data."""

import asyncio
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from backend.desktop.storage import load_preferences, migrate, save_preferences
from backend.desktop.worker import DesktopEngine
from backend.hud.server import create_app


def test_preferences_are_strict_private_and_durable(tmp_path):
    assert not load_preferences(tmp_path).auto_listen
    saved = save_preferences(tmp_path, {"start_hidden": True, "energy_saving": True})
    assert load_preferences(tmp_path) == saved
    assert (tmp_path / "application.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValidationError):
        save_preferences(tmp_path, {"auto_listen": "yes"})
    with pytest.raises(ValidationError):
        save_preferences(tmp_path, {"unknown": True})
    assert load_preferences(tmp_path) == saved


def test_migration_includes_wal_and_preserves_source_and_destination(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    (source / "data").mkdir()
    (source / "config.yaml").write_text("data_dir: data")
    (source / ".env").write_text("EXAMPLE=private")
    db = sqlite3.connect(source / "data/jarvis.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE test(value)")
    db.execute("INSERT INTO test VALUES (42)")
    db.commit()
    files = migrate(source, target)
    assert "data/jarvis.db" in files
    with sqlite3.connect(target / "data/jarvis.db") as copy:
        assert copy.execute("SELECT value FROM test").fetchone() == (42,)
    assert not (target / "data/jarvis.db-wal").exists()
    assert (source / ".env").read_text() == "EXAMPLE=private"
    (target / ".env").write_text("preserve")
    assert migrate(source, target) == []
    assert (target / ".env").read_text() == "preserve"
    db.close()


def test_migration_refuses_symlink(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("data_dir: data")
    (source / ".env").symlink_to(tmp_path / "secret")
    with pytest.raises(ValueError, match="ссылка"):
        migrate(source, tmp_path / "target")


def test_controls_require_token_and_reject_browser_origins():
    async def run():
        engine = DesktopEngine(None, "private-test-token", smoke=True)
        app = create_app(engine.bus, set(), native=engine)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            assert (await client.post("/_desktop", json={"op": "status"})).status_code == 403
            headers = {"X-Chushpan-Token": "private-test-token"}
            assert (
                await client.post(
                    "/_desktop", headers={**headers, "Origin": "http://evil"}, json={"op": "status"}
                )
            ).status_code == 403
            assert (
                await client.post(
                    "/_desktop", headers=headers, json={"op": "listen", "enabled": True}
                )
            ).status_code == 409
            assert (
                await client.post("/_desktop", headers=headers, json={"op": "mute"})
            ).status_code == 400

    asyncio.run(run())


def test_microphone_gate_never_starts_before_enable_and_stops_immediately():
    async def run():
        engine = DesktopEngine(None, "token")
        starts, stops, warmed = [], [], []

        class Microphone:
            def start(self):
                starts.append(True)

            def stop(self):
                stops.append(True)

            def flush(self):
                pass

            async def frames(self):
                yield "frame"
                await asyncio.Event().wait()

        loop = SimpleNamespace(
            command=lambda _: None,
            _close_session=lambda: None,
            microphone_running=False,
            _publish_voice=lambda: None,
        )

        async def warm():
            warmed.append(True)

        stream = engine.capture(Microphone(), loop, warm)
        waiting = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        assert starts == [] and warmed == []
        engine.set_listening(True)
        assert await asyncio.wait_for(waiting, 1) == "frame"
        assert len(starts) == len(warmed) == 1
        engine.set_listening(False)
        assert stops and not loop.microphone_running
        await stream.aclose()

    asyncio.run(run())


def test_singleton_reopens_existing_window_without_another_worker(tmp_path, monkeypatch):
    import time

    from backend.desktop import app

    monkeypatch.setattr(app, "USER_ROOT", tmp_path)
    first, second = app.Application(smoke=True), app.Application(smoke=True)
    opened = []
    first.show = lambda: opened.append(True)
    try:
        assert first.singleton()
        assert not second.singleton()
        for _ in range(20):
            if opened:
                break
            time.sleep(0.01)
        assert opened == [True]
        assert first.proc is second.proc is None
    finally:
        first.quit()


def test_corrupt_preferences_preserve_file_and_disable_auto_listen(tmp_path):
    original = "{broken json"
    (tmp_path / "application.json").write_text(original)
    assert load_preferences(tmp_path).auto_listen is False
    assert (tmp_path / "application.json").read_text() == original


@pytest.mark.parametrize("action", ["listen", "login", "migrate", "disable_legacy"])
def test_smoke_mode_blocks_recording_migration_and_system_autostart(action):
    from backend.desktop.app import Application

    app = Application.__new__(Application)
    app.smoke = True
    result = app.desktop_action(action)
    assert result["ok"] is False
    assert "проверочном режиме" in result["message"]


def test_desktop_publishes_preparing_and_muting_during_load_never_opens_input():
    async def run():
        engine = DesktopEngine(None, "token")
        warm_started, release = asyncio.Event(), asyncio.Event()
        changes, starts = [], []

        class Microphone:
            def start(self):
                starts.append(True)

            def stop(self):
                pass

            def flush(self):
                pass

        loop = SimpleNamespace(
            command=lambda _: None,
            _close_session=lambda: None,
            microphone_running=False,
            microphone_starting=False,
        )
        loop._publish_voice = lambda: changes.append(
            (loop.microphone_starting, loop.microphone_running)
        )

        async def warm():
            warm_started.set()
            await release.wait()

        stream = engine.capture(Microphone(), loop, warm)
        pending = asyncio.create_task(anext(stream))
        engine.set_listening(True)
        await asyncio.wait_for(warm_started.wait(), 1)
        assert changes[-1] == (True, False)
        engine.set_listening(False)
        assert changes[-1] == (False, False)
        release.set()
        await asyncio.sleep(0)
        assert starts == []
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()

    asyncio.run(run())
