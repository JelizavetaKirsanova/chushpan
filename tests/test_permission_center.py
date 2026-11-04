"""Public API adapters are mocked; no TCC changes, recording, devices or browser."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.automation.locations import Locations
from backend.automation.models import GeoZone, WiFiBinding
from backend.config import Settings
from backend.context_signals import signal
from backend.memory.store import MemoryStore
from backend.permission_center import PermissionCenter
from backend.permission_policy import requests_allowed


@pytest.fixture
def center(monkeypatch):
    import backend.permission_center as module

    settings = Settings()
    settings.led.enabled = True
    c = PermissionCenter(settings, MemoryStore(":memory:"))
    c.music.permission = AsyncMock(
        return_value={
            "automation": "not_determined",
            "executable": "/fake/native",
            "osstatus": -1744,
        }
    )
    c.personal.permissions = AsyncMock(
        return_value={
            "calendar": "granted",
            "location": "not_determined",
            "location_services": True,
            "identity": {"bundle_id": "local.chushpan.personal"},
        }
    )
    c.desktop.request = AsyncMock(
        return_value={"accessibility": "check_needed", "screen_recording": "check_needed"}
    )
    c.video.request = AsyncMock(
        return_value={"automation": "not_determined", "accessibility": "not_granted"}
    )
    monkeypatch.setattr(
        module,
        "signing_identity",
        AsyncMock(return_value={"verified": True, "CDHash": "fake-build"}),
    )
    monkeypatch.setattr(
        module.platform,
        "permission_status",
        lambda: {"microphone": "not_determined", "bluetooth": "denied", "login": "check_needed"},
    )
    monkeypatch.setattr(
        module.platform, "open_privacy", lambda key: {"opened": True, "message": "Открой " + key}
    )
    return c


def test_diagnostics_only_preflights_and_exact_executor_identities(center):
    report = asyncio.run(center.report())
    by_id = {i["id"]: i for i in report["items"]}
    assert by_id["calendar"]["status"] == "granted"
    assert by_id["location"]["status"] == "not_determined"
    assert by_id["automation_safari"]["status"] == "not_determined"
    assert by_id["calendar"]["details"]["identity"]["bundle_id"] == "local.chushpan.personal"
    assert all(not c.kwargs.get("request") for c in center.music.permission.call_args_list)
    center.personal.permissions.assert_awaited_once_with()
    center.desktop.request.assert_awaited_once_with("permission_status")
    assert not requests_allowed.get()


@pytest.mark.parametrize(
    "key",
    [
        "speech",
        "camera",
        "contacts",
        "reminders",
        "photos",
        "full_disk",
        "system_audio",
        "input_monitoring",
        "audio_devices",
        "automation_finder",
        "automation_music",
    ],
)
def test_unimplemented_capabilities_never_have_fake_request_buttons(center, key):
    with pytest.raises(ValueError, match="нет отдельного запроса"):
        asyncio.run(center.report("request", key))
    center.music.permission.assert_not_awaited()
    center.personal.permissions.assert_not_awaited()


@pytest.mark.parametrize("decision", ["granted", "denied", "unknown"])
def test_request_is_followed_by_fresh_status_and_no_success_guess(center, decision):
    state = "not_determined"

    async def permission(**kw):
        nonlocal state
        if kw.get("request"):
            state = decision
        return {"automation": state}

    center.music.permission = AsyncMock(side_effect=permission)
    report = asyncio.run(center.report("request", "automation_safari"))
    item = next(i for i in report["items"] if i["id"] == "automation_safari")
    assert item["status"] == (decision if decision != "unknown" else "check_needed")
    requests = [c for c in center.music.permission.call_args_list if c.kwargs.get("request")]
    assert len(requests) == 1
    assert requests[0].kwargs["target"] == "com.apple.Safari"
    if decision == "denied":
        assert "automation" in report["message"]


def test_previously_denied_opens_settings_without_reprompt(center):
    center.music.permission.return_value = {"automation": "denied"}
    result = asyncio.run(center.report("request", "automation_safari"))
    assert "automation" in result["message"]
    assert all(not c.kwargs.get("request") for c in center.music.permission.call_args_list)


def test_revoked_grants_and_api_failure_never_reuse_old_authority(center):
    center.personal.permissions.return_value = {"location": "granted", "calendar": "granted"}
    asyncio.run(center.report())
    center.personal.permissions.side_effect = RuntimeError("private contents must not be journaled")
    result = asyncio.run(center.report())
    assert next(i for i in result["items"] if i["id"] == "calendar")["status"] == "check_needed"
    assert "private contents" not in json.dumps(result)
    assert len(result["history"]) == 40


def test_location_grant_does_not_hide_coordinate_failure(center):
    center.store.set_preference(
        "dashboard",
        "location",
        {
            "status": "error",
            "error_code": "location_timeout",
            "native_code": 0,
            "latitude": 59.43712,
        },
    )
    result = asyncio.run(center.report())
    row = next(i for i in result["items"] if i["id"] == "location")
    assert "Core Location" in row["last_error"]
    assert row["details"]["last_location_error"]["error_code"] == "location_timeout"
    assert "59.43712" not in json.dumps(result)


def places():
    memory = MemoryStore(":memory:")
    locations = Locations(memory)
    for key in ("home", "dorm"):
        place = locations.get(key)
        locations.save(place.model_copy(update={"wifi_networks": [WiFiBinding(ssid=key)]}))
    return locations


def observe_wifi(locations, ssid, bssid=None):
    locations.signal_observation = {
        "source": "macos",
        "updated_at": datetime.now(UTC).isoformat(),
        "wifi": {
            "status": "ready",
            "ssid_key": signal("ssid", ssid, "")["key"],
            "bssid_key": signal("bssid", bssid, "")["key"] if bssid else None,
        },
    }


def test_manual_wifi_and_general_priority_and_missing_ssid():
    loc = places()
    observe_wifi(loc, "dorm")
    assert loc.current()["place_id"] == "dorm"
    loc.select("manual", "home")
    assert loc.current()["place_id"] == "home"
    loc.select("auto")
    observe_wifi(loc, "unregistered")
    assert loc.current()["place_id"] is None
    loc.signal_observation = {}
    assert loc.current()["place_id"] is None


def test_duplicate_ssid_fails_closed_and_bssid_disambiguates():
    loc = places()
    for key in ("home", "dorm"):
        p = loc.get(key)
        loc.save(p.model_copy(update={"wifi_networks": [WiFiBinding(ssid="same")]}))
    observe_wifi(loc, "same")
    assert loc.current()["reason"] == "wifi_conflict"
    for key, bssid in [("home", "aa:bb:cc:dd:ee:01"), ("dorm", "aa:bb:cc:dd:ee:02")]:
        p = loc.get(key)
        loc.save(p.model_copy(update={"wifi_networks": [WiFiBinding(ssid="same", bssid=bssid)]}))
    observe_wifi(loc, "same", "aa:bb:cc:dd:ee:02")
    assert loc.current()["place_id"] == "dorm"
    observe_wifi(loc, "same")
    assert loc.current()["place_id"] is None


def test_geo_zone_only_with_fresh_authorized_observation_and_wifi_wins():
    loc = places()
    place = loc.get("home")
    loc.save(
        place.model_copy(update={"zones": [GeoZone(latitude=59.4, longitude=24.8, radius_km=20)]})
    )
    loc.memory.set_preference("dashboard", "location_enabled", True)
    loc.provider = lambda: {
        "source": "macos",
        "status": "ready",
        "city": "test",
        "latitude": 59.4,
        "longitude": 24.8,
        "accuracy_m": 100,
        "measured_at": datetime.now(UTC).isoformat(),
    }
    assert loc.current()["place_id"] == "home"
    observe_wifi(loc, "dorm")
    assert loc.current()["place_id"] == "dorm"


@pytest.mark.parametrize("value", ["а" * 17, "line\nfeed", ""])
def test_wifi_rejects_invalid_ssid(value):
    with pytest.raises(ValueError):
        WiFiBinding(ssid=value)


def test_exact_settings_url_and_fallback_use_nsworkspace(monkeypatch):
    import sys

    from backend.desktop.platform import open_privacy

    opened = []

    def open_url(url):
        opened.append(url)
        return len(opened) == 2

    workspace = SimpleNamespace(openURL_=open_url)
    monkeypatch.setitem(
        sys.modules,
        "AppKit",
        SimpleNamespace(NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace)),
    )
    monkeypatch.setitem(
        sys.modules,
        "Foundation",
        SimpleNamespace(NSURL=SimpleNamespace(URLWithString_=lambda value: value)),
    )
    result = open_privacy("location")
    assert opened[0].endswith("?Privacy_LocationServices")
    assert result["fallback"] and result["opened"]
    assert "Службы геолокации" in result["message"]


def test_automatic_safari_consent_resumes_one_unsubmitted_operation():
    from backend.media.music_safari import SafariMusicTab

    async def run():
        tab = SafariMusicTab(Settings())
        tab._native = AsyncMock(
            side_effect=[
                {"automation": "not_determined"},
                {"automation": "granted"},
                {"result": "done"},
            ]
        )
        token = requests_allowed.set(True)
        try:
            await tab._ready()
            assert await tab._run("fixed server operation") == "done"
        finally:
            requests_allowed.reset(token)
        calls = [c.args[0] for c in tab._native.call_args_list]
        assert [c["op"] for c in calls] == ["probe", "request", "script"]
        assert calls[1]["request_access"] is True
        assert calls[2]["request_access"] is False

    asyncio.run(run())


def test_diagnostics_suppress_consent_even_inside_user_tool():
    from backend.media.music_safari import SafariMusicTab

    async def run():
        tab = SafariMusicTab(Settings())
        tab._native = AsyncMock(return_value={"automation": "not_determined"})
        token = requests_allowed.set(True)
        try:
            result = await tab.diagnose()
            assert result["error_code"] == "automation_unknown"
            assert requests_allowed.get()
        finally:
            requests_allowed.reset(token)
        assert all(c.args[0]["op"] == "probe" for c in tab._native.call_args_list)

    asyncio.run(run())


def test_personal_manual_grant_resumes_only_failed_calendar_read(monkeypatch):
    from backend.desktop import platform
    from backend.personal import PersonalContext

    monkeypatch.setattr(platform, "open_privacy", lambda key: {"opened": True})
    personal = PersonalContext(Settings())
    personal._read = AsyncMock(
        side_effect=[
            {"calendar": {"error": "calendar_permission"}, "location": {"city": "test"}},
            {"calendar": {"events": []}},
        ]
    )
    personal.permissions = AsyncMock(side_effect=[{"calendar": "denied"}, {"calendar": "granted"}])
    result = asyncio.run(personal.read())
    assert result == {"calendar": {"events": []}, "location": {"city": "test"}}
    assert personal._read.call_args_list[-1].kwargs["parts"] == ("calendar",)
    assert personal._read.call_args_list[-1].kwargs["request_access"] is False


@pytest.mark.parametrize("confirmed", [False, True])
def test_microphone_callback_alone_is_not_a_grant(monkeypatch, confirmed):
    import sys

    from backend.desktop.platform import request_microphone

    state = [0]

    def request(media, callback):
        state[0] = 3 if confirmed else 0
        callback(True)

    capture = SimpleNamespace(
        authorizationStatusForMediaType_=lambda media: state[0],
        requestAccessForMediaType_completionHandler_=request,
    )
    monkeypatch.setitem(
        sys.modules,
        "AVFoundation",
        SimpleNamespace(AVCaptureDevice=capture, AVMediaTypeAudio="mock"),
    )
    assert request_microphone() is confirmed


@pytest.mark.parametrize(
    "parts,permission,direct",
    [
        (("calendar",), None, True),
        (("location",), None, False),
        (("permissions",), "calendar", True),
        (("permissions",), "location", False),
    ],
)
def test_calendar_keeps_existing_identity_and_location_uses_launchservices(
    tmp_path, monkeypatch, parts, permission, direct
):
    from pathlib import Path

    import backend.paths as paths
    import backend.personal as module

    monkeypatch.setattr(paths, "DESKTOP", True)
    personal = module.PersonalContext(Settings())
    personal.helper = AsyncMock(return_value=tmp_path / "Helper.app")

    async def execute(*args, **kwargs):
        Path(args[-1]).write_text('{"permissions": {}}')
        return ""

    process = AsyncMock(side_effect=execute)
    monkeypatch.setattr(module, "process", process)
    asyncio.run(personal._read(parts=parts, permission=permission, request_access=False))
    command = process.call_args.args
    assert (command[0] != "open") is direct
    if direct:
        assert command[0].endswith("Contents/MacOS/chushpan-personal")
    else:
        assert command[:4] == ("open", "-W", "-n", "-g")


def test_current_geo_profile_wins_over_old_bluetooth_binding():
    loc = places()
    home = loc.get("home")
    binding = signal("device", "test-device", "Test")
    loc.save(home.model_copy(update={"signals": [binding]}))
    loc.signal_observation = {
        "source": "macos",
        "updated_at": datetime.now(UTC).isoformat(),
        "signals": [binding],
    }
    loc.memory.set_preference("dashboard", "location_enabled", True)
    loc.provider = lambda: {
        "source": "macos",
        "status": "ready",
        "city": "Tallinn",
        "latitude": 59.4,
        "longitude": 24.8,
        "accuracy_m": 100,
        "measured_at": datetime.now(UTC).isoformat(),
    }
    assert loc.current()["place_id"] == "dorm"


def test_background_voice_requires_both_running_loop_and_microphone_grant(center, monkeypatch):
    import backend.permission_center as module

    center.voice_state = lambda: True
    first = asyncio.run(center.report())
    assert (
        next(i for i in first["items"] if i["id"] == "background_voice")["status"] == "check_needed"
    )
    monkeypatch.setattr(module.platform, "permission_status", lambda: {"microphone": "granted"})
    second = asyncio.run(center.report())
    assert next(i for i in second["items"] if i["id"] == "background_voice")["status"] == "granted"
    center.voice_state = lambda: False
    third = asyncio.run(center.report())
    assert (
        next(i for i in third["items"] if i["id"] == "background_voice")["status"] == "check_needed"
    )


@pytest.mark.parametrize("decision", ["write_only", "granted"])
def test_calendar_write_only_requests_full_read_and_rechecks(center, decision):
    state = "write_only"

    async def permission(request=None):
        nonlocal state
        if request == "calendar":
            state = decision
        return {"calendar": state, "location": "not_determined"}

    center.personal.permissions = AsyncMock(side_effect=permission)
    result = asyncio.run(center.report("request", "calendar"))
    calendar = next(i for i in result["items"] if i["id"] == "calendar")
    assert calendar["status"] == ("granted" if decision == "granted" else "check_needed")
    assert (
        sum(
            c.kwargs.get("request") == "calendar"
            for c in center.personal.permissions.call_args_list
        )
        == 1
    )
    if decision == "write_only":
        assert "добавление" in calendar["last_error"]
        assert "calendar" in result["message"]
    else:
        assert calendar["last_error"] is None


@pytest.mark.parametrize("decision", ["granted", "unknown"])
def test_morning_direct_route_requests_consent_once_and_keeps_order(decision):
    from backend.brain.morning import MorningRoutine
    from backend.media.music_safari import SafariMusicTab

    async def run():
        events = []
        settings = Settings()
        settings.media.youtube_music.transport = "safari"
        tab = SafariMusicTab(settings)

        async def native(request):
            events.append(request["op"])
            if request["op"] == "probe":
                return {"automation": "not_determined"}
            return {"automation": decision}

        tab._native = AsyncMock(side_effect=native)
        if decision == "unknown":
            from unittest.mock import patch

            context = patch(
                "backend.permission_policy.wait_for_grant",
                AsyncMock(return_value={"automation": "unknown"}),
            )
            opener = patch("backend.desktop.platform.open_privacy", return_value={"opened": True})
        else:
            from contextlib import nullcontext

            context = opener = nullcontext()

        async def launch():
            assert requests_allowed.get()
            await tab._ready()

        async def volume(**kwargs):
            events.append("volume")
            return {"ok": True}

        async def play(**kwargs):
            events.append("play")
            return {"ok": True}

        media = SimpleNamespace(
            youtube=SimpleNamespace(app=SimpleNamespace(launch=launch)),
            media_volume=volume,
            play_music=play,
        )
        morning = MorningRoutine(settings, None, None, media)
        with context, opener:
            result = await morning.start_music()
        assert not requests_allowed.get()
        assert events.count("request") == 1
        if decision == "granted":
            assert result is None
            assert events == ["probe", "request", "volume", "play"]
        else:
            assert "Музыку включить не удалось" in result
            assert "play" not in events

    asyncio.run(run())
