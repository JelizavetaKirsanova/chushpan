"""Local, explainable metadata recommendations. No model or paid AI calls."""

import asyncio
import math
import re
from collections import Counter, defaultdict

from backend.cinema.filters import allowed, with_preferences
from backend.cinema.models import CatalogueQuery
from backend.tools.registry import ToolError

SHELVES = [
    ("liked", "Тебе понравится"),
    ("similar", "Похоже на любимые"),
    ("hidden", "Недооценённые находки"),
    ("mood", "Под настроение"),
    ("explore", "Попробуй что-то новое"),
    ("now", "Можно посмотреть прямо сейчас"),
]
MOODS = {"any": "", "calm": "комедия", "tense": "триллер", "adventure": "приключения"}
STOP = set(
    "фильм сериал когда после перед который которая которые очень чтобы этого своей "
    "the and this that with from into their they have about movie series".split()
)


def normalized(value):
    return " ".join(re.findall(r"\w+", value.casefold().replace("ё", "е")))


def features(item):
    meta = item.get("metadata") or {}
    result = {}
    for field, prefix, weight in (
        ("genres", "genre", 1.4),
        ("keywords", "keyword", 1.2),
        ("directors", "director", 1.8),
        ("cast", "actor", 0.55),
    ):
        for name in meta.get(field, [])[:40]:
            result[f"{prefix}:{normalized(name)}"] = (weight, name)
    # Sparse description features complement keywords; cap their total influence.
    words = [
        w for w in normalized(item.get("description", "")).split() if len(w) > 4 and w not in STOP
    ]
    for word, _ in Counter(words).most_common(30):
        result[f"theme:{word}"] = (0.12, word)
    return result


def signal(item, reset_at="", prefs=None):
    rating = item.get("user_rating") if prefs is None or prefs.recommend_ratings else None
    updated = item.get("library_updated_at") or item.get("updated_at") or ""
    if reset_at and (item.get("rating_updated_at") or "") <= reset_at:
        rating = None
    value = (rating - 6) / 2 if rating is not None else 0.0
    favorite_at = item.get("favorite_added_at") or updated
    if (
        item.get("favorite")
        and (prefs is None or prefs.recommend_favorites)
        and (not reset_at or favorite_at > reset_at)
    ):
        value += 1.5
    viewed = (item.get("current") or {}).get("last_viewed") or updated
    if (
        rating is None
        and (prefs is None or prefs.recommend_history)
        and item.get("status") in {"watching", "watched"}
        and (not reset_at or viewed > reset_at)
    ):
        value += 0.2
    return max(-3.0, min(3.0, value))


def taste(items, reset_at="", prefs=None):
    weights, evidence = defaultdict(float), Counter()
    samples = 0
    for item in items:
        strength = signal(item, reset_at, prefs)
        if not strength:
            continue
        samples += 1
        for key, (importance, _) in features(item).items():
            weights[key] += strength * importance
            evidence[key] += 1
    # Shrink sparse evidence; a single title never creates certainty.
    return {k: v / (3 + math.sqrt(evidence[k])) for k, v in weights.items()}, samples


def score(item, weights):
    parts = [
        (weights.get(k, 0) * importance, k, name)
        for k, (importance, name) in features(item).items()
    ]
    meta = item.get("metadata") or {}
    quality = (meta.get("rating", 0) - 6) * 0.12
    value = sum(p[0] for p in parts) + quality
    positives = sorted((p for p in parts if p[0] > 0.02), reverse=True)
    if positives:
        _, key, label = positives[0]
        explanation = {
            "genre": f"Тебе нравятся истории жанра «{label}».",
            "director": f"В твоих любимых есть работы: {label}.",
            "actor": f"В понравившихся историях снимается {label}.",
            "keyword": f"Общая тема с понравившимися: {label}.",
            "theme": f"В описании есть общая тема: «{label}».",
        }[key.split(":")[0]]
    else:
        explanation = "Высокий рейтинг TMDB; пока мало совпадений с твоими оценками."
    return value, explanation


def profile(items, events=(), ratings=()):
    rated = [i for i in items if i.get("user_rating") is not None]
    watched = [i for i in items if i.get("status") == "watched"]
    positive = [i for i in items if signal(i) >= 1]
    groups = {}
    for field in ("genres", "directors", "cast"):
        counts = Counter(
            name for i in positive for name in set((i.get("metadata") or {}).get(field, []))
        )
        groups[field] = [{"name": name, "count": count} for name, count in counts.most_common(8)]
    bins = Counter(min(10, int(i["user_rating"])) for i in rated)
    by_id = {i["id"]: i for i in items}
    months, seen = defaultdict(lambda: {"titles": set(), "genres": Counter(), "ratings": []}), set()
    for event in events:
        key = (event["created_at"][:7], event["title_id"])
        if key in seen or event["title_id"] not in by_id:
            continue
        seen.add(key)
        month = months[key[0]]
        month["titles"].add(key[1])
        month["genres"].update((by_id[key[1]].get("metadata") or {}).get("genres", []))
    for event in ratings:
        if event["rating"] is not None:
            months[event["created_at"][:7]]["ratings"].append(event["rating"])
    trend = [
        {
            "month": key,
            "count": len(value["titles"]),
            "genre": value["genres"].most_common(1)[0][0] if value["genres"] else "",
            "rating": round(sum(value["ratings"]) / len(value["ratings"]), 2)
            if value["ratings"]
            else None,
        }
        for key, value in sorted(months.items())[-12:]
    ]
    enough = len(rated) >= 10
    insights = [
        f"Сейчас {len(rated)} оценок. "
        + (
            "Тенденции становятся заметнее, но выборка всё ещё ограничена твоей медиатекой."
            if enough
            else "Пока это первые наблюдения; для устойчивого профиля нужно больше оценок."
        )
    ]
    if groups["genres"]:
        first = groups["genres"][0]
        insights.append(f"«{first['name']}» встречается в {first['count']} понравившихся историях.")
    enriched = sum(bool((i.get("metadata") or {}).get("enriched")) for i in positive)
    if enriched < len(positive):
        insights.append(
            "Данные об актёрах, режиссёрах и темах есть не у всех названий. "
            "Обнови метаданные для более полного профиля."
        )
    return {
        "watched_movies": sum(i["kind"] == "movie" for i in watched),
        "watched_series": sum(i["kind"] == "series" for i in watched),
        "favorites": sum(bool(i.get("favorite")) for i in items),
        "rated_count": len(rated),
        "average": round(sum(i["user_rating"] for i in rated) / len(rated), 2) if rated else None,
        "distribution": [{"rating": i, "count": bins[i]} for i in range(1, 11)],
        "top": sorted(rated, key=lambda i: (-i["user_rating"], i["title"]))[:10],
        "least": sorted(
            [i for i in rated if i["kind"] == "movie" and i["user_rating"] <= 5],
            key=lambda i: (i["user_rating"], i["title"]),
        )[:8],
        "trend": trend,
        "insights": insights,
        "confidence": "developing" if enough else "small",
        "metadata_coverage": enriched,
        "positive_count": len(positive),
        **groups,
    }


class Recommender:
    def __init__(self, tmdb, catalogue):
        self.tmdb, self.catalogue = tmdb, catalogue

    async def recommendations(self, library, state, prefs, *, mood="any", ready=(), genre=""):
        if mood not in MOODS:
            raise ValueError("Выбери настроение из списка.")
        weights, samples = (
            taste(library, state["reset_at"], prefs) if prefs.personalize else ({}, 0)
        )
        seeds = [
            i["catalogue_key"]
            for i in sorted(
                library, key=lambda i: signal(i, state["reset_at"], prefs), reverse=True
            )
            if prefs.personalize
            and signal(i, state["reset_at"], prefs) >= 1
            and i.get("catalogue_key", "").startswith("tmdb:")
        ][:2]
        requests = [
            CatalogueQuery(feed="recommended" if seeds else "top"),
            CatalogueQuery(feed="new", sort="rating"),
            CatalogueQuery(feed="hidden", sort="rating"),
        ]
        if genre or MOODS[mood]:
            requests.append(CatalogueQuery(genre=genre or MOODS[mood], sort="rating"))
        pages = await asyncio.gather(
            *(self.catalogue.page(with_preferences(q, prefs), seeds=seeds) for q in requests),
            return_exceptions=True,
        )
        candidates, errors = {}, []
        for page in pages:
            if isinstance(page, Exception):
                errors.append(
                    str(page) if isinstance(page, ToolError) else "Часть TMDB недоступна."
                )
                continue
            for item in page["items"]:
                candidates.setdefault(item["catalogue_key"], item)
        excluded_keys = {i.get("catalogue_key") for i in library}
        excluded_names = {
            (i["kind"], normalized(i["title"]), i["year"])
            for i in library
            if not i.get("catalogue_key")
        }
        dismissed = set(state["dismissed"])
        candidates = [
            i
            for key, i in candidates.items()
            if key not in excluded_keys | dismissed
            and allowed(i, prefs)
            and not any(
                (i["kind"], normalized(i["title"]), year) in excluded_names
                for year in (i["year"], 0)
            )
        ]
        # Enrich a bounded shortlist via the same cached/rate-limited TMDB client.
        shortlist = sorted(candidates, key=lambda i: score(i, weights)[0], reverse=True)[:16]
        discoveries = sorted(
            [i for i in candidates if 20 <= i["metadata"].get("vote_count", 0) <= 1500],
            key=lambda i: i["metadata"].get("rating", 0),
            reverse=True,
        )[:8]
        shortlist = list({i["catalogue_key"]: i for i in [*shortlist, *discoveries]}.values())

        async def enrich(item):
            if (item.get("metadata") or {}).get("enriched") or not item["catalogue_key"].startswith(
                "tmdb:"
            ):
                return item
            try:
                return await self.tmdb.detail(item["catalogue_key"])
            except ToolError:
                return item

        rich = await asyncio.gather(*(enrich(i) for i in shortlist))
        rich_keys = {i["catalogue_key"] for i in rich}
        ranked = [
            {
                **i,
                "recommendation_score": round(score(i, weights)[0], 4),
                "reason": score(i, weights)[1],
            }
            for i in [*rich, *[i for i in candidates if i["catalogue_key"] not in rich_keys]]
        ]
        ranked.sort(key=lambda i: (-i["recommendation_score"], i["title"]))
        ranked = [i for i in ranked if allowed(i, prefs)]
        if genre:
            ranked = [
                i
                for i in ranked
                if normalized(genre) in {normalized(g) for g in i["metadata"].get("genres", [])}
            ]
        known_genres = {
            k.split(":", 1)[1]
            for k, value in weights.items()
            if k.startswith("genre:") and value > 0
        }
        pools = {
            "liked": ranked,
            "similar": [i for i in ranked if i["recommendation_score"] > 0.2] if seeds else [],
            "hidden": [
                i
                for i in ranked
                if i["metadata"].get("rating", 0) >= 7
                and 20 <= i["metadata"].get("vote_count", 0) <= 1500
            ],
            "mood": [
                i
                for i in ranked
                if not MOODS[mood]
                or MOODS[mood] in [g.casefold() for g in i["metadata"].get("genres", [])]
            ],
            "explore": [
                i
                for i in ranked
                if not known_genres.intersection(
                    normalized(g) for g in i["metadata"].get("genres", [])
                )
                and i["metadata"].get("rating", 0) >= 6.5
            ],
            "now": [i for i in ranked if i["catalogue_key"] in ready],
        }
        shelves = list(SHELVES)
        return {
            "samples": samples,
            "personalized": bool(samples),
            "mood": mood,
            "message": "Персонализация выключена. Показаны общие подборки по метаданным TMDB."
            if not prefs.personalize
            else "Мало оценок: рекомендации предварительные."
            if samples < 10
            else "Локальный подбор по твоим положительным и отрицательным оценкам.",
            "errors": list(dict.fromkeys(errors)),
            "shelves": [
                {
                    "id": key,
                    "name": name,
                    "items": [
                        {
                            **i,
                            "reason": "Другой жанр и хороший рейтинг TMDB — для знакомства с новым."
                            if key == "explore"
                            else "Сохранённый источник недавно прошёл проверку; "
                            "запуск проверим отдельно."
                            if key == "now"
                            else i["reason"],
                        }
                        for i in pools[key][:12]
                    ],
                }
                for key, name in shelves
            ],
        }
