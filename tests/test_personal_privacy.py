import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.config import Settings
from backend.mac import MacDesktop
from backend.memory.store import MemoryStore
from backend.tools.registry import ToolError


def test_capture_exclusion_applies_to_voice_and_registry_desktop_before_screen_access(tmp_path):
    store = MemoryStore(tmp_path / "privacy.sqlite")
    desktop = MacDesktop(Settings())
    desktop.memory = store
    store.set_preference("settings", "access", {"blocked_apps": [" Safari "]})
    desktop.bridge.request = AsyncMock(
        return_value={"apps": [{"name": "Safari", "bundle": "com.apple.Safari"}]}
    )
    try:
        with pytest.raises(ToolError, match="приватности"):
            asyncio.run(desktop.screenshot())
        assert desktop.bridge.request.await_count == 1
        assert desktop.bridge.request.await_args.args[0] == "desktop_apps"
        assert not (desktop.s.data_path / "screenshots").exists()
    finally:
        store.close()
