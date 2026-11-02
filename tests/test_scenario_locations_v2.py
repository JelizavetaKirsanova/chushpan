"""Mandatory General, native city freshness, manual authority and atomic legacy migration."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from backend.automation.locations import LOCATION_TTL_S, Locations, native_observation
from backend.automation.models import Block, Scenario, Variant
from backend.automation.store import ScenarioStore
from backend.hud.bus import HudBus
from backend.hud.dashboard import Dashboard
from backend.memory.store import MemoryStore
from tests.test_automations import rig as rig
from tests.test_dialogue_context import ask


def fix(city="Tallinn", *, age=10, accuracy=100, **changes):
    value = {
        "city": city,
        "latitude": 59.437123,
        "longitude": 24.753456,
        "accuracy_m": accuracy,
        "measured_at": (datetime.now(UTC) - timedelta(seconds=age)).isoformat(),
        "country_code": "EE",
    }
    value.update(changes)
    return value


def observed(rig, value):
    rig.memory.set_preference("dashboard", "location_enabled", True)
    observation = native_observation(value)
    rig.service.locations.provider = lambda: observation
    return observation


def document(rig, **changes):
    fields = dict(
        name="Я спать",
        phrases=["Мой единый сценарий"],
        actions=[Block(capability="mac.brightness", parameters={"level": 10})],
        variants=[
            Variant(
                name="Общага",
                place_id="dorm",
                actions=[Block(capability="mac.brightness", parameters={"level": 15})],
            ),
            Variant(
                name="Дом",
                place_id="home",
                actions=[Block(capability="mac.brightness", parameters={"level": 25})],
            ),
        ],
    )
    fields.update(changes)
    return rig.service.store.save(Scenario(**fields))


@pytest.mark.parametrize(
    "city,expected,level",
    [
        ("Tallinn", "Общага", 15),
        ("Таллинн", "Общага", 15),
        ("Таллин", "Общага", 15),
        ("Sillamäe", "Дом", 25),
        ("Sillamae", "Дом", 25),
        ("Силламяэ", "Дом", 25),
        ("Силламяе", "Дом", 25),
        ("Narva", "Общий", 10),
        ("Тарту", "Общий", 10),
        ("Helsinki", "Общий", 10),
    ],
)
def test_native_city_voice_chooses_exactly_one_version(rig, city, expected, level):
    scenario = document(rig)
    observed(rig, fix(city))
    asyncio.run(ask(rig.conv, "Чушпан, мой единый сценарий"))
    rig.brightness.assert_awaited_once_with(level=level)
    run = rig.service.store.history(scenario.id)[0]
    assert run["variant_name"] == expected and len(run["steps"]) == 1
    assert not rig.conv.llm.requests


@pytest.mark.parametrize("city,remove", [("Tallinn", "dorm"), ("Силламяэ", "home")])
def test_absent_local_version_falls_back_to_general(rig, city, remove):
    scenario = document(rig)
    scenario = rig.service.store.save(
        scenario.model_copy(
            update={
                "variants": [v for v in scenario.variants if v.place_id != remove],
            }
        )
    )
    observed(rig, fix(city))
    asyncio.run(ask(rig.conv, "Мой единый сценарий"))
    rig.brightness.assert_awaited_once_with(level=10)
    assert rig.service.store.history(scenario.id)[0]["variant_name"] == "Общий"


@pytest.mark.parametrize(
    "result",
    [
        {"error": "location_permission"},
        {"error": "location_disabled"},
        {"error": "location_city_unavailable"},
        {"error": "location_unavailable"},
        {},
        lambda: fix(age=LOCATION_TTL_S + 1),
        lambda: fix(age=-20),
        fix(accuracy=3001),
        fix(accuracy=-1),
        fix(accuracy=float("nan")),
        fix(accuracy=float("inf")),
    ],
)
def test_failed_stale_future_or_imprecise_location_always_uses_general(rig, result):
    scenario = document(rig)
    if callable(result):
        result = result()
    observed(rig, result)
    # Avoid real reads: a denied/failed native refresh is also injected, never a device probe.
    rig.service.locations.refresher = AsyncMock()
    asyncio.run(ask(rig.conv, "Мой единый сценарий"))
    rig.brightness.assert_awaited_once_with(level=10)
    assert rig.service.store.history(scenario.id)[0]["variant_name"] == "Общий"


def test_repacked_old_coordinate_is_not_made_fresh_by_cache_write_time(rig):
    value = observed(rig, fix(age=20))
    value["measured_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    value["updated_at"] = datetime.now(UTC).isoformat()
    assert rig.service.locations.current()["place_id"] is None
    # A pre-upgrade macOS cache without the actual measurement time is also not authority.
    value.pop("measured_at")
    assert rig.service.locations.current()["place_id"] is None


def test_permission_opt_out_invalidates_even_a_fresh_cached_city(rig):
    observed(rig, fix())
    assert rig.service.locations.current()["place_id"] == "dorm"
    rig.memory.set_preference("dashboard", "location_enabled", False)
    rig.service.locations.refresher = AsyncMock()
    assert asyncio.run(rig.service.locations.refresh())["place_id"] is None
    rig.service.locations.refresher.assert_not_awaited()


def test_manual_choice_including_general_persists_and_wins_until_auto(rig, tmp_path):
    scenario = document(rig)
    observed(rig, fix())
    rig.service.locations.refresher = AsyncMock()
    rig.service.locations.select("manual", "home")
    asyncio.run(ask(rig.conv, "Мой единый сценарий"))
    rig.brightness.assert_awaited_once_with(level=25)
    assert rig.service.locations.current()["mode"] == "manual"
    rig.service.locations.refresher.assert_not_awaited()
    # A separate Locations instance sees the same persistent preference.
    other = Locations(rig.memory)
    assert other.current()["place_id"] == "home"
    rig.service.locations.select("manual", "general")
    asyncio.run(ask(rig.conv, "Мой единый сценарий"))
    rig.service.locations.select("auto")
    asyncio.run(ask(rig.conv, "Мой единый сценарий"))
    assert [c.kwargs["level"] for c in rig.brightness.await_args_list] == [25, 10, 15]
    assert len(rig.service.store.history(scenario.id)) == 3


def test_unknown_deleted_manual_place_never_silently_reverts_to_auto(rig):
    observed(rig, fix())
    rig.memory.set_preference("automation", "location", {"mode": "manual", "place_id": "missing"})
    result = asyncio.run(rig.service.locations.refresh())
    assert result["place_id"] is None and result["mode"] == "manual"


def test_small_gps_jitter_and_translated_city_names_do_not_switch_local_version(rig):
    value = observed(rig, fix("Sillamäe", age=20))
    assert rig.service.locations.current()["place_id"] == "home"
    value.update(
        native_observation(fix("Силламяэ", age=15, latitude=59.4, longitude=27.8, accuracy=500))
    )
    assert rig.service.locations.current()["place_id"] == "home"
    value.update(
        native_observation(fix("Силламяе", age=10, latitude=59.39, longitude=27.76, accuracy=1000))
    )
    assert rig.service.locations.current()["place_id"] == "home"


def test_nearby_city_change_requires_two_distinct_fresh_fixes_not_two_reads_of_cache(rig):
    value = observed(rig, fix("Narva", age=20))
    assert rig.service.locations.current()["name"] == "Общий"
    value.update(native_observation(fix("Силламяэ", age=15)))
    assert rig.service.locations.current()["status"] == "confirming"
    assert rig.service.locations.current()["place_id"] is None
    assert rig.service.locations.current()["place_id"] is None
    value.update(native_observation(fix("Силламяэ", age=10)))
    assert rig.service.locations.current()["place_id"] == "home"
    # A stale former city does not impose a confirmation requirement on a new valid fix.
    rig.service.locations._stable = ("dorm", (datetime.now(UTC) - timedelta(hours=1)).isoformat())
    assert rig.service.locations.current()["place_id"] == "home"


def test_external_display_wifi_and_bluetooth_state_cannot_determine_city(rig):
    document(rig)
    rig.service.engine.device_state.update(
        tv={"connected": True}, led={"connected": True}, mac={"wifi": "Tallinn"}
    )
    assert rig.service.locations.current()["place_id"] is None
    asyncio.run(ask(rig.conv, "Мой единый сценарий"))
    rig.brightness.assert_awaited_once_with(level=10)


def test_expired_cache_is_refreshed_asynchronously_once_before_execution(rig):
    document(rig)
    value = observed(rig, fix(age=5))
    value["measured_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    refresh_started = asyncio.Event()
    release = asyncio.Event()

    async def native(access):
        assert access is False
        refresh_started.set()
        await release.wait()
        value.clear()
        value.update(native_observation(fix("Sillamäe")))

    rig.service.locations.refresher = AsyncMock(side_effect=native)

    async def run():
        task = asyncio.create_task(ask(rig.conv, "Мой единый сценарий"))
        await asyncio.wait_for(refresh_started.wait(), 2)
        assert not task.done()  # The event loop is available while native resolution waits.
        rig.brightness.assert_not_awaited()
        release.set()
        await task
        rig.brightness.assert_awaited_once_with(level=25)
        await rig.service.locations.refresh()
        rig.service.locations.refresher.assert_awaited_once_with(False)

    asyncio.run(run())


def test_denied_native_read_is_throttled_and_never_prompts_without_consent(rig):
    document(rig)
    observed(rig, {"error": "location_permission"})
    rig.service.locations.personal = SimpleNamespace(
        read=AsyncMock(return_value={"location": {"error": "location_permission"}})
    )
    rig.service.locations.provider = None

    async def run():
        await rig.service.locations.refresh()
        await rig.service.locations.refresh()
        rig.service.locations.personal.read.assert_awaited_once_with(
            request_access=False, parts=("location",), city_only=True
        )

    asyncio.run(run())


def test_dashboard_and_scenario_runner_share_reader_and_do_not_request_weather_for_run(rig):
    personal = SimpleNamespace(read=AsyncMock(return_value={"location": fix("Sillamäe")}))
    weather = SimpleNamespace(forecast=AsyncMock())
    dashboard = Dashboard(HudBus(), rig.s, rig.memory, rig.conv, weather, personal=personal)
    rig.memory.set_preference("dashboard", "location_enabled", True)
    document(rig)
    asyncio.run(ask(rig.conv, "Мой единый сценарий"))
    personal.read.assert_awaited_once_with(
        request_access=False, parts=("location",), city_only=True
    )
    weather.forecast.assert_not_awaited()
    rig.brightness.assert_awaited_once_with(level=25)
    assert dashboard.location["longitude"] == 24.8
    assert dashboard.location["measured_at"]


def test_connect_requires_explicit_consent_and_does_not_clear_manual_mode_on_refresh(rig):
    rig.service.locations.refresher = AsyncMock()
    bus = HudBus()
    rig.service.bus = bus
    queue = bus.subscribe()

    async def run():
        rig.service.locations.select("manual", "home")
        await rig.service.dispatch({"op": "location_connect"})
        rig.service.locations.refresher.assert_not_awaited()
        assert rig.service.locations.current()["mode"] == "manual"
        assert queue.get_nowait().get("error")
        await rig.service.dispatch({"op": "location_refresh"})
        rig.service.locations.refresher.assert_not_awaited()
        await rig.service.dispatch({"op": "location_connect", "consent": True})
        rig.service.locations.refresher.assert_awaited_once_with(True)
        assert rig.service.locations.current()["mode"] == "auto"

    asyncio.run(run())


@pytest.mark.parametrize(
    "changes",
    [
        {"general_enabled": False},
        {"actions": None},
        {"version_policy": "append"},
        {"schema_version": 3},
    ],
)
def test_general_is_mandatory_and_the_api_cannot_reenable_append_mode(changes):
    with pytest.raises(ValidationError):
        Scenario(name="Обязательная общая", **changes)
    assert Scenario(name="Пустой новый").actions == []


def legacy_row(memory, *, policy="append", general=True, count=1):
    raw = Scenario(
        id="a" * 32,
        revision=1,
        name="Старый сценарий",
        icon="☾",
        phrases=["Моя старая фраза"],
        actions=[
            Block(capability="mac.brightness", parameters={"level": 10}) for _ in range(count)
        ],
        variants=[
            Variant(
                name="Дом",
                place_id="home",
                actions=[Block(capability="mac.brightness", parameters={"level": 25})],
            )
        ],
    ).model_dump()
    raw.pop("schema_version")
    raw.update(version_policy=policy, general_enabled=general)
    serialized = json.dumps(raw, ensure_ascii=False)
    memory.db.execute(
        "INSERT INTO scenarios VALUES (?,?,?,?)",
        (raw["id"], 1, serialized, datetime.now(UTC).isoformat()),
    )
    memory.db.execute("INSERT INTO scenario_phrases VALUES (?,?)", ("моя старая фраза", raw["id"]))
    memory.db.commit()
    return raw, serialized


@pytest.mark.parametrize(
    "policy,general,expected",
    [("append", True, [10, 25]), ("fallback", True, [25]), ("fallback", False, [25])],
)
def test_migration_preserves_effects_in_independent_versions_and_keeps_exact_backup(
    tmp_path, policy, general, expected
):
    path = tmp_path / "legacy.sqlite"
    memory = MemoryStore(path)
    ScenarioStore(memory)
    raw, serialized = legacy_row(memory, policy=policy, general=general)
    store = ScenarioStore(memory)
    scenario = store.get(raw["id"])
    assert scenario.schema_version == 2 and scenario.general_enabled is True
    assert scenario.revision == 2
    assert [b.parameters["level"] for b in scenario.variants[0].actions] == expected
    assert [b.parameters["level"] for b in scenario.actions] == (
        [10] if general or policy == "append" else []
    )
    assert (
        memory.db.execute("SELECT data FROM scenario_migration_backups").fetchone()[0] == serialized
    )
    assert (
        memory.db.execute("SELECT data FROM scenario_revisions WHERE revision=1").fetchone()[0]
        == serialized
    )
    scenario.variants[0].actions[0].parameters["level"] = 33
    assert not scenario.actions or scenario.actions[0].parameters["level"] == 10
    restored = store.restore(scenario.id, 1, 2)
    assert [b.parameters["level"] for b in restored.variants[0].actions] == expected
    memory.close()
    reopened = MemoryStore(path)
    try:
        saved = ScenarioStore(reopened).get(raw["id"])
        assert saved.revision == 3  # Reopening never migrates or copies a prefix twice.
        assert [b.parameters["level"] for b in saved.variants[0].actions] == expected
        assert (
            reopened.db.execute("SELECT COUNT(*) FROM scenario_migration_backups").fetchone()[0]
            == 1
        )
    finally:
        reopened.close()


def test_migration_preserves_large_old_common_prefix_and_rolls_back_on_failure(
    tmp_path, monkeypatch
):
    memory = MemoryStore(tmp_path / "migration.sqlite")
    ScenarioStore(memory)
    raw, original = legacy_row(memory, count=300)
    original_save = ScenarioStore.save

    def fail_after_save(self, scenario):
        original_save(self, scenario)
        raise RuntimeError("Interrupted migration")

    monkeypatch.setattr(ScenarioStore, "save", fail_after_save)
    with pytest.raises(RuntimeError, match="Interrupted"):
        ScenarioStore(memory)
    assert memory.db.execute("SELECT data FROM scenarios").fetchone()[0] == original
    assert memory.db.execute("SELECT COUNT(*) FROM scenario_migration_backups").fetchone()[0] == 0
    monkeypatch.setattr(ScenarioStore, "save", original_save)
    scenario = ScenarioStore(memory).get(raw["id"])
    assert len(scenario.actions) == 300 and len(scenario.variants[0].actions) == 301
    memory.close()


def test_migrated_legacy_document_runs_only_one_materialized_list(rig):
    raw, _ = legacy_row(rig.memory)
    ScenarioStore(rig.memory)
    rig.service.locations.select("manual", "home")
    asyncio.run(ask(rig.conv, "Моя старая фраза"))
    assert [c.kwargs["level"] for c in rig.brightness.await_args_list] == [10, 25]
    assert len(rig.service.store.history(raw["id"])[0]["steps"]) == 2


@pytest.mark.parametrize("source", [None, "wifi", "bluetooth", "display", "manual", "ip"])
def test_only_the_macos_location_source_can_select_an_automatic_local_version(rig, source):
    value = observed(rig, fix())
    value["source"] = source
    assert rig.service.locations.current()["place_id"] is None


@pytest.mark.parametrize("city,level", [("Tallinn", 15), ("Силламяэ", 25), ("Нарва", 10)])
def test_literal_sleep_phrase_uses_scenario_before_builtin_night_routine(rig, city, level):
    document(rig, phrases=["Я спать"])
    observed(rig, fix(city))
    asyncio.run(ask(rig.conv, "Чушпан, я спать"))
    rig.brightness.assert_awaited_once_with(level=level)
    rig.power.assert_not_awaited()
    assert rig.conv.last_stats.tools == ["run_scenario"]


def test_failed_forced_refresh_invalidates_a_previously_fresh_cached_fix(rig):
    from backend.tools.registry import ToolError

    rig.memory.set_preference("dashboard", "location_enabled", True)
    rig.memory.set_preference("dashboard", "location", native_observation(fix()))
    rig.service.locations.personal = SimpleNamespace(
        read=AsyncMock(side_effect=ToolError("No position"))
    )
    assert rig.service.locations.current()["place_id"] == "dorm"
    result = asyncio.run(rig.service.locations.refresh(force=True))
    assert result["place_id"] is None and result["status"] == "unknown"


@pytest.mark.parametrize("manual", [True, False])
def test_run_api_cannot_override_current_place_by_passing_another_tab(rig, manual):
    scenario = document(rig)
    if manual:
        rig.service.locations.select("manual", "home")
        wrong = scenario.variants[0].id
    else:
        observed(rig, fix("Tallinn"))
        wrong = scenario.variants[1].id
    bus = HudBus()
    rig.service.bus, rig.service.conv = bus, rig.conv
    queue = bus.subscribe()
    asyncio.run(
        rig.service.dispatch(
            {"op": "run", "id": scenario.id, "revision": scenario.revision, "variant": wrong}
        )
    )
    rig.brightness.assert_not_awaited()
    assert not rig.service.store.history(scenario.id)
    event = queue.get_nowait()
    assert "по текущему месту" in event["result"]["error"]
