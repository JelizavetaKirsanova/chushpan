"""Shared catalogue/recommendation policy; never filters stored personal lists."""

import re

from backend.cinema.models import CatalogueQuery

POLICY_FIELDS = ("russian_titles", "hide_animation", "hide_anime", "hide_documentary")
ANIMATION = {"animation", "мультфильм", "мультфильмы", "анимация"}
DOCUMENTARY = {"documentary", "документальный", "документальный фильм"}


def with_preferences(query, prefs):
    values = {key: getattr(prefs, key) for key in POLICY_FIELDS if getattr(query, key) is None}
    if "sort" not in query.model_fields_set:
        values["sort"] = prefs.default_sort
    return query.model_copy(update=values)


def allowed(item, rules):
    if isinstance(rules, CatalogueQuery) and rules.query.strip() and rules.relax_restrictions:
        return True
    meta = item.get("metadata") or {}
    genres = set(meta.get("genre_ids", []))
    names = {name.casefold() for name in meta.get("genres", [])}
    animated = 16 in genres or bool(names & ANIMATION)
    documentary = 99 in genres or bool(names & DOCUMENTARY)
    keywords = {name.casefold() for name in meta.get("keywords", [])}
    anime = any(
        word == "anime" or word == "аниме" or word.startswith("anime ") for word in keywords
    ) or (animated and "JP" in meta.get("countries", []))
    return not (
        (rules.russian_titles and not re.search(r"[А-Яа-яЁё]", item.get("title", "")))
        or (rules.hide_animation and animated)
        or (rules.hide_anime and anime)
        or (rules.hide_documentary and documentary)
    )
