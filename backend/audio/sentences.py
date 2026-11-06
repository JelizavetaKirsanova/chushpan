"""Split streamed LLM text into sentences that can be spoken one by one."""

from __future__ import annotations

import re
import unicodedata

from backend.i18n import noun_form

_BOUNDARY = re.compile(r"[.!?…]+[\"')\]]*\s+|\n+")
_LAST_WORD = re.compile(r"(\w+)$")
_CLAUSE = re.compile(r"[,;:]\s+")
_ABBREVIATIONS = {
    "sr",
    "sra",
    "srta",
    "dr",
    "dra",
    "prof",
    "profa",
    "av",
    "etc",
    "mr",
    "mrs",
    "ms",
    "st",
    "vs",
    "jr",
    "г",
    "ул",
    "д",
    "им",
    "руб",
}
_MARKUP = re.compile(r"[*_#`~>|]+")
# "1. ", "2) ", "- " opening a line: news summaries come back as lists despite the prompt.
_LIST_MARKER = re.compile(r"^[ \t]*(?:\d{1,2}[.)]|[-•])[ \t]+", re.MULTILINE)

# Units after a number, spelled out. The prompt asks for this, but tool data
# ("89%", "14°C") often leaks through, and the voice would misread or skip the symbol.
_UNITS = {
    "pt": (
        (r"\s*°\s*C\b", " graus"),
        (r"\s*[°º]", " graus"),
        (r"\s*%", " por cento"),
        (r"\s*km/h\b", " quilômetros por hora"),
        (r"\s*mm\b", " milímetros"),
    ),
    "en": (
        (r"\s*°\s*C\b", " degrees"),
        (r"\s*[°º]", " degrees"),
        (r"\s*%", " per cent"),
        (r"\s*km/h\b", " kilometres an hour"),
        (r"\s*mm\b", " millimetres"),
    ),
}
_UNIT_RES = {
    lang: [(re.compile(r"(?<=\d)" + pattern), word) for pattern, word in rules]
    for lang, rules in _UNITS.items()
}


def spell_units(text: str, lang: str) -> str:
    if lang == "ru":
        forms = {
            "degrees": ("градус", "градуса", "градусов"),
            "percent": ("процент", "процента", "процентов"),
            "wind": ("километр в час", "километра в час", "километров в час"),
            "rain": ("миллиметр", "миллиметра", "миллиметров"),
        }
        units = r"(?P<degrees>°\s*[CС]\b|[°º])|(?P<percent>%)"
        units += r"|(?P<wind>km/h\b|км/ч\b)|(?P<rain>mm\b|мм\b)"

        def expand(match: re.Match) -> str:
            number = match["number"]
            unit = next(key for key in forms if match[key] is not None)
            word = noun_form(float(number.replace(",", ".")), forms[unit])
            return f"{number.replace('.', ',')} {word}"

        return re.sub(rf"(?P<number>-?\d+(?:[.,]\d+)?)\s*(?:{units})", expand, text)
    for pattern, word in _UNIT_RES.get(lang, _UNIT_RES["en"]):
        text = pattern.sub(word, text)
    return text


def clean_for_speech(text: str, lang: str = "pt") -> str:
    """Drop markdown, list markers and emoji that a TTS voice would read aloud; spell out units."""
    text = spell_units(_MARKUP.sub("", _LIST_MARKER.sub("", text)), lang)
    text = "".join(c for c in text if unicodedata.category(c) not in ("So", "Cs"))
    return re.sub(r"\s+", " ", text).strip()


class SentenceSplitter:
    def __init__(
        self,
        min_chars: int = 20,
        first_min_chars: int = 6,
        first_clause_chars: int = 30,
        *,
        protect_literals: bool = False,
        max_chars: int = 240,
    ) -> None:
        self.min_chars = min_chars
        self.protect_literals = protect_literals
        self.max_chars = max(80, max_chars)
        # A long first sentence may be cut at its first comma so the voice starts sooner.
        self.first_clause_chars = first_clause_chars
        # The first sentence goes out as soon as possible: it sets the perceived latency.
        self.first_min_chars = first_min_chars
        self._emitted = False
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        self._buf += text
        out: list[str] = []
        start = 0
        for m in _BOUNDARY.finditer(self._buf):
            if self._protected(m.start()):
                continue
            if m.group().startswith("."):
                word = _LAST_WORD.search(self._buf[start : m.start()])
                if word and word.group(1).lower() in _ABBREVIATIONS:
                    continue
                if word and len(word.group(1)) == 1 and word.group(1).isupper():
                    continue  # initials and spaced initialisms: А. П. И. / Д. Т. П.
            sentence = self._buf[start : m.end()].strip()
            # Very short fragments sound choppy on their own; merge with the next one.
            limit = self.min_chars if self._emitted else self.first_min_chars
            if len(sentence) < limit:
                continue
            out.append(sentence)
            self._emitted = True
            start = m.end()
        self._buf = self._buf[start:]
        if not self._emitted and not out:
            for m in _CLAUSE.finditer(self._buf):
                if m.start() >= self.first_clause_chars and not self._protected(m.start()):
                    out.append(self._buf[: m.end()].strip())
                    self._buf = self._buf[m.end() :]
                    self._emitted = True
                    break
        # A model can stream an entire paragraph without punctuation. Bound the
        # wait at a word boundary; don't split a number, URL, abbreviation or name.
        while len(self._buf) > self.max_chars:
            candidates = [
                match.start()
                for match in re.finditer(r"\s+", self._buf[: self.max_chars + 1])
                if match.start() >= self.first_min_chars
                and not self._protected(match.start())
                and not self._abbreviation_before(match.start())
            ]
            if not candidates:
                break  # A single long protected literal stays whole.
            cut = candidates[-1]
            out.append(self._buf[:cut].strip())
            self._buf = self._buf[cut:].lstrip()
            self._emitted = True
        return out

    def _abbreviation_before(self, position: int) -> bool:
        word = self._buf[:position].split()[-1].rstrip(".")
        return word.casefold() in _ABBREVIATIONS or (len(word) == 1 and word.isupper())

    def _protected(self, position: int) -> bool:
        if not self.protect_literals:
            return False
        from backend.audio.russian import PROTECTED

        return any(m.start() <= position < m.end() for m in PROTECTED.finditer(self._buf))

    def flush(self) -> str | None:
        rest, self._buf = self._buf.strip(), ""
        return rest or None
