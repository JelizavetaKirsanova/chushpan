"""Paged TMDB browsing; local library state is supplied separately, never inferred."""

import asyncio
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from backend.cinema.models import CatalogueQuery

FEEDS = [
    ("trending", "Сейчас в тренде"),
    ("popular_movies", "Популярные фильмы"),
    ("popular_series", "Популярные сериалы"),
    ("new", "Новинки"),
    ("upcoming", "Скоро выйдут"),
    ("top", "Лучшие по рейтингу"),
    ("scifi", "Фантастика"),
    ("comedy", "Комедии"),
    ("horror", "Ужасы"),
    ("thriller", "Триллеры"),
    ("mystery", "Детективы"),
    ("drama", "Драмы"),
    ("animation", "Мультфильмы"),
    ("recommended", "Рекомендовано для меня"),
]
GENRE_FEEDS = {
    "scifi": {"movie": 878, "tv": 10765},
    "comedy": {"movie": 35, "tv": 35},
    "horror": {"movie": 27},
    "thriller": {"movie": 53},
    "mystery": {"movie": 9648, "tv": 9648},
    "drama": {"movie": 18, "tv": 18},
    "animation": {"movie": 16, "tv": 16},
}


class Catalogue:
    def __init__(self, tmdb, *, today=None):
        self.tmdb = tmdb
        self.today = today or (lambda: datetime.now(UTC).date())
        self.page_counts = OrderedDict()

    async def configuration(self):
        movie, tv, countries, languages = await asyncio.gather(
            self.tmdb.genres("movie"),
            self.tmdb.genres("tv"),
            self.tmdb.get("configuration/countries"),
            self.tmdb.get("configuration/languages"),
        )
        return {
            "feeds": [{"id": key, "name": name} for key, name in FEEDS],
            "genres": sorted(set(movie.values()) | set(tv.values())),
            "genres_by_kind": {"movie": sorted(movie.values()), "series": sorted(tv.values())},
            "countries": [
                {"id": c["iso_3166_1"], "name": c.get("native_name") or c["english_name"]}
                for c in countries
                if isinstance(c, dict) and c.get("iso_3166_1")
            ],
            "languages": [
                {"id": c["iso_639_1"], "name": c.get("name") or c["english_name"]}
                for c in languages
                if isinstance(c, dict) and c.get("iso_639_1")
            ],
        }

    async def page(self, query: CatalogueQuery, *, seeds=(), excluded=(), excluded_titles=()):
        q = query
        if q.feed in {"top", "recommended"} and "sort" not in q.model_fields_set:
            q = q.model_copy(update={"sort": "rating"})
        kinds = ["movie", "tv"] if q.kind == "all" else ["tv" if q.kind == "series" else "movie"]
        if q.feed == "popular_movies":
            kinds = [k for k in kinds if k == "movie"]
        elif q.feed == "popular_series":
            kinds = [k for k in kinds if k == "tv"]
        if q.feed in GENRE_FEEDS:
            kinds = [k for k in kinds if k in GENRE_FEEDS[q.feed]]
        eligible = [key for key in seeds if self.tmdb.identity(key)[0] in kinds]
        personalized = q.feed == "recommended" and bool(eligible)
        if personalized:
            targets = []
            for key in eligible[:2]:
                kind, identifier = self.tmdb.identity(key)
                if kind in kinds:
                    targets.extend(
                        (kind, f"{kind}/{identifier}/{method}")
                        for method in ("recommendations", "similar")
                    )
        else:
            targets = [(kind, self.endpoint(q, kind)) for kind in kinds]

        async def fetch(kind, endpoint):
            genres = await self.tmdb.genres(kind)
            selected = set(q.genres) | ({q.genre.casefold()} if q.genre else set())
            genre = [key for key, name in genres.items() if name.casefold() in selected]
            # A genre missing from this type cannot satisfy AND; OR can use the rest.
            if selected and (not genre or (q.genre_mode == "and" and len(genre) < len(selected))):
                return [], 0, 0
            params = self.parameters(q, kind, endpoint, genre)
            count_key = (endpoint, tuple(sorted((k, v) for k, v in params.items() if k != "page")))
            known = self.page_counts.get(count_key)
            if known and known[0] > time.monotonic() and q.page > known[1]:
                return [], known[1], known[2]
            data = await self.tmdb.get(endpoint, **params)
            rows = []
            for raw in data.get("results", []):
                if raw.get("adult") or raw.get("media_type", kind) != kind:
                    continue
                row = raw
                # Search doesn't expose movie countries. Resolve just this bounded page
                # when that filter is selected; detail calls share the same rate/cache.
                if (
                    q.country
                    and not endpoint.startswith("discover/")
                    and not raw.get("origin_country")
                ):
                    row = {**raw, **await self.tmdb.get(f"{kind}/{raw['id']}")}
                if not self.matches(row, q, kind, genre, endpoint.startswith("discover/")):
                    continue
                rows.append(self.tmdb.metadata(row, kind, genres))
            pages = min(500, max(0, int(data.get("total_pages", 1))))
            total = int(data.get("total_results", len(rows)))
            self.page_counts[count_key] = (time.monotonic() + 900, pages, total)
            self.page_counts.move_to_end(count_key)
            while len(self.page_counts) > 80:
                self.page_counts.popitem(last=False)
            return rows, pages, total

        groups = await asyncio.gather(*(fetch(kind, endpoint) for kind, endpoint in targets))
        unique = {}
        excluded_titles = set(excluded_titles)
        for rows, _, _ in groups:
            for row in rows:
                title = " ".join(row["title"].casefold().split())
                if row["catalogue_key"] not in excluded and not any(
                    (row["kind"], title, year) in excluded_titles for year in (row["year"], 0)
                ):
                    unique.setdefault(row["catalogue_key"], row)
        items = list(unique.values())
        self.sort(items, q.sort)
        pages = max((group[1] for group in groups), default=0)
        # Raw totals precede local search/recommendation filters; never claim an exact
        # filtered total or stop at an empty page while the API still has more pages.
        local_filters = bool(q.query or personalized or q.feed == "trending")
        return {
            "feed": q.feed,
            "page": q.page,
            "items": items,
            "total_pages": pages,
            "total_results": sum(g[2] for g in groups),
            "next_page": q.page + 1 if q.page < pages else None,
            "sort_scope": "page" if local_filters or len(targets) > 1 else "global",
            "filtered_total": not (q.query or personalized or excluded),
            "message": "На основе твоих оценок, избранного и истории."
            if personalized
            else "Пока мало истории: показываю лучшие по рейтингу. "
            "Отмечай понравившееся для персональных рекомендаций."
            if q.feed == "recommended"
            else "",
        }

    def endpoint(self, q, kind):
        if q.query.strip():
            return f"search/{kind}"
        if q.feed == "trending":
            return f"trending/{kind}/week"
        if q.feed in {"popular_movies", "popular_series"}:
            if (
                q.sort != "popularity"
                or q.genre
                or q.genres
                or q.year
                or q.country
                or q.original_language
                or q.rating
            ):
                return f"discover/{kind}"
            return f"{kind}/popular"
        if q.feed in {"top", "recommended"}:
            return f"discover/{kind}" if q.feed == "top" else f"{kind}/top_rated"
        return f"discover/{kind}"

    def parameters(self, q, kind, endpoint, genre):
        params = {"page": q.page, "include_adult": "false"}
        if endpoint.startswith("search/"):
            params["query"] = q.query.strip()
            if q.year:
                params["year" if kind == "movie" else "first_air_date_year"] = q.year
        if endpoint.startswith("discover/"):
            params["sort_by"] = {
                "popularity": "popularity.desc",
                "rating": "vote_average.desc",
                "date": "primary_release_date.desc" if kind == "movie" else "first_air_date.desc",
                "title": "title.asc" if kind == "movie" else "name.asc",
            }[q.sort]
            params["vote_average.gte"] = q.rating
            if q.sort == "rating" or q.feed == "top":
                params["vote_count.gte"] = 100
            genre_ids = set(genre)
            if genre_ids:
                separator = "," if q.genre_mode == "and" else "|"
                params["with_genres"] = separator.join(str(g) for g in sorted(genre_ids))
            # A shelf genre is an additional constraint, independent of the user's OR.
            # TMDB has no parenthesized genre expressions; filter that case locally.
            if q.feed in GENRE_FEEDS and (not genre_ids or q.genre_mode == "and"):
                genre_ids.add(GENRE_FEEDS[q.feed][kind])
                params["with_genres"] = ",".join(str(g) for g in sorted(genre_ids))
            if q.year:
                params["primary_release_year" if kind == "movie" else "first_air_date_year"] = (
                    q.year
                )
            if q.country:
                params["with_origin_country"] = q.country
            if q.original_language:
                params["with_original_language"] = q.original_language
            if q.feed in {"new", "upcoming"}:
                date = "primary_release_date" if kind == "movie" else "first_air_date"
                if q.feed == "new":
                    params[f"{date}.gte"] = str(self.today() - timedelta(days=90))
                    params[f"{date}.lte"] = str(self.today())
                else:
                    params[f"{date}.gte"] = str(self.today() + timedelta(days=1))
                    params[f"{date}.lte"] = str(self.today() + timedelta(days=365))
        return params

    @staticmethod
    def matches(row, q, kind, genre, discovered=False):
        if (row.get("vote_average") or 0) < q.rating:
            return False
        ids = row.get("genre_ids") or [g["id"] for g in row.get("genres", [])]
        if genre and not (
            all(g in ids for g in genre) if q.genre_mode == "and" else any(g in ids for g in genre)
        ):
            return False
        if q.feed in GENRE_FEEDS and GENRE_FEEDS[q.feed].get(kind) not in ids:
            return False
        if q.original_language and row.get("original_language") != q.original_language:
            return False
        date = row.get("release_date" if kind == "movie" else "first_air_date") or ""
        if q.year and date[:4] != str(q.year):
            return False
        countries = row.get("origin_country") or [
            c["iso_3166_1"] for c in row.get("production_countries", [])
        ]
        # Discover already applies country on the server; its movie list omits it.
        if q.country and not discovered and q.country not in countries:
            return False
        return True

    @staticmethod
    def sort(items, sort):
        key = {
            "popularity": lambda item: item["metadata"]["popularity"],
            "rating": lambda item: item["metadata"]["rating"],
            "date": lambda item: item["metadata"]["release_date"],
            "title": lambda item: item["title"].casefold(),
        }[sort]
        items.sort(key=key, reverse=sort != "title")
