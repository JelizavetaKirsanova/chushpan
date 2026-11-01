"""Curated, data-driven pronunciations; uppercase alone does not imply an initialism."""

import json
import re
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def dictionary() -> dict:
    return json.loads(Path(__file__).with_name("pronunciations.json").read_text())


def expand(text: str, exceptions: dict[str, str] | None = None) -> str:
    entries = {
        key.casefold(): value["spoken"] for key, value in dictionary()["abbreviations"].items()
    }
    entries.update({key.casefold(): value for key, value in (exceptions or {}).items()})
    patterns = []
    for key in sorted(entries, key=len, reverse=True):
        patterns.append(re.escape(key))
        if key.isalpha() and len(key) >= 2:
            patterns.append(r"\.[ \t]*".join(re.escape(letter) for letter in key) + r"\.?")
    pattern = r"(?<!\w)(?:" + "|".join(patterns) + r")(?!\w)"

    def spoken(match):
        key = match[0].casefold()
        return entries.get(key, entries.get(re.sub(r"[.\s]", "", key), match[0]))

    return re.sub(pattern, spoken, text, flags=re.I)


def protected_compound(text: str) -> str | None:
    parts = text.split("/")
    entries = {
        key.casefold(): value["spoken"] for key, value in dictionary()["abbreviations"].items()
    }
    if len(parts) > 1 and all(part.casefold() in entries for part in parts):
        return ", ".join(entries[part.casefold()] for part in parts)
    return None
