import asyncio
import json
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.brain.conversation import Conversation
from backend.config import Settings
from backend.hud.bus import HudBus
from backend.hud.dashboard import Dashboard, city_zone, dashboard_settings
from backend.hud.window import _Api
from backend.hud.workspace import bind_workspace
from backend.memory.store import MemoryStore
from backend.tools.clock import clock_tools
from backend.tools.registry import ToolRegistry


def environment(path=":memory:"):
    store = MemoryStore(path)
    settings = Settings(language="ru-RU")
    bus = HudBus()
    events = []
    bus.publish = events.append
    bind_workspace(bus, settings, store)
    conv = Conversation(
        settings, SimpleNamespace(), store, tools=ToolRegistry(clock_tools(settings))
    )
    personal = SimpleNamespace(read=AsyncMock())
    weather = SimpleNamespace(forecast=AsyncMock())
    dashboard = Dashboard(bus, settings, store, conv, weather, personal=personal)
    dashboard.loop = SimpleNamespace(
        busy=False, submit_text=lambda value: events.append({"submitted": value})
    )
    return store, dashboard, events


def test_old_history_migrates_and_search_is_russian_and_excludes_tools(tmp_path):
    path = tmp_path / "history.db"
    store, dashboard, events = environment(path)
    identifier = dashboard.conv.id
    store.add(identifier, "user", "Мой план на неделю")
    store.add(identifier, "assistant", "Вот список")
    store.add(identifier, "tool", "невидимый ключ", tool_name="test")
    assert store.conversation_list("ПЛАН")[0]["id"] == identifier
    assert not store.conversation_list("невидимый")
    dashboard.request({"op": "conversation_rename", "id": identifier, "title": "Учёба"})
    dashboard.request({"op": "conversation_pin", "id": identifier})
    store.close()
    reopened = MemoryStore(path)
    try:
        assert reopened.conversation_list()[0]["title"] == "Учёба"
        assert reopened.conversation_list()[0]["pinned"] == 1
        assert reopened.count(identifier) == 3
    finally:
        reopened.close()


def test_conversation_trash_preserves_context_and_can_restore():
    store, dashboard, events = environment()
    identifier = dashboard.conv.id
    store.add(identifier, "user", "Сохранить этот разговор")
    dashboard.request({"op": "conversation_delete", "id": identifier})
    assert dashboard.conv.id != identifier
    assert not any(c["id"] == identifier for c in store.conversation_list())
    assert store.conversation_list(trash=True)[0]["id"] == identifier
    assert store.transcript(identifier)[0]["content"] == "Сохранить этот разговор"
    dashboard.request({"op": "conversation_restore", "id": identifier})
    assert any(c["id"] == identifier for c in store.conversation_list())
    store.close()


def test_notes_and_tasks_are_real_persistent_rows_and_bad_input_cannot_write(tmp_path):
    store, dashboard, events = environment(tmp_path / "notes.db")
    dashboard.request({"op": "item_add", "kind": "task", "content": "Написать заметку"})
    row = store.db.execute("SELECT * FROM dashboard_items").fetchone()
    dashboard.request({"op": "item_update", "id": row["id"], "completed": True})
    assert store.db.execute("SELECT completed FROM dashboard_items").fetchone()[0] == 1
    dashboard.request({"op": "item_add", "kind": "fake", "content": "Не сохранить"})
    dashboard.request({"op": "item_delete", "id": True})
    assert (
        store.db.execute(
            "SELECT count(*) FROM dashboard_items WHERE deleted_at IS NULL"
        ).fetchone()[0]
        == 1
    )
    assert events[-1]["error"]
    store.close()
    reopened = MemoryStore(tmp_path / "notes.db")
    assert (
        reopened.db.execute("SELECT content FROM dashboard_items").fetchone()[0]
        == "Написать заметку"
    )
    reopened.close()


@pytest.mark.parametrize(
    "city,zone",
    [
        ("Tallinn", "В общаге"),
        ("Таллинн", "В общаге"),
        ("Sillamäe", "Дома"),
        ("Силламяэ", "Дома"),
        ("Тарту", "Тарту"),
    ],
)
def test_city_labels_are_only_city_aliases(city, zone):
    assert city_zone(city) == zone


def test_location_permission_is_scoped_and_only_coarse_coordinates_are_saved():
    async def run():
        store, dashboard, events = environment()
        dashboard.personal.read.return_value = {
            "location": {"city": "Таллин", "latitude": 59.437123, "longitude": 24.745234}
        }
        dashboard.weather.forecast.return_value = {"now": {"temperature": 5}, "days": []}
        await dashboard.read_location(True)
        dashboard.personal.read.assert_awaited_once_with(
            request_access=True, parts=("location",), city_only=True
        )
        saved = store.preference("dashboard", "location")
        assert saved["latitude"] == 59.4 and saved["longitude"] == 24.7
        dashboard.weather.forecast.assert_awaited_once_with(
            days=3, coordinates={"latitude": 59.4, "longitude": 24.7}
        )
        assert "59.437123" not in json.dumps(events)
        store.close()

    asyncio.run(run())


def test_calendar_is_not_read_without_opt_in_and_failure_does_not_invent_events():
    async def run():
        store, dashboard, events = environment()
        await dashboard.read_calendar("2026-11-02")
        dashboard.personal.read.assert_not_called()
        dashboard.personal.read.return_value = {"calendar": {"error": "calendar_permission"}}
        await dashboard.read_calendar("2026-11-02", True)
        dashboard.personal.read.assert_awaited_once_with(
            request_access=True, parts=("calendar",), day="2026-11-02"
        )
        assert dashboard.calendar["2026-11-02"]["status"] == "error"
        assert not dashboard.calendar["2026-11-02"]["events"]
        assert store.preference("dashboard", "calendar_enabled") is not True
        store.close()

    asyncio.run(run())


def test_memory_edits_preserve_registry_and_revision_checks():
    store, dashboard, events = environment()
    identifier, _ = store.add_fact("Я люблю чай")
    dashboard.request(
        {"op": "memory_update", "id": identifier, "revision": 99, "content": "Другое"}
    )
    assert not any("submitted" in event for event in events)
    revision = store.memories()[0]["revision"]
    dashboard.request({"op": "memory_delete", "id": identifier, "revision": revision})
    assert events[-1]["submitted"]["text"] == f"забудь это факт {identifier}"
    assert store.facts() == [(identifier, "Я люблю чай")]
    dashboard.request({"op": "confirm", "fingerprint": "invented", "code": "1234"})
    assert events[-1]["error"]
    store.close()


def test_stats_use_local_day_boundary_and_missing_audio_is_not_zero(monkeypatch):
    from backend.hud import dashboard as module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 1, 22, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(module, "datetime", Clock)
    store, dashboard, events = environment()
    dashboard.s.location.timezone = "Europe/Tallinn"
    for index, (when, first) in enumerate(
        [("2026-11-01T22:15:00+00:00", 3.0), ("2026-11-01T21:59:00+00:00", 50.0)]
    ):
        trace = {
            "request_id": str(index),
            "created_at": when,
            "status": "completed",
            "first_content_s": first,
            "metadata": {},
            "stages": [],
        }
        store.db.execute(
            "INSERT INTO request_traces VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(index),
                when,
                None,
                "voice",
                "completed",
                first,
                first + 1,
                None,
                json.dumps(trace),
            ),
        )
    dashboard.statistics()
    assert events[-1]["response_s"] == 3.0
    assert events[-1]["stages"]["stt"] is None
    store.close()


def test_text_chat_uses_same_tools_and_trace_without_stt_or_fake_audio():
    from tests.test_voice_loop import Harness

    async def run():
        harness = Harness(text="Must not transcribe", wake=False)
        harness.s.language = "ru-RU"
        store = MemoryStore(":memory:")
        harness.loop.conv = Conversation(
            harness.loop.s,
            SimpleNamespace(),
            store,
            tools=ToolRegistry(clock_tools(harness.loop.s)),
        )
        harness.loop.stt.transcribe = AsyncMock(
            side_effect=AssertionError("no microphone transcription")
        )
        assert harness.loop.submit_text({"text": "Сколько времени?"})
        await harness.loop.task
        await asyncio.sleep(0)
        harness.loop.stt.transcribe.assert_not_awaited()
        trace = json.loads(store.db.execute("SELECT data FROM request_traces").fetchone()[0])
        assert trace["kind"] == "text" and trace["metadata"]["input_mode"] == "text"
        assert not any(stage["stage"] == "stt" for stage in trace["stages"])
        assert trace["status"] == "completed"
        assert len(store.transcript(harness.loop.conv.id)) == 2
        assert (
            next(e for e in harness.loop.bus.snapshot() if e["type"] == "addressee")["state"]
            == "waiting"
        )
        store.close()

    asyncio.run(run())


def test_native_ambient_restores_window_and_never_moves_a_minimized_window(monkeypatch):
    screens = [
        SimpleNamespace(x=0, y=0, width=1440, height=900),
        SimpleNamespace(x=1440, y=0, width=1920, height=1080),
    ]
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(screens=screens))
    moves, toggles = [], []
    api = _Api()
    api.window = SimpleNamespace(
        x=30,
        y=50,
        width=1000,
        height=700,
        move=lambda x, y: moves.append((x, y)),
        toggle_fullscreen=lambda: toggles.append(True),
    )
    assert api.ambient(True, "external", True)["ok"]
    assert moves[-1][0] >= 1440 and len(toggles) == 1
    assert api.ambient(False, "primary", False)["ok"]
    assert moves[-1] == (30, 50) and len(toggles) == 2
    api.minimized = True
    assert not api.ambient(True, "primary", True)["ok"]
    assert len(toggles) == 2


def test_ambient_preferences_reject_unsupported_delay_and_unknown_fields():
    with pytest.raises(ValueError):
        dashboard_settings({"ambient_minutes": 0})
    with pytest.raises(ValueError):
        dashboard_settings({"prevent_sleep": True})
    assert dashboard_settings({"ambient_minutes": 3})["ambient_minutes"] == 3


def test_native_source_failures_keep_last_city_and_never_confirm_empty_calendar():
    from backend.tools.registry import ToolError

    async def run():
        store, dashboard, events = environment()
        dashboard.location = {"city": "Таллин", "latitude": 59.4, "longitude": 24.7}
        dashboard.personal.read.side_effect = ToolError("unavailable")
        await dashboard.read_location()
        assert dashboard.location["status"] == "error"
        assert dashboard.location["city"] == "Таллин"
        dashboard.personal.read.side_effect = None
        dashboard.personal.read.return_value = {}
        await dashboard.read_calendar("2026-11-02", True)
        assert dashboard.calendar["2026-11-02"]["status"] == "error"
        assert store.preference("dashboard", "calendar_enabled") is not True
        assert any(e.get("location", {}).get("status") == "loading" for e in events)
        store.close()

    asyncio.run(run())


def test_disabled_calendar_cannot_reenable_itself_after_inflight_refresh():
    async def run():
        store, dashboard, events = environment()
        waiting = asyncio.Event()

        async def read(**kwargs):
            waiting.set()
            await asyncio.Event().wait()

        dashboard.personal.read.side_effect = read
        dashboard.request({"op": "calendar_connect"})
        await waiting.wait()
        dashboard.request({"op": "calendar_disable"})
        await asyncio.gather(*dashboard.jobs.values(), return_exceptions=True)
        assert not dashboard.calendar
        assert store.preference("dashboard", "calendar_enabled") is False
        store.close()

    asyncio.run(run())


def test_calendar_keeps_occurrences_all_day_and_native_timezone():
    async def run():
        store, dashboard, events = environment()
        event = {
            "id": "series:2026-11-02",
            "title": "Series occurrence",
            "all_day": True,
            "start": "2026-11-02T05:00:00Z",
            "end": "2026-11-03T05:00:00Z",
            "color": "#fa00ae",
        }
        dashboard.personal.read.return_value = {
            "calendar": {"events": [event], "timezone": "America/New_York"}
        }
        await dashboard.read_calendar("2026-11-02", True)
        assert dashboard.timezone == "America/New_York"
        assert dashboard.calendar["2026-11-02"]["events"] == [event]
        dashboard.native_timezone({"timezone": "untrusted/timezone"})
        assert dashboard.timezone == "America/New_York"
        store.close()

    asyncio.run(run())


def test_failed_weather_keeps_last_measurement_and_marks_it_as_unconfirmed():
    async def run():
        store, dashboard, events = environment()
        dashboard.location = {"city": "Таллин", "latitude": 59.4, "longitude": 24.7}
        old = {"place": "Таллин", "now": {"temperature": 7}, "days": []}
        dashboard.weather_state = {"status": "ready", "data": old}
        dashboard.weather.forecast.side_effect = TimeoutError()
        await dashboard.read_weather()
        assert dashboard.weather_state["status"] == "error"
        assert dashboard.weather_state["data"] is old
        assert "устаревшим" in dashboard.weather_state["message"]
        store.close()

    asyncio.run(run())


def test_calendar_open_uses_arguments_and_distinguishes_permission_from_missing_event(monkeypatch):
    from backend.media import safari

    async def run():
        store, dashboard, events = environment()
        title = 'Meeting "quoted" $(private)'
        dashboard.timezone = "Europe/Tallinn"
        dashboard.calendar["2026-11-02"] = {
            "events": [
                {
                    "id": "occurrence",
                    "external_id": "external-uid",
                    "calendar": "Personal",
                    "title": title,
                    "start": "2026-11-01T22:00:00Z",
                }
            ]
        }
        execute = AsyncMock(return_value="opened\n")
        monkeypatch.setattr(safari, "process", execute)
        await dashboard.open_calendar("occurrence")
        args = execute.call_args.args
        assert args[0] == "osascript" and title not in args[2]
        assert title in args and args[-4:] == ("2026", "11", "2", "0")
        assert not any(e["type"] == "notice" for e in events)
        execute.return_value = "not_found\n"
        await dashboard.open_calendar("occurrence")
        assert "не найдено однозначно" in events[-1]["message"]
        execute.return_value = "error:-1743\n"
        await dashboard.open_calendar("occurrence")
        assert "Автоматизация" in events[-1]["message"]
        with pytest.raises(ValueError):
            await dashboard.open_calendar("stale-id")
        store.close()

    asyncio.run(run())
