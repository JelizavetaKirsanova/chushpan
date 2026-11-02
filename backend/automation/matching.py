"""Cached local Russian semantic features; ambiguity never becomes an arbitrary action."""

import math
import re
from collections import Counter

from backend.audio.stt import is_hallucination
from backend.automation.models import normalize_phrase

# Small local semantic vocabulary plus Russian inflection stems. No downloaded model.
CONCEPTS = {
    "сон": r"спать|сна|сну|сон|ложить\w*|ложус\w*|ложусь|ложим\w*|засып\w*|кроват\w*|ночн\w*",
    "подготовить": r"подготов\w*|готов\w*",
    "свет": r"лент\w*|подсвет\w*|освещ\w*|свет\w*",
    "включить": r"включ\w*|вруб\w*|заж\w*",
    "выключить": r"выключ\w*|выруб\w*|погас\w*|отключ\w*",
    "вечер": r"вечер\w*",
    "розовый": r"розов\w*",
    "теплый": r"тепл\w*",
}
STOP = {
    "я",
    "мне",
    "на",
    "в",
    "ко",
    "к",
    "со",
    "с",
    "и",
    "а",
    "все",
    "уже",
    "пора",
    "давай",
    "хочу",
    "сделай",
    "поставь",
    "режим",
    "пошла",
    "пошел",
}
BLOCKED = re.compile(
    r"\b(?:не|нет|нельзя|если|когда|завтра|потом|позже|или|либо|сказал\w*|сказала|скажи|расскажи|почему|как|что|покажи)\b|[«»\"“”`?;\n]",
    re.I,
)


def features(text):
    words = normalize_phrase(text).split()
    result = []
    for word in words:
        if word in STOP:
            continue
        concept = next(
            (key for key, pattern in CONCEPTS.items() if re.fullmatch(pattern, word)), None
        )
        result.append(
            concept
            or (
                word[:-2]
                if len(word) > 6 and word.endswith(("ую", "ой", "ая", "ый", "ым"))
                else word
            )
        )
    return Counter(result)


def similarity(left, right):
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(key, 0) for key, value in left.items())
    return dot / math.sqrt(sum(v * v for v in left.values()) * sum(v * v for v in right.values()))


class Matcher:
    def __init__(self, store):
        self.store, self.generation = store, -1
        self.rows = []
        self.exact = {}
        self.names = {}

    def index(self):
        if self.generation != self.store.generation:
            self.rows = [
                (s, [(normalize_phrase(p), features(p)) for p in s.phrases])
                for s in self.store.list()
                if s.enabled and any(t.kind == "voice" for t in s.triggers)
            ]
            self.generation = self.store.generation
            self.exact = {phrase: s for s, phrases in self.rows for phrase, _ in phrases}
            names = {}
            for scenario, _ in self.rows:
                for verb in ("запусти", "выполни"):
                    names.setdefault(
                        verb + " сценарий " + normalize_phrase(scenario.name), []
                    ).append(scenario)
            self.names = names
        return self.rows

    def match(self, text, *, exact_only=False):
        value = normalize_phrase(text)
        empty = {"scenario_id": None, "confidence": 0.0, "method": "none", "candidates": []}
        if not value or is_hallucination(text) or len(text) > 1000 or BLOCKED.search(text):
            return empty
        rows = self.index()
        exact = [self.exact[value]] if value in self.exact else self.names.get(value, [])
        if len(exact) == 1:
            return {
                "scenario_id": exact[0].id,
                "name": exact[0].name,
                "confidence": 1.0,
                "method": "exact",
                "candidates": [],
            }
        if exact_only:
            return empty
        query = features(text)
        scores = sorted(
            [
                (max((similarity(query, f) for _, f in phrases), default=0), s)
                for s, phrases in rows
                if s.mode == "smart"
            ],
            key=lambda item: item[0],
            reverse=True,
        )
        candidates = [
            {"id": s.id, "name": s.name, "confidence": round(score, 4)}
            for score, s in scores[:3]
            if score >= 0.4
        ]
        if not candidates:
            return empty
        score, scenario = scores[0]
        margin = score - (scores[1][0] if len(scores) > 1 else 0)
        # An extra unrecognized word cannot disappear into a broad one-word match.
        accepted = score >= 0.88 and margin >= 0.12 and len(query) >= 1
        return {
            "scenario_id": scenario.id if accepted else None,
            "name": scenario.name if accepted else None,
            "confidence": round(score, 4),
            "method": "semantic" if accepted else "ambiguous",
            "candidates": candidates,
        }
