"""Foreign names for Russian TTS: phrase exceptions, then real English phonemes.

No translation. Unlisted names use an approximate Estonian spelling fallback,
reported in explicit diagnostics; curated exceptions take priority. Only speech
copies enter here, never display text or command arguments.
"""

import gzip
import logging
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from backend.audio.abbreviations import dictionary

log = logging.getLogger(__name__)
NAMES = dictionary()["names"]
PHONES = dict(
    zip(
        (
            "AA AE AH AO AW AY B CH D DH EH ER EY F G HH IH IY JH K L M N NG "
            "OW OY P R S SH T TH UH UW V W Y Z ZH"
        ).split(),
        (
            "а э а о ау ай б ч д з э эр эй ф г х и и дж к л м н нг оу ой п р с ш т с у у в у й з ж"
        ).split(),
        strict=True,
    )
)
TOKEN = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿĀ-ž]+(?:['’\-][A-Za-zÀ-ÖØ-öø-ÿĀ-ž]+)*")


@lru_cache(maxsize=1)
def lexicon() -> dict[str, str]:
    result = {}
    path = Path(__file__).with_name("lexicon") / "cmudict.dict.gz"
    with gzip.open(path, "rt", encoding="utf-8") as source:
        for line in source:
            word, _, phones = line.partition(" ")
            if word.endswith(")") or not re.fullmatch(r"[a-z]+(?:'[a-z]+)?", word):
                continue
            symbols = re.findall(r"\b[A-Z]+[012]?\b", phones.split("#")[0])
            if symbols and all(symbol.rstrip("012") in PHONES for symbol in symbols):
                spoken = "".join(
                    "э" if symbol == "AH0" else PHONES[symbol.rstrip("012")] for symbol in symbols
                )
                spoken = re.sub(r"([бвгджзклмнпрстфх])йу", r"\1ью", spoken)
                result.setdefault(word, spoken)
    return result


@lru_cache(maxsize=2048)
def word_sound(word: str) -> str | None:
    # Accents are not stripped: an Estonian word must not turn into a different English word.
    return lexicon().get(word.casefold())


def estonian_sound(word: str) -> str:
    """Approximate Estonian graphemes as phonemes, including length and clusters.

    Russian Silero cannot reproduce all Estonian vowel qualities or three lengths.
    This fallback preserves a word instead of replacing its content by a placeholder.
    """
    clusters = {
        "sh": "ш",
        "zh": "ж",
        "ch": "тш",
        "ng": "нг",
        "ts": "тс",
        "aa": "аа",
        "ee": "ээ",
        "ii": "ии",
        "oo": "оо",
        "uu": "уу",
        "õõ": "ыы",
        "ää": "яя",
        "öö": "ёё",
        "üü": "юю",
        "ai": "ай",
        "ei": "эй",
        "oi": "ой",
        "ui": "уй",
        "au": "ау",
        "ou": "оу",
    }
    singles = dict(
        zip(
            "abdefghijklmnoprstuvõäöüxyzcqw",
            "а б д э ф г х и й к л м н о п р с т у в ы я ё ю кс и з тс к у".split(),
            strict=True,
        )
    )
    parts = re.findall(
        r"sh|zh|ch|ng|ts|aa|ee|ii|oo|uu|õõ|ää|öö|üü|ai|ei|oi|ui|au|ou|.", word.casefold()
    )
    return "".join(clusters.get(part, singles.get(part, part)) for part in parts)


def prepare_foreign(
    text: str, exceptions: dict[str, str] | None = None, *, diagnostics=None
) -> str:
    text = unicodedata.normalize("NFC", text)
    names = {key.casefold(): value for key, value in NAMES.items()}
    names.update(
        {
            unicodedata.normalize("NFC", key).casefold(): value
            for key, value in (exceptions or {}).items()
            if key
        }
    )
    pattern = (
        r"(?<!\w)(?:"
        + "|".join(re.escape(key) for key in sorted(names, key=len, reverse=True))
        + r")(?!\w)"
    )
    text = re.sub(pattern, lambda match: names[match[0].casefold()], text, flags=re.I)
    unknown = 0

    def pronounce(match):
        nonlocal unknown
        spoken = word_sound(match[0])
        if spoken:
            return spoken
        unknown += 1
        spoken = estonian_sound(match[0])
        if diagnostics is not None:
            diagnostics.append({"kind": "estimated_foreign", "source": match[0], "spoken": spoken})
        return spoken

    text = TOKEN.sub(pronounce, text)
    if unknown:
        log.debug(
            "Foreign TTS: %d estimated foreign words; pronunciation exceptions available", unknown
        )
    return text
