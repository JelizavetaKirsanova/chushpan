import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from backend.config import Settings
from backend.hud.bus import HudBus
from backend.mac_center import MacCenter
from backend.memory.store import MemoryStore
from backend.security import Confirmations


def setup(tmp_path):
    store = MemoryStore(tmp_path / "mac.sqlite")
    tools = SimpleNamespace(
        desktop=SimpleNamespace(bridge=SimpleNamespace(request=AsyncMock())),
        confirmations=Confirmations(),
        _tools={},
        call=AsyncMock(),
        enabled=lambda _: True,
    )
    return store, tools, MacCenter(Settings(), store, tools, HudBus())


def test_capture_is_explicit_local_ocr_and_cleans_artifact(tmp_path):
    async def go():
        store, tools, service = setup(tmp_path)
        screenshot = tmp_path / "mock-screen.png"
        Image.new("RGB", (80, 40), "purple").save(screenshot)
        tools.call.side_effect = [
            json.dumps({"apps": []}),
            json.dumps({"path": str(screenshot), "ok": True}),
        ]
        tools.desktop.bridge.request.return_value = {"lines": [{"text": "Пример ошибки"}]}
        try:
            tools.call.assert_not_awaited()
            result = await service.handle({"op": "capture", "analyze": True})
            assert result["data"]["preview"].startswith("data:image/jpeg;base64,")
            assert result["data"]["analysis"]["mode"] == "local_ocr"
            assert "Пример ошибки" in result["data"]["analysis"]["text"]
            assert not screenshot.exists()
            assert tools.call.await_args_list[1].args[0] == "take_screenshot"
            await service.handle({"op": "clear_capture"})
            assert service.snapshot()["preview"] is None
        finally:
            await service.close()
            store.close()

    asyncio.run(go())


def test_excluded_app_prevents_capture_before_any_screen_read(tmp_path):
    async def go():
        store, tools, service = setup(tmp_path)
        tools.call.return_value = json.dumps(
            {"apps": [{"name": "Passwords", "bundle": "example.passwords"}]}
        )
        try:
            with pytest.raises(ValueError, match="менеджеры паролей"):
                await service.handle({"op": "capture"})
            assert tools.call.await_count == 1
            tools.desktop.bridge.request.assert_not_awaited()
        finally:
            await service.close()
            store.close()

    asyncio.run(go())


def test_permission_failure_and_stale_window_do_not_claim_success(tmp_path):
    async def go():
        store, tools, service = setup(tmp_path)
        try:
            with pytest.raises(ValueError, match="устарел"):
                await service.handle({"op": "window", "window_id": "missing", "action": "close"})
            tools.call.assert_not_awaited()
            tools.call.return_value = json.dumps(
                {"error": "Screen Recording denied", "result_status": "failed"}
            )
            with pytest.raises(ValueError, match="denied"):
                await service.handle({"op": "refresh"})
            assert service.snapshot()["history"][0]["status"] == "failed"
        finally:
            await service.close()
            store.close()

    asyncio.run(go())
