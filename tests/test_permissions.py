import asyncio
import json

import pytest

from backend import permissions
from backend.config import ROOT_DIR


def test_diagnostics_are_read_only_and_parse_distinct_states(monkeypatch):
    calls = []
    report = {
        "accessibility": "not_granted",
        "screen_recording": "granted",
        "microphone": "denied",
        "automation": {"com.spotify.client": "target_not_running"},
    }

    async def fake_run(*args, **kwargs):
        calls.append(args)
        return "" if args[0] == "xcrun" else json.dumps(report)

    monkeypatch.setattr(permissions, "_run", fake_run)
    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    from unittest.mock import AsyncMock

    monkeypatch.setattr(permissions, "launch_application", AsyncMock(return_value="Чушпан.app"))
    monkeypatch.setattr(permissions, "process_chain", AsyncMock(return_value={"processes": []}))
    result = asyncio.run(permissions.diagnose(("com.spotify.client",)))
    assert {key: result[key] for key in report} == report
    assert result["launch_application"] == "Чушпан.app"
    assert result["probe_identity"]["scope"] == "current_process_chain_not_other_app_tcc"
    assert len(calls) == 2 and calls[0][:2] == ("xcrun", "swiftc")
    assert calls[1][-1] == "com.spotify.client"
    assert "requestAccess" not in (ROOT_DIR / "backend/permissions.swift").read_text()
    assert "typeWildCard), false" in (ROOT_DIR / "backend/permissions.swift").read_text()


def test_unavailable_diagnostics_are_not_success(monkeypatch):
    async def unavailable(*args, **kwargs):
        raise OSError("no compiler")

    monkeypatch.setattr(permissions, "_run", unavailable)
    assert "error" in asyncio.run(permissions.diagnose())
    assert "error" in asyncio.run(permissions.diagnose(("arbitrary.target",)))


def test_each_denied_permission_has_specific_russian_instruction():
    for key, label in permissions.PERMISSION_NAMES.items():
        message = permissions.instruction(key)
        assert label in message and "приложению запуска" in message
        assert "Системные настройки" in message


def test_process_chain_never_reads_arguments_or_environment(monkeypatch):
    calls = []

    async def run(*args):
        calls.append(args)
        if args[1] == "-axo":
            return "30 20 /opt/homebrew/Python"
        return {
            "20": "20 10 /opt/homebrew/Python",
            "10": "10 0 /Applications/Чушпан.app/Contents/MacOS/Чушпан",
        }[args[2]]

    monkeypatch.setattr(permissions, "_run", run)
    result = asyncio.run(permissions.process_chain(20))
    assert [p["pid"] for p in result["processes"]] == [20, 10]
    assert all(call[-1] == "pid=,ppid=,comm=" for call in calls)
    assert result["tcc_responsible_app"] == "not_inferred"
    assert result["children"][0]["pid"] == 30


def test_launch_identity_does_not_stop_at_python_framework(monkeypatch):
    rows = iter(
        [
            "10 /Library/Python.app/Contents/MacOS/Python",
            "1 /Applications/Чушпан.app/Contents/MacOS/Чушпан",
        ]
    )

    async def run(*args):
        return next(rows)

    monkeypatch.setattr(permissions, "_run", run)
    assert asyncio.run(permissions.launch_application()).startswith("Чушпан.app")


def test_safari_executor_status_is_not_replaced_by_generic_granted(monkeypatch):
    from unittest.mock import AsyncMock

    from backend.config import Settings
    from backend.media.youtube_playback import SafariPlayback

    generic = {
        "accessibility": "granted",
        "automation": {"com.apple.Safari": "granted"},
        "safari_javascript": "granted",
    }

    async def run(*args, **kwargs):
        return "" if args[0] == "xcrun" else json.dumps(generic)

    probe = AsyncMock(
        return_value={
            "pid": 42,
            "ppid": 20,
            "executable": "/helper",
            "accessibility": "granted",
            "automation": "not_determined",
            "safari_javascript": "not_checked",
        }
    )
    monkeypatch.setattr(permissions, "_run", run)
    monkeypatch.setattr(permissions, "launch_application", AsyncMock(return_value="Чушпан.app"))
    monkeypatch.setattr(permissions, "process_chain", AsyncMock(return_value={"processes": []}))
    monkeypatch.setattr(SafariPlayback, "request", probe)
    result = asyncio.run(permissions.diagnose(("com.apple.Safari",), settings=Settings()))
    probe.assert_awaited_once_with("youtube_permissions")
    assert result["automation"]["com.apple.Safari"] == "not_determined"
    assert result["safari_javascript"] == "not_checked"
    assert result["safari_executor"]["pid"] == 42


def test_diagnostic_cli_reports_denial_without_success_claim(monkeypatch, capsys):
    async def report(*args):
        return {
            "accessibility": "not_granted",
            "screen_recording": "not_granted",
            "microphone": "denied",
            "automation": {},
        }

    monkeypatch.setattr(permissions, "diagnose", report)
    assert asyncio.run(permissions.run()) == 0
    output = capsys.readouterr().out
    assert "denied" in output and "Универсальный доступ" in output
    assert "Всё готово" not in output and "не запрашиваются" in output


@pytest.mark.parametrize("required", [False, True])
def test_doctor_does_not_claim_readiness_when_a_check_failed(monkeypatch, capsys, required):
    from backend import doctor
    from backend.config import Settings

    monkeypatch.setattr(doctor, "get_settings", Settings)
    for name in (
        "platform",
        "python",
        "language",
        "owner",
        "location",
        "voice",
        "hud",
        "spotify",
        "autostart",
    ):
        monkeypatch.setattr(doctor, f"check_{name}", lambda *args: doctor.Check("База", True, "ok"))
    for name in ("ollama", "speech_models", "audio"):
        monkeypatch.setattr(doctor, f"check_{name}", lambda *args: [])
    monkeypatch.setattr(
        doctor,
        "check_permissions",
        lambda *args: [doctor.Check("Микрофон", False, "denied", required=required)],
    )
    assert doctor.run() == int(required)
    output = capsys.readouterr().out
    assert "Все выполненные проверки пройдены" not in output
    assert "Всё готово" not in output
    assert ("требуют настройки" in output) == (not required)
