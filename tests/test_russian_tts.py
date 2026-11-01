import asyncio
import logging
from pathlib import Path

import pytest

from backend.audio.russian import normalize
from backend.audio.tts import SileroSpeaker
from backend.voice import speak_tokens, speech_sentences
from tests.test_silero_tts import FakeSilero


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("31 видео", "тридцать одно видео"),
        ("31 ошибка", "тридцать одна ошибка"),
        ("22 попытки", "двадцать две попытки"),
        ("1/2", "один делённое на два"),
        ("1 234 видео", "одна тысяча двести тридцать четыре видео"),
        ("21 минута", "двадцать одна минута"),
        ("2 секунды", "две секунды"),
        ("10 минут", "десять минут"),
        ("0", "ноль"),
        ("-42", "минус сорок два"),
        ("2001", "две тысячи один"),
        ("25%", "двадцать пять процентов"),
        ("1%", "один процент"),
        ("-2,5%", "минус две целых пять десятых процента"),
        ("0.01", "ноль целых одна сотая"),
        ("12:31", "двенадцать часов тридцать одна минута"),
        ("01:02:03", "один час две минуты три секунды"),
        ("31.10.2026", "тридцать первое октября, год две тысячи двадцать шесть"),
        ("2026-10-31", "тридцать первое октября, год две тысячи двадцать шесть"),
        ("21 кг", "двадцать один килограмм"),
        ("-5°C", "минус пять градусов Цельсия"),
        ("YouTube", "Ютуб"),
        ("MacBook Pro", "Макбук Про"),
        ("Gemini 3.8 Flash", "Джемини три точка восемь Флэш"),
        ("Python 3.12", "Пайтон три точка двенадцать"),
        ("Safari и GitHub: 31 видео", "Сафари и Гитхаб: тридцать одно видео"),
    ],
)
def test_pronunciation(source, expected):
    assert normalize(source) == expected


def test_words_use_phonemes_and_unknown_names_have_private_diagnostics(caplog):
    with caplog.at_level(logging.DEBUG):
        assert normalize("Zed") == "зэд"
        details = []
        assert normalize("Qwzxqz", diagnostics=details) == "кузкскз"
    assert details == [{"kind": "estimated_foreign", "source": "Qwzxqz", "spoken": "кузкскз"}]
    assert "estimated foreign words" in caplog.text
    assert "Qwzxqz" not in caplog.text
    assert "Zed" not in caplog.text
    assert normalize("Zed YouTube", {"Zed": "Зэд", "YouTube": "Ютьюб"}) == "Зэд Ютьюб"


@pytest.mark.parametrize(
    "source",
    [
        "https://example.com/api/123?key=sk-abcdef",
        "/Users/user/code.py",
        "~/my-jarvis/config.yaml",
        "src/main.py",
        "`print(123)`",
        "```python\nprint(123)\n```",
        "api_key=abc123",
        "sk-abcd1234",
    ],
)
def test_literals_not_read_or_mutated(source):
    original = source
    assert normalize(source) == "фрагмент в тексте"
    assert source == original


def test_protected_content_is_removed_before_number_grouping():
    assert normalize("`123` 456 видео") == "фрагмент в тексте четыреста пятьдесят шесть видео"


@pytest.mark.parametrize("stride", [1, 2, 7, 1000])
def test_stream_keeps_display_and_normalizes_complete_numbers(stride):
    source = "Gemini 3.8 Flash: 31 видео. Заряд 25%. Код: ```python\nprint(123)\n```"
    speaker = SileroSpeaker(Path("unused"))
    speaker._voice = FakeSilero()
    seen, shown = [], []

    async def tokens():
        for i in range(0, len(source), stride):
            yield source[i : i + stride]

    async def no_audio(sentences, on_start=None):
        async for sentence in sentences:
            seen.append(sentence)
            speaker._synth(sentence)
        return True

    speaker.speak = no_audio
    assert asyncio.run(speak_tokens(speaker, tokens(), 20, "ru", on_token=shown.append))
    assert "".join(shown) == source
    assert seen == speech_sentences(source, 20, "ru")
    spoken = " ".join(r["text"] for r in speaker._voice.requests)
    assert "Джемини три точка восемь Флэш" in spoken
    assert "тридцать одно видео" in spoken
    assert "двадцать пять процентов" in spoken
    assert "print" not in spoken and "123" not in spoken
    assert any("Gemini 3.8" in text for text in seen)
    speaker._synth_pool.shutdown()
