import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from backend.audio.stt import is_morning_greeting
from backend.brain.conversation import Conversation
from backend.brain.morning import MorningRoutine, calendar_sentences
from backend.config import Settings
from backend.memory.store import MemoryStore
from backend.tools.news import DELFI_LATEST, NewsService, parse_delfi_latest
from backend.tools.registry import ToolError, ToolRegistry
from backend.tools.weather import WeatherService
from backend.voice import reply_aloud


@pytest.mark.parametrize(
    "text",
    [
        "Доброе утро, Чушпан!",
        "доброе утро чушпа́н",
        "Чушпан, доброе утро",
        "Эй, Чушпан, доброе утро!",
        "Доброе   утро, Чушпан",
    ],
)
def test_morning_command_includes_name_and_stress(text):
    assert is_morning_greeting(text, "Чушпан", require_name=True)


@pytest.mark.parametrize(
    "text",
    [
        "Он сказал доброе утро чушпан",
        "Что значит доброе утро чушпан?",
        "Доброе утро, Чушпан, не включай музыку",
        "доброе утро",
        "доброе утро сосед",
    ],
)
def test_unrelated_speech_cannot_activate_morning(text):
    assert not is_morning_greeting(text, "Чушпан", require_name=True)


def test_awake_greeting_does_not_need_the_name():
    assert is_morning_greeting("Доброе утро!", "Чушпан")


def delfi_page(titles):
    data = []

    def ref(value):
        data.append(value)
        return len(data) - 1

    article_type = ref("Article")
    now = datetime.now(UTC)
    for i, title in enumerate(titles):
        text = ref(title)
        title_node = ref({"text": text})
        content = ref({"title": title_node})
        published = ref((now - timedelta(minutes=i + 1)).isoformat())
        article_id = ref(100 + i)
        ref(
            {
                "__typename": article_type,
                "id": article_id,
                "publishAt": published,
                "content": content,
            }
        )
    # Payload order isn't publication order; duplicate articles and image titles are ignored.
    data.append(data[-1])
    data.append({"title": text})
    return '<script type="application/json" id="__NUXT_DATA__">' + json.dumps(data) + "</script>"


def test_delfi_reads_exactly_five_latest_titles_without_summaries():
    items = parse_delfi_latest(delfi_page([f"Новость {i}" for i in range(7)]))
    assert [item["title"] for item in items] == [f"Новость {i}" for i in range(5)]
    assert all(item["source"] == "Delfi" and "summary" not in item for item in items)
    assert items[0]["link"] == "https://rus.delfi.ee/artikkel/100"


def test_delfi_does_not_treat_changed_page_as_empty_news():
    with pytest.raises(ValueError):
        parse_delfi_latest("<html>Temporary error</html>")


def test_delfi_refreshes_from_official_latest_page_on_each_request():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, text=delfi_page(["Свежий заголовок"]))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = NewsService(Settings(), client)
            assert len(await service.delfi_headlines()) == 1
            await service.delfi_headlines()

    asyncio.run(scenario())
    assert requests == [DELFI_LATEST, DELFI_LATEST]


CALENDAR = {
    "timezone": "Europe/Tallinn",
    "events": [
        {
            "id": "1",
            "title": "Врач",
            "start": "2026-10-30T09:00:00+02:00",
            "end": "2026-10-30T10:00:00+02:00",
            "all_day": False,
        },
        {
            "id": "2",
            "title": "Поездка",
            "start": "2026-10-31T00:00:00+02:00",
            "end": "2026-11-01T00:00:00+02:00",
            "all_day": True,
        },
        {
            "id": "3",
            "title": "За пределами двух дней",
            "start": "2026-11-01T00:00:00+02:00",
            "end": "2026-11-01T01:00:00+02:00",
            "all_day": False,
        },
    ],
}
NOW = datetime.fromisoformat("2026-10-30T08:00:00+02:00")


def test_calendar_uses_local_dates_times_and_exclusive_all_day_end():
    text = calendar_sentences(CALENDAR, NOW)
    assert text == "Сегодня в календаре. В 09:00: Врач. Завтра в календаре. На весь день: Поездка."


def test_calendar_distinguishes_empty_day_from_denied_access():
    assert calendar_sentences({"events": [], "timezone": "Europe/Tallinn"}, NOW) == (
        "Сегодня в календаре событий нет. Завтра в календаре событий нет."
    )
    assert "прочитать не удалось" in calendar_sentences({"error": "calendar_permission"}, NOW)
    assert "событий нет" not in calendar_sentences({}, NOW)


def test_calendar_overnight_event_and_dst_window():
    data = {
        "timezone": "Europe/Tallinn",
        "events": [
            {
                "id": "night",
                "title": "Дежурство",
                "start": "2026-10-24T22:00:00+03:00",
                "end": "2026-10-25T09:00:00+02:00",
                "all_day": False,
            },
        ],
    }
    text = calendar_sentences(data, datetime.fromisoformat("2026-10-24T10:00:00+03:00"))
    assert "В 22:00: Дежурство" in text
    assert "Завтра в календаре. Продолжается с предыдущего дня: Дежурство" in text


def test_weather_live_coordinates_override_home_without_changing_settings():
    s = Settings()
    s.location.latitude, s.location.longitude = 50, 10
    original = s.location.model_dump()
    requests = []

    def handler(request):
        requests.append(request.url.params)
        return httpx.Response(200, json={"current": {"temperature_2m": 6}, "daily": {}})

    service = WeatherService(s, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = asyncio.run(
        service.forecast(days=1, coordinates={"latitude": 59.437, "longitude": 24.753})
    )
    assert requests[0]["latitude"] == "59.4" and requests[0]["longitude"] == "24.8"
    assert requests[0]["timezone"] == "auto" and result["now"]["temperature"] == 6
    assert s.location.model_dump() == original


class FakePersonal:
    async def read(self):
        return {"location": {"latitude": 59.4, "longitude": 24.7}, "calendar": CALENDAR}


class FakeWeather:
    async def forecast(self, **kwargs):
        assert kwargs == {"days": 1, "coordinates": {"latitude": 59.4, "longitude": 24.7}}
        return {"now": {"temperature": 6, "conditions": "пасмурно"}, "days": []}


class FakeNews:
    async def delfi_headlines(self):
        return [{"title": f"Заголовок {i}"} for i in range(1, 6)]


class FakeMedia:
    def __init__(self):
        self.calls = []
        self.youtube = SimpleNamespace(app=SimpleNamespace(launch=self.launch))

    async def launch(self):
        self.calls.append("launch")

    async def media_volume(self, *, level):
        self.calls.append(("volume", level))
        return {"ok": True}

    async def play_music(self, **kwargs):
        assert kwargs.get("kind", "playlist") == "playlist"
        self.calls.append("play_default_playlist")
        return {"ok": True}


def morning_conv(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_LANGUAGE", "ru-RU")
    monkeypatch.setenv("JARVIS_ASSISTANT_NAME", "Чушпан")
    s = Settings()
    media = FakeMedia()
    routine = MorningRoutine(s, FakeWeather(), FakeNews(), media, FakePersonal())

    class NoLLM:
        async def chat_stream(self, *args, **kwargs):
            raise AssertionError("morning command must not depend on LLM tool guesses")
            yield

    registry = ToolRegistry(morning=routine)
    # An empty registry still has the explicitly configured routine.
    conv = Conversation(s, NoLLM(), MemoryStore(tmp_path / "test.db"), tools=registry)
    return conv, media


def test_morning_orders_facts_and_defers_music_until_display_finished(tmp_path, monkeypatch):
    conv, media = morning_conv(tmp_path, monkeypatch)

    async def scenario():
        pieces = [p async for p in conv.reply("Доброе утро, Чушпан")]
        text = "".join(pieces)
        assert text.index("6 градусов") < text.index("Заголовок 1") < text.index("в календаре")
        assert all(f"Заголовок {i}" in text for i in range(1, 6))
        assert media.calls == []
        assert await conv.finish_reply() is None
        assert media.calls == ["launch", ("volume", 20), "play_default_playlist"]
        await conv.finish_reply()
        assert media.calls.count("play_default_playlist") == 1
        assert conv.last_stats.quiet
        assert len(conv.store.facts()) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("interrupted", [False, True])
def test_voice_starts_music_only_after_audio_drains(tmp_path, monkeypatch, interrupted):
    conv, media = morning_conv(tmp_path, monkeypatch)

    class QueuedSpeaker:
        async def speak(self, sentences, on_start=None):
            # Model text can finish while audio is still queued, but music must wait.
            self.said = [s async for s in sentences]
            assert media.calls == []
            await asyncio.sleep(0)
            return not interrupted

    completed = asyncio.run(reply_aloud(conv, QueuedSpeaker(), "Доброе утро, Чушпан", 20))
    assert completed is not interrupted
    assert bool(media.calls) is not interrupted


def test_music_failure_never_starts_playlist_loudly(tmp_path, monkeypatch):
    conv, media = morning_conv(tmp_path, monkeypatch)

    async def fail_volume(**kwargs):
        raise ToolError("Громкость недоступна.")

    media.media_volume = fail_volume

    async def scenario():
        _ = [p async for p in conv.reply("Доброе утро, Чушпан")]
        assert "Музыку включить не удалось" in await conv.finish_reply()
        assert media.calls == ["launch"]
        assert not conv.last_stats.quiet

    asyncio.run(scenario())


def test_missing_sources_do_not_make_up_weather_or_events(tmp_path, monkeypatch):
    conv, media = morning_conv(tmp_path, monkeypatch)

    class Unavailable:
        async def read(self):
            raise ToolError("Access denied")

    conv.tools.morning.personal = Unavailable()
    conv.tools.morning.news = None

    async def scenario():
        text = "".join([p async for p in conv.reply("Доброе утро, Чушпан")])
        assert "Погоду получить не удалось" in text
        assert "Delfi сейчас недоступны" in text
        assert "Календарь прочитать не удалось" in text
        assert "событий нет" not in text
        await conv.finish_reply()
        assert media.calls[-1] == "play_default_playlist"

    asyncio.run(scenario())


def test_personal_helper_has_usage_descriptions_and_cleans_temporary_results(tmp_path, monkeypatch):
    import plistlib

    import backend.personal as personal

    requests = []

    async def process(*args, **kwargs):
        requests.append(args)
        if args[0] == "open":
            Path(args[-1]).write_text(json.dumps({"calendar": {"events": []}}))
        return ""

    monkeypatch.setattr(personal, "process", process)
    client = personal.PersonalContext(Settings())
    result = asyncio.run(client.read(request_access=False))
    assert result == {"calendar": {"events": []}}
    app = client.s.data_path / "native" / "Чушпан — утро.app"
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert info["NSCalendarsFullAccessUsageDescription"] and info["NSLocationUsageDescription"]
    assert "-W" in requests[-1] and not Path(requests[-1][-1]).exists()
