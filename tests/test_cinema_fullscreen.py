"""Public-API doubles for fullscreen transitions; Safari is checked separately."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.cinema.adapters import HTML5Adapter
from backend.tools.registry import ToolError


def test_fullscreen_flag_does_not_confirm_a_pending_transition(monkeypatch):
    bridge = SimpleNamespace(
        evaluate=AsyncMock(
            side_effect=[
                {"fullscreen": True, "fullscreen_pending": True},
                {"fullscreen": True, "fullscreen_pending": True},
                {"fullscreen": True, "fullscreen_pending": False},
            ]
        )
    )
    monkeypatch.setattr("backend.cinema.adapters.asyncio.sleep", AsyncMock())
    result = asyncio.run(HTML5Adapter(bridge).control("fullscreen"))
    assert result["fullscreen_confirmed"] and bridge.evaluate.await_count == 3
    assert [call.args[1]["action"] for call in bridge.evaluate.await_args_list] == [
        "fullscreen",
        "status",
        "status",
    ]


def test_rejected_transition_cannot_be_hidden_by_an_early_fullscreen_flag(monkeypatch):
    bridge = SimpleNamespace(
        evaluate=AsyncMock(
            side_effect=[
                {"fullscreen": True, "fullscreen_pending": True},
                {
                    "fullscreen": True,
                    "fullscreen_pending": False,
                    "fullscreen_error": "gesture_required",
                },
            ]
        )
    )
    monkeypatch.setattr("backend.cinema.adapters.asyncio.sleep", AsyncMock())
    with pytest.raises(ToolError, match="действия пользователя"):
        asyncio.run(HTML5Adapter(bridge).control("fullscreen"))
    assert bridge.evaluate.await_count == 2


def test_cancelled_transition_sends_no_further_browser_commands(monkeypatch):
    bridge = SimpleNamespace(evaluate=AsyncMock(side_effect=asyncio.CancelledError))
    monkeypatch.setattr("backend.cinema.adapters.asyncio.sleep", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(HTML5Adapter(bridge).control("fullscreen"))
    assert bridge.evaluate.await_count == 1
