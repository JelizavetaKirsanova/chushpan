import asyncio
from unittest.mock import AsyncMock

import pytest

from backend import night_preflight
from backend.night_preflight import NightPreflight, permissions_requested
from backend.tools.registry import ToolError
from tests.test_night import routine


@pytest.fixture
def checker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        night_preflight.permissions, "launch_application", AsyncMock(return_value="Terminal.app")
    )
    service = routine(tmp_path)
    check = NightPreflight(service.s, service.display, service.shortcuts)
    monkeypatch.setattr(
        night_preflight.permissions,
        "diagnose",
        AsyncMock(
            return_value={
                "accessibility": "granted",
                "automation": {"com.apple.Safari": "granted"},
                "safari_javascript": "granted",
                "launch_application": "Terminal.app",
            }
        ),
    )
    monkeypatch.setattr(
        night_preflight,
        "system_status",
        AsyncMock(
            return_value={
                "sound": {"volume_percent": 50},
            }
        ),
    )
    monkeypatch.setattr(night_preflight.Path, "is_file", lambda p: True)
    service.s.night.shortcut_permissions_reviewed = True
    return service, check


@pytest.mark.parametrize("state", ["not_granted", "denied", "unknown", "not_determined"])
def test_denied_ax_prevents_all_effects(checker, state):
    service, check = checker
    night_preflight.permissions.diagnose.return_value["accessibility"] = state
    service.preflight = check
    result = asyncio.run(service.run())
    assert not result["ok"] and not result["shortcut_started"]
    assert "Универсальный доступ" in result["message"]
    assert "Terminal.app" in result["message"]
    assert "Системные настройки" in result["message"]
    service.display.set.assert_not_awaited()
    service.display.inventory.assert_not_awaited()
    service.volume.assert_not_awaited()
    service.shortcuts.start_sleep.assert_not_awaited()


@pytest.mark.parametrize("target", ["automation", "safari_javascript"])
def test_safari_unknown_is_not_success(checker, target):
    service, check = checker
    result = night_preflight.permissions.diagnose.return_value
    result[target] = (
        {"com.apple.Safari": "target_not_running"} if target == "automation" else "no_tab"
    )
    assert not asyncio.run(check.check())["ok"]
    service.shortcuts.start_sleep.assert_not_awaited()


def test_shortcuts_permissions_are_not_inferred_from_host(checker):
    service, check = checker
    service.s.night.shortcut_permissions_reviewed = False
    report = asyncio.run(check.check())
    assert not report["ok"] and "Команды (Shortcuts)" in report["message"]
    assert "автоматически не проверены" in report["message"]


def test_read_only_check_checks_inventory_and_volume(checker):
    service, check = checker
    report = asyncio.run(check.check())
    assert report["ok"]
    service.display.set.assert_not_awaited()
    service.volume.assert_not_awaited()
    service.shortcuts.start_sleep.assert_not_awaited()
    service.shortcuts.verify_sleep.assert_awaited_once()


@pytest.mark.parametrize("component", ["display", "shortcut", "volume", "diagnostic"])
def test_component_failures_block(checker, component):
    service, check = checker
    if component == "display":
        service.display.read.side_effect = ToolError("unavailable")
    elif component == "shortcut":
        service.shortcuts.verify_sleep.side_effect = ToolError("missing UUID")
    elif component == "volume":
        night_preflight.system_status.return_value = {}
    else:
        night_preflight.permissions.diagnose.return_value = {"error": "no compiler"}
    service.preflight = check
    result = asyncio.run(service.run())
    assert not result["ok"] and not result["shortcut_started"]
    service.display.set.assert_not_awaited()
    service.volume.assert_not_awaited()
    service.shortcuts.start_sleep.assert_not_awaited()


def test_permission_intent():
    assert permissions_requested("Чушпан, проверь разрешения")
    assert permissions_requested("Чушпан. Проверь разрешения ночного режима.")
    assert not permissions_requested("Не проверь разрешения")
    assert not permissions_requested("Что означает «проверь разрешения»?")


@pytest.mark.parametrize("failure", [OSError, TimeoutError])
@pytest.mark.parametrize("component", ["display", "shortcut", "volume"])
def test_unavailable_component_reports_failure_without_side_effects(checker, failure, component):
    service, check = checker
    if component == "display":
        service.display.read.side_effect = failure("private diagnostic detail")
    elif component == "shortcut":
        service.shortcuts.verify_sleep.side_effect = failure("private diagnostic detail")
    else:
        night_preflight.system_status.side_effect = failure("private diagnostic detail")
    service.preflight = check
    result = asyncio.run(service.run())
    assert not result["ok"] and not result["shortcut_started"]
    assert "private diagnostic detail" not in result["message"]
    service.display.set.assert_not_awaited()
    service.volume.assert_not_awaited()
    service.shortcuts.start_sleep.assert_not_awaited()


def test_manual_review_is_explicit_blocker_even_with_all_tcc_grants(checker):
    service, check = checker
    service.s.night.shortcut_permissions_reviewed = False
    report = asyncio.run(check.check())
    assert [(c["component"], c["status"]) for c in report["blocking_checks"]] == [
        ("Shortcuts permissions", "unknown")
    ]
    assert "Блокирующие проверки: Shortcuts permissions=unknown" in report["message"]


def test_equivalent_display_uuid_case_does_not_block(checker):
    service, check = checker
    uid = "aabbccdd-0000-0000-0000-000000000002"
    service.s.night.external_uuid = uid
    service.s.youtube_video.night_autoplay = True
    service.s.youtube_video.preferred_screen = "external"
    service.s.youtube_video.external_uuid = uid.upper()
    service.display.inventory.return_value[1]["uuid"] = uid
    assert asyncio.run(check.check())["ok"]


def test_actual_executor_failure_cannot_be_replaced_by_generic_grants(checker):
    service, check = checker
    night_preflight.permissions.diagnose.return_value["safari_executor"] = {"status": "unavailable"}
    report = asyncio.run(check.check())
    assert any(c["component"] == "Safari executor" for c in report["blocking_checks"])
