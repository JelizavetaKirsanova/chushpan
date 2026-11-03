"""Tools the assistant can call: weather, AI news, the Mac, timers, clock, memory, music."""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.config import Settings
from backend.mac_agent import LocalMacAgent, MacAgent
from backend.tools.registry import Tool, ToolError, ToolRegistry
from backend.tools.timers import TimerManager

if TYPE_CHECKING:
    from backend.mac import MacDesktop
    from backend.memory.store import MemoryStore
    from backend.tools.media import MediaService
    from backend.tools.news import NewsService
    from backend.tools.weather import WeatherService

__all__ = ["Tool", "ToolError", "ToolRegistry", "build_registry"]


def build_registry(
    s: Settings,
    timers: TimerManager | None = None,
    extra: list[Tool] | None = None,
    weather: WeatherService | None = None,
    news: NewsService | None = None,
    store: MemoryStore | None = None,
    media: MediaService | None = None,
    *,
    mac_agent: MacAgent | None = None,
    local_mac: bool = True,
    desktop: MacDesktop | None = None,
) -> ToolRegistry:
    """The tools enabled in `tools.enabled`, plus any session-specific `extra` ones.

    Pass the services to share their cache, e.g. with the briefing, or `media` to
    lower the music while the voice loop is awake. Memory needs the
    conversation's `store`; without it, the memory tools are left out.
    """
    from backend.brain.morning import MorningRoutine
    from backend.led import LEDStrip
    from backend.media.sleep_video import SleepVideo
    from backend.memory.profile import ProfileMemory
    from backend.night import NightRoutine
    from backend.shortcuts import shortcut_tools
    from backend.tools.clock import clock_tools
    from backend.tools.mac import mac_tools
    from backend.tools.media import MediaService, media_tools
    from backend.tools.memory import memory_tools
    from backend.tools.news import NewsService, news_tools
    from backend.tools.system import system_tools
    from backend.tools.timers import timer_tools
    from backend.tools.weather import WeatherService, weather_tools
    from backend.vision import vision_tools

    enabled = set(s.tools.enabled)
    tools: list[Tool] = []
    led = LEDStrip(s) if s.led.enabled and "system" in enabled else None
    if led:
        tools.append(led.tool())
    video = SleepVideo(s, store) if "media" in enabled and store is not None else None
    if "weather" in enabled:
        weather = weather or WeatherService(s)
        tools += weather_tools(s, weather)
    if "news" in enabled:
        news = news or NewsService(s)
        tools += news_tools(s, news)
    if "system" in enabled:
        tools += system_tools(s)
        tools += mac_tools(s, desktop)
        tools += vision_tools(s, desktop)
        tools += shortcut_tools(s)
        if store is not None:
            night = NightRoutine(s, store, desktop, video=video, led=led)
            tools.extend([night.tool(), night.permissions_tool()])
    if "timers" in enabled:
        tools += timer_tools(timers or TimerManager(s))
    if "clock" in enabled:
        tools += clock_tools(s)
    if "memory" in enabled and store is not None:
        tools += memory_tools(store)
    if "media" in enabled:
        media = media or MediaService(s, store=store)
        tools += media_tools(s, media, store)
        from backend.music import music_tool

        tools.append(music_tool(s, media))
        if video:
            video.media = media
            tools.append(video.tool())
    agent = mac_agent or (LocalMacAgent() if local_mac else None)
    for tool in tools:
        if tool.name in {"get_system_status", "set_volume", "open_app", "open_website"}:
            tool.required_capability = (
                "mac.apps" if tool.name in {"open_app", "open_website"} else "mac.system"
            )
        elif tool.name in {
            "play_music",
            "music_command",
            "music_search",
            "music_playlist",
            "media_control",
            "now_playing",
            "media_seek",
            "media_volume",
        }:
            tool.required_capability = "mac.media"
        if tool.required_capability:
            tool.agent = agent
            if isinstance(agent, LocalMacAgent):
                agent.register(tool.name, tool._handler)
        # Constraints are shared by schema, registered handler and registry dispatch.
        limits = {
            "level": {"minimum": 0, "maximum": 100},
            "change": {"minimum": -100, "maximum": 100},
            "days": {"minimum": 1, "maximum": 7},
            "count": {"minimum": 1, "maximum": 10},
            "id": {"minimum": 1},
            "replaces": {"minimum": 1},
            "hours": {"minimum": 0, "maximum": 24},
            "minutes": {"minimum": 0, "maximum": 1440},
            "seconds": {"minimum": 0, "maximum": 86400},
            "query": {"maxLength": 400},
            "fact": {"minLength": 1, "maxLength": 300},
            "text": {"minLength": 1, "maxLength": 300},
            "label": {"maxLength": 120},
            "name": {"minLength": 1, "maxLength": 120},
        }
        for key, schema in tool.parameters.items():
            schema.update(limits.get(key, {}))
        if tool.name == "open_app":
            tool.parameters["name"]["format"] = "safe-path"
        if tool.name in {"open_app", "manage_app", "get_windows"}:
            tool.parameters["name"]["pattern"] = r"[\w .()+&'-]+"
        if tool.name == "media_seek":
            tool.parameters["seconds"]["minimum"] = -86400
    profile = ProfileMemory(store) if "memory" in enabled and store is not None else None
    if profile:
        tools.append(profile.tool())
    if store is not None:
        from backend.personality import personality_tool

        tools.append(personality_tool(store))
    registry = ToolRegistry(
        [*tools, *(extra or [])],
        # The legacy native morning helper is local-only until its own Agent port exists.
        morning=MorningRoutine(s, weather, news, media)
        if isinstance(agent, LocalMacAgent)
        else None,
    )
    registry.memory = profile
    registry.youtube_video = video
    if store is not None:
        from backend.automation.service import Automations

        registry.automations = Automations(registry, s, store)
    return registry
