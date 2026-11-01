"""Calendar fixtures pass through the actual streaming -> Silero boundary, without audio IO."""

import asyncio
import copy
from datetime import datetime
from pathlib import Path

import pytest

from backend.audio.abbreviations import dictionary
from backend.audio.russian import normalize
from backend.audio.tts import SileroSpeaker
from backend.brain.morning import calendar_sentences
from backend.voice import speak_tokens
from tests.test_silero_tts import FakeSilero

TITLES = [
    "Diskreetne matemaatika",
    "Programmeerimise algkursus",
    "Andmebaasid",
    "Tarkvaratehnika",
    "Sissejuhatus infotehnoloogiasse",
    "IT süsteemide arendus",
    "Jõhvi",
    "Kohtla-Järve",
    "Tallinna Tehnikaülikool",
]


@pytest.mark.parametrize("stride", [1, 7, 10000])
def test_original_calendar_and_ui_survive_streaming_into_silero(stride):
    calendar = {
        "timezone": "Europe/Tallinn",
        "events": [
            {
                "id": str(i),
                "title": title,
                "start": "2026-11-01T10:00:00+02:00",
                "end": "2026-11-01T11:00:00+02:00",
            }
            for i, title in enumerate(TITLES)
        ],
    }
    original = copy.deepcopy(calendar)
    source = calendar_sentences(calendar, datetime.fromisoformat("2026-11-01T08:00:00+02:00"))
    speaker = SileroSpeaker(Path("unused"))
    speaker._voice = FakeSilero()
    shown = []

    async def tokens():
        for i in range(0, len(source), stride):
            yield source[i : i + stride]

    async def silent(sentences, on_start=None):
        async for sentence in sentences:
            speaker._synth(sentence)
        return True

    speaker.speak = silent
    try:
        assert asyncio.run(speak_tokens(speaker, tokens(), 20, "ru", on_token=shown.append))
        assert "".join(shown) == source
        assert calendar == original
        spoken = " ".join(item["text"] for item in speaker._voice.requests).replace("+", "")
        for title in TITLES:
            assert title in source
            assert dictionary()["names"][title].replace("\u0301", "") in spoken
        assert "название в тексте" not in spoken
    finally:
        speaker._synth_pool.shutdown()


@pytest.mark.parametrize("key,entry", dictionary()["abbreviations"].items())
def test_curated_initialisms_and_word_acronyms(key, entry):
    assert normalize(key).replace("\u0301", "") == entry["spoken"].replace("\u0301", "")


@pytest.mark.parametrize(
    "source,expected",
    [
        ("Д.Т.П.", "дэ тэ пэ"),
        ("Д. Т. П.", "дэ тэ пэ"),
        ("A. P. I.", "эй пи ай"),
        ("API/CSS", "эй пи ай, си эс эс"),
        ("USB‑C", "ю эс би си"),
        ("ПРИВЕТ YouTube", "ПРИВЕТ Ютуб"),
    ],
)
def test_dots_compounds_brands_and_uppercase(source, expected):
    assert normalize(source) == expected


@pytest.mark.parametrize("stride", [1, 2, 1000])
def test_spaced_initialisms_remain_whole_in_stream(stride):
    from backend.audio.sentences import SentenceSplitter
    from backend.voice import speech_sentences

    source = "Впереди Д. Т. П. Проверь A. P. I. Потом YouTube."
    split = SentenceSplitter(protect_literals=True)
    sentences = []
    for i in range(0, len(source), stride):
        sentences.extend(split.feed(source[i : i + stride]))
    if rest := split.flush():
        sentences.append(rest)
    assert sentences == speech_sentences(source, 20, "ru")
    spoken = " ".join(normalize(sentence) for sentence in sentences)
    assert "дэ тэ пэ" in spoken and "эй пи ай" in spoken


def test_unlisted_estonian_words_not_replaced_by_placeholder():
    details = []
    result = normalize("Õppimine ülikoolis", diagnostics=details)
    assert "фрагмент" not in result and "название в тексте" not in result
    assert result == "ыппиминэ юликоолис"
    assert len(details) == 2
    assert all(item["kind"] == "estimated_foreign" for item in details)
