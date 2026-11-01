import asyncio
import unicodedata
from pathlib import Path

import pytest

from backend.audio.foreign import lexicon
from backend.audio.russian import normalize
from backend.audio.tts import SileroSpeaker
from backend.voice import speak_tokens
from tests.test_silero_tts import FakeSilero


@pytest.mark.parametrize(
    "source,spoken",
    [
        ("TalTech", "Талтэк"),
        ("Tallinn", "Таллин"),
        ("Tartu", "Тарту"),
        ("Jõhvi", "Йыхви"),
        ("Kohtla-Järve", "Кохтла-Ярве"),
        ("YouTube", "Ютуб"),
        ("Spotify", "Спотифай"),
        ("Safari", "Сафари"),
        ("Apple Music", "Эпл Мьюзик"),
        ("Python", "Пайтон"),
        ("JavaScript", "Джаваскрипт"),
        ("Next.js", "Некст джей эс"),
        ("Adele", "Адель"),
        ("The Weeknd", "Зэ Уикэнд"),
        ("Blinding Lights", "Блайндинг Лайтс"),
        ("Põhja-Tallinn", "Пыхья-Таллин"),
        ("Tõnis Mägi", "Тынис Мяги"),
        ("Ülemiste", "Юлемисте"),
    ],
)
def test_foreign_names_are_whole_pronunciations(source, spoken):
    assert normalize(source).replace("\u0301", "") == spoken
    assert normalize(unicodedata.normalize("NFD", source)).replace("\u0301", "") == spoken


def test_real_english_phonemes_and_user_exceptions_not_letter_substitution():
    assert normalize("Zed") == "зэд"
    assert normalize("Music") == "мьюзик"
    assert normalize("UnknownBrand", {"UnknownBrand": "Моя марка"}) == "Моя марка"
    assert normalize("Jõhvi", {"Jõhvi": "Йыхви́"}) == "Йыхви́"
    assert lexicon() is lexicon()


@pytest.mark.parametrize("stride", [1, 2, 7, 1000])
def test_streaming_preserves_ui_and_complete_foreign_names(stride):
    source = "Я учусь в TalTech, живу в Jõhvi. Играет The Weeknd — Blinding Lights."
    speaker = SileroSpeaker(Path("unused"))
    speaker._voice = FakeSilero()
    shown = []

    async def tokens():
        for index in range(0, len(source), stride):
            yield source[index : index + stride]

    async def silent_speaker(sentences, on_start=None):
        async for sentence in sentences:
            speaker._synth(sentence)
        return True

    speaker.speak = silent_speaker
    try:
        asyncio.run(speak_tokens(speaker, tokens(), 1, "ru", on_token=shown.append))
        assert "".join(shown) == source
        spoken = " ".join(request["text"] for request in speaker._voice.requests).replace("+", "")
        for expected in ("Талтэк", "Йыхви", "Зэ Уикэнд", "Блайндинг Лайтс"):
            assert expected in spoken
    finally:
        speaker._synth_pool.shutdown()


def test_literals_and_commands_are_not_rewritten_by_foreign_dictionary():
    original = "https://example.com/Tallinn /tmp/TalTech.py `Next.js` api_key=secret"
    spoken = normalize(original)
    assert "Талл" not in spoken and "Некст" not in spoken and "secret" not in spoken
    assert original == "https://example.com/Tallinn /tmp/TalTech.py `Next.js` api_key=secret"
