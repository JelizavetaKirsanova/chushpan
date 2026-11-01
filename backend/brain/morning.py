"""The explicit morning greeting: live facts in order, then music after speech finishes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from backend.config import Settings
from backend.personal import PersonalContext
from backend.tools.registry import ToolError

if TYPE_CHECKING:
    from backend.brain.conversation import Conversation
    from backend.tools.media import MediaService
    from backend.tools.news import NewsService
    from backend.tools.weather import WeatherService

log = logging.getLogger(__name__)


def calendar_sentences(data: dict, now: datetime | None = None) -> str:
    if data.get("error") or "events" not in data:
        return (
            "Календарь прочитать не удалось. Разреши приложению «Чушпан — утро» доступ "
            "к календарям в настройках конфиденциальности macOS."
        )
    tz = ZoneInfo(data.get("timezone") or "Europe/Tallinn")
    now = now or datetime.now(tz)
    today = now.astimezone(tz).date()
    pieces = []
    for offset, label in enumerate(("Сегодня", "Завтра")):
        day = today + timedelta(days=offset)
        start = datetime.combine(day, datetime.min.time(), tz)
        end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tz)
        events, seen = [], set()
        for event in data["events"]:
            begins = datetime.fromisoformat(event["start"]).astimezone(tz)
            ends = datetime.fromisoformat(event["end"]).astimezone(tz)
            key = (event.get("id"), begins, ends, event["title"])
            if begins >= end or ends <= start or key in seen:
                continue
            seen.add(key)
            title = " ".join(event["title"].split()) or "Без названия"
            if event.get("all_day"):
                line = f"На весь день: {title}."
            elif begins < start:
                line = f"Продолжается с предыдущего дня: {title}."
            else:
                line = f"В {begins:%H:%M}: {title}."
            events.append((begins, line))
        if events:
            pieces.append(f"{label} в календаре. " + " ".join(v for _, v in sorted(events)))
        else:
            pieces.append(f"{label} в календаре событий нет.")
    return " ".join(pieces)


class MorningRoutine:
    def __init__(
        self,
        s: Settings,
        weather: WeatherService | None,
        news: NewsService | None,
        media: MediaService | None,
        personal: PersonalContext | None = None,
    ) -> None:
        self.s, self.weather, self.news, self.media = s, weather, news, media
        self.personal = personal or PersonalContext(s)

    async def _guard(self, awaitable, timeout: float = 65):
        try:
            return await asyncio.wait_for(awaitable, timeout)
        except Exception as exc:  # noqa: BLE001 - one failed source doesn't cancel the morning
            log.warning("Morning source unavailable: %s", type(exc).__name__)
            return None

    async def stream(self, conv: Conversation) -> AsyncIterator[str]:
        from backend.brain.briefing import BriefingData, weather_brief, weather_sentence

        personal_task = asyncio.create_task(self._guard(self.personal.read()))
        news_task = (
            asyncio.create_task(self._guard(self.news.delfi_headlines(), 20)) if self.news else None
        )
        try:
            yield "Доброе утро. "
            context = await personal_task or {}
            location = context.get("location") or {}
            forecast = None
            if self.weather:
                if "latitude" in location and "longitude" in location:
                    forecast = await self._guard(
                        self.weather.forecast(days=1, coordinates=location), 20
                    )
                elif self.s.location.resolved:
                    yield "Геопозиция недоступна. Погода для сохранённого города. "
                    forecast = await self._guard(self.weather.forecast(days=1), 20)
            if forecast and (
                sentence := weather_sentence(conv, BriefingData(weather=weather_brief(forecast)))
            ):
                yield sentence + " "
            else:
                yield (
                    "Погоду получить не удалось. Если Mac ещё не дал геопозицию, "
                    "разреши её приложению «Чушпан — утро» в настройках конфиденциальности. "
                )
            headlines = await news_task if news_task else None
            if headlines:
                count = (
                    "Пять последних заголовков Delfi."
                    if len(headlines) == 5
                    else (f"На Delfi удалось получить только {len(headlines)} свежих заголовка.")
                )
                yield count + " "
                for item in headlines:
                    yield item["title"].rstrip(".!? ") + ". "
            else:
                yield "Свежие заголовки Delfi сейчас недоступны. "
            yield calendar_sentences(context.get("calendar") or {}) + " "
            # The caller starts music only after the audio player has drained this stream.
        finally:
            for task in (personal_task, news_task):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(
                *[t for t in (personal_task, news_task) if t], return_exceptions=True
            )

    async def start_music(self) -> str | None:
        if not self.media or not self.media.youtube:
            return "Не удалось включить любимый плейлист: YouTube Music недоступен."
        try:
            # Set and confirm the quiet level BEFORE playback. If the slider is unavailable,
            # fail instead of briefly blasting music at the previously saved level.
            await self.media.youtube.app.launch()
            volume = await self.media.media_volume(level=self.s.briefing.morning_volume)
            if not volume.get("ok"):
                raise ToolError("Не удалось установить небольшую громкость.")
            # The existing morning scenario uses the saved favourite playlist. A plain
            # "включи музыку" in the new Safari adapter instead resumes the current queue.
            result = await self.media.play_music(
                **({"kind": "playlist"} if self.s.media.youtube_music.transport == "safari" else {})
            )
            if not result.get("ok"):
                raise ToolError("Не удалось включить сохранённый любимый плейлист.")
            return None
        except ToolError as exc:
            return f"Музыку включить не удалось. {exc}"
