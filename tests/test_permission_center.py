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
    assert "location_timeout" in row["last_error"]
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
