import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from backend import mac_check
from backend.config import Settings


@pytest.mark.parametrize("music_running", [False, True])
def test_diagnostic_marks_read_checks_and_does_not_claim_action_tests(monkeypatch, music_running):
    class Desktop:
        async def apps(self, scope="running"):
            return {"apps": []}

        async def list_windows(self):
            return {"windows": [{"title": "private title must be omitted"}]}

        async def brightness(self):
            return {"level": 40}

    class Music:
        async def request(self, op):
            return {"running": music_running, "trusted": True} if op == "status" else {"nodes": []}

    monkeypatch.setattr(mac_check, "MacDesktop", lambda s: Desktop())
    monkeypatch.setattr(mac_check, "SafariMusic", lambda s: Music())
    monkeypatch.setattr(
        mac_check,
        "diagnose",
        AsyncMock(
            return_value={
                "accessibility": "granted",
                "screen_recording": "not_granted",
                "microphone": "not_determined",
                "automation": {},
            }
        ),
    )
    monkeypatch.setattr(
        mac_check, "system_status", AsyncMock(return_value={"battery": {"percent": 50}})
    )
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        mac_check.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            **kwargs,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"version": "test"})
            ),
        ),
    )
    result = asyncio.run(mac_check.inspect(Settings()))
    assert result["write_operations_tested"] is False
    assert result["scope"] == "read_only_current_launch_identity"
    assert result["real_checks"]["windows"]["result"] == {
        "windows_count": 1,
        "titles_omitted": True,
    }
    assert "private title" not in str(result)
    assert "screen_recording" in result["permission_instructions"]
    assert any(t["name"] == "mac_power" for t in result["tools"])
    assert result["components"]["speech"]["live_audio_tested"] is False
    if music_running:
        music = result["real_checks"]["music_status"]
        assert music["read_succeeded"] and not music["ok"] and music["state"] == "unknown"
    else:
        assert "music_status" not in result["real_checks"]
