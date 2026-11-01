"""Russian behavior across configuration, speech, tools, briefing and exports."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import yaml

from backend.audio.sentences import clean_for_speech
from backend.audio.stt import is_sign_off, is_wake_phrase, only_thanks
from backend.brain.briefing import BriefingData, build_messages, weather_brief, weather_sentence
from backend.brain.conversation import Conversation
from backend.brain.prompts import build_context_note, build_greeting, build_system_prompt
from backend.config import CONFIG_FILE, Settings
from backend.i18n import RU, get_locale
from backend.memory.export import export_conversation, to_markdown
from backend.memory.store import MemoryStore
from backend.tools import build_registry
from backend.tools.memory import asks_to_remember
from backend.tools.weather import WMO, describe, summarize

NOW = datetime(2026, 10, 29, 9, 5, tzinfo=ZoneInfo("Europe/Tallinn"))


def russian(**kwargs):
    return Settings(language="ru-RU", assistant_name="Чушпан", **kwargs)


def test_russian_defaults_and_language_codes():
    assert Settings.model_fields["language"].default == "ru-RU"
    assert yaml.safe_load(CONFIG_FILE.read_text())["language"] == "ru-RU"
    assert get_locale(None) == RU
    for code in ("ru", "ru-RU", "ru_ru", "RU-ru"):
        loc = get_locale(code)
        assert loc.lang == "ru" and loc.native_prompt
    s = russian()
    assert s.voice_name == "ru_RU-dmitri-medium"  # Piper fallback, isolated from production TTS
    assert s.stt_language == "ru"
    assert "Чушпан" in s.stt_hint


def test_russian_dotenv(tmp_path):
    env = tmp_path / ".env"
    env.write_text("JARVIS_LANGUAGE=ru-RU\n", encoding="utf-8")
    assert Settings(_env_file=env).locale.lang == "ru"


def test_russian_dates_greetings_and_context():
    s = russian(location={"timezone": "Europe/Tallinn", "latitude": 59.44})
    assert RU.format_datetime(NOW) == "четверг, 29 октября 2026 года, 09:05"
    assert build_greeting(s, NOW) is None
    note = build_context_note(s, NOW, first_turn=True, tools=True)
    assert "утро, осень" in note and "через инструмент" in note
    prompt = build_system_prompt(s, ["узнавать погоду"], [(1, "Предпочитает чай.")])
    assert "Всегда отвечай по-русски" in prompt
    assert "вызывай соответствующий инструмент" in prompt
    assert "[1] Предпочитает чай." in prompt
    assert NOW.strftime("%H:%M") not in prompt


@pytest.mark.parametrize(
    "gender",
    ["female", "женский", "male", "мужской"],
)
def test_russian_forms_of_address(gender):
    s = russian(user={"name": "Алекс", "gender": gender})
    assert s.owner_full_address == "Алекс"


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0 секунд"),
        (1, "1 секунда"),
        (2, "2 секунды"),
        (5, "5 секунд"),
        (11, "11 секунд"),
        (21, "21 секунда"),
        (22, "22 секунды"),
        (25, "25 секунд"),
        (60, "1 минута"),
        (120, "2 минуты"),
        (300, "5 минут"),
        (660, "11 минут"),
        (1260, "21 минута"),
        (1320, "22 минуты"),
        (1500, "25 минут"),
        (3725, "1 час, 2 минуты и 5 секунд"),
    ],
)
def test_russian_duration_agreement(seconds, expected):
    assert RU.format_duration(seconds) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("21°C, 22%, 5 km/h", "21 градус, 22 процента, 5 километров в час"),
        ("-11°C и 1 мм", "-11 градусов и 1 миллиметр"),
        ("2.5°C", "2,5 градуса"),
        ("25% и 22 км/ч", "25 процентов и 22 километра в час"),
    ],
)
def test_russian_spoken_units(text, expected):
    assert clean_for_speech(text, "ru") == expected


@pytest.mark.parametrize("text", ["Спасибо, Джарвис.", "Большое спасибо!", "Благодарю вас."])
def test_russian_thanks(text):
    assert only_thanks(text)
    assert not only_thanks("Спасибо, а какая погода?")


@pytest.mark.parametrize(
    "text",
    ["Спасибо, можешь отдыхать.", "На этом всё.", "Пока, Джарвис!", "Хорошо, это всё на сегодня."],
)
def test_russian_sign_off(text):
    assert is_sign_off(text)
    assert not is_sign_off("Можешь отдыхать после того, как расскажешь погоду.")


def test_russian_wake_and_repetition():
    assert is_wake_phrase("Хей, Джарвис!")
    assert not is_wake_phrase("Джарвис, какая погода?")
    history = [{"role": "assistant", "content": "Предыдущий ответ."}]
    assert Conversation._earlier_replies("Повтори ещё раз", history) == []
    assert Conversation._earlier_replies("Какая погода?", history)


@pytest.mark.parametrize(
    "text",
    [
        "Запомни, что я люблю чай.",
        "Сохрани мой адрес.",
        "Забуд мой старый адрес.",
        "Исправь эту запись.",
    ],
)
def test_russian_memory_requests(text):
    assert asks_to_remember(text)
    assert not asks_to_remember("Ты помнишь, какой чай я люблю?")
    assert not asks_to_remember("Я люблю чай.")


def test_russian_weather_and_briefing():
    s = russian()
    for code in WMO:
        assert describe(code, "ru")
    assert describe(61, "ru") == "небольшой дождь"
    assert describe(None, "ru") is None
    forecast = summarize(
        {
            "current": {"weather_code": 2, "temperature_2m": 21, "apparent_temperature": 17},
            "daily": {
                "time": ["2026-10-29", "2026-10-30"],
                "weather_code": [3, 61],
                "temperature_2m_min": [10, 9],
                "temperature_2m_max": [22, 21],
                "precipitation_probability_max": [45, 80],
            },
        },
        s,
        "Таллин",
    )
    assert [day["day"] for day in forecast["days"]] == ["сегодня", "завтра"]
    conv = SimpleNamespace(s=s, system_prompt=build_system_prompt(s))
    data = BriefingData(
        weather=weather_brief(forecast), news=[{"title": "Test headline", "source": "Test"}]
    )
    text = weather_sentence(conv, data, NOW)
    assert "21 градус" in text and "ощущается как 17 градусов" in text
    assert "45 процентов" in text and "от 10 до 22 градусов" in text
    messages = build_messages(conv, data, NOW)
    assert "Утренняя сводка" in messages[1]["content"]
    assert "на естественном русском" in messages[-1]["content"]


def test_russian_tools_and_timer_announcement():
    from backend.tools.timers import TimerManager

    s = russian()
    timers = TimerManager(s)
    result = asyncio.run(timers.set_timer(minutes=21, label="чай"))
    timer = next(iter(timers.timers.values()))
    assert "21 минута" in timers.announcement(timer)
    assert "чай" in timers.announcement(timer)
    assert result["id"]
    caps = build_registry(s).capabilities("ru")
    assert caps and all(any("а" <= c.lower() <= "я" for c in cap) for cap in caps)
    timers.cancel_all()


def test_russian_export(tmp_path):
    s = russian(location={"timezone": "Europe/Tallinn"})
    lines = [{"role": "user", "content": "Привет", "created_at": NOW.isoformat()}]
    md = to_markdown(s, lines, NOW)
    assert md.startswith("# Разговор с Чушпан\n")
    assert "Сохранено 29.10.2026 09:05" in md
    assert "**Ты**" in md
    assert "Разговор пока пуст" in to_markdown(s, [], NOW)
    with_store = MemoryStore(tmp_path / "test.db")
    try:
        conv = with_store.new_conversation()
        with_store.add(conv, "user", "Привет")
        path = export_conversation(s, with_store, conv, tmp_path)
        assert "разговор" in path.name and "Привет" in path.read_text()
    finally:
        with_store.close()


@pytest.mark.parametrize(
    "text,expected_request",
    [
        ("Эй, Чушпан!", ""),
        ("ЭЙ ЧУШПАН", ""),
        ("Эй, Чушпа́н, поставь таймер на минуту.", "поставь таймер на минуту."),
        ("Эй чушпанская команда", None),
        ("Чушпан", None),
        ("Привет всем", None),
        ("Скажи эй чушпан", None),
    ],
)
def test_custom_wake_phrase_matches_only_at_the_start(text, expected_request):
    from backend.audio.stt import strip_wake_phrase

    assert strip_wake_phrase(text, "эй чушпан") == expected_request


def test_assistant_name_and_stress_configuration(monkeypatch):
    from backend.audio.tts import apply_pronunciations
    from backend.voice import make_speaker

    monkeypatch.setitem(Settings.model_config, "yaml_file", CONFIG_FILE)
    s = Settings(_env_file=None)
    assert s.assistant_name == "Чушпан"
    assert s.wakeword.phrase == "эй чушпан"
    assert s.tts.pronunciations == {"Чушпан": "Чушпа́н"}
    assert make_speaker(s).pronunciations == s.tts.pronunciations
    assert apply_pronunciations("Я Чушпан. ЧУШПАН слушает.", s.tts.pronunciations) == (
        "Я Чушпа́н. Чушпа́н слушает."
    )
    assert apply_pronunciations("чушпанский", s.tts.pronunciations) == "чушпанский"
    assert is_wake_phrase("Эй, Чушпан!", s.assistant_name)
    assert not is_wake_phrase("Hey Jarvis", s.assistant_name)
    assert only_thanks("Спасибо, Чушпан.", s.assistant_name)
    assert not only_thanks("Спасибо, Jarvis.", s.assistant_name)
