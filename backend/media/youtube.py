"""YouTube Music data via ytmusicapi; playback stays in the Safari Web App."""

from __future__ import annotations

import asyncio
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from backend.config import ROOT_DIR, Settings
from backend.media.safari import ORIGIN, SafariMusic, label, player_nodes
from backend.memory.store import MemoryStore
from backend.tools.registry import Tool, ToolError, user_text


def normal(text: str) -> str:
    text = "".join(
        c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c)
    ).replace("ё", "е")
    text = text.replace("favourite", "favorite")
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def music_url(kind: str, item_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,160}", item_id):
        raise ToolError("Некорректный ID музыки.")
    if kind == "song":
        return f"{ORIGIN}/watch?{urlencode({'v': item_id})}"
    if kind == "playlist":
        return f"{ORIGIN}/playlist?{urlencode({'list': item_id})}"
    if kind in ("album", "artist"):
        return f"{ORIGIN}/{'browse' if kind == 'album' else 'channel'}/{item_id}"
    raise ToolError("Неизвестный тип музыки.")


def candidates(query: str, items: list[dict]) -> dict:
    q = normal(query)
    exact = [
        i
        for i in items
        if normal(i.get("title", "")) == q
        or i.get("id") == query
        or normal(i.get("title", "") + " " + i.get("artist", "")) == q
    ]
    if len(exact) == 1:
        return {"item": exact[0]}
    ranked = (
        exact
        or sorted(
            items,
            key=lambda i: SequenceMatcher(
                None, q, normal(i.get("title", "") + " " + i.get("artist", ""))
            ).ratio(),
            reverse=True,
        )[:5]
    )
    return {
        "status": "choose" if ranked else "not_found",
        "candidates": ranked,
        "message": "Уточни выбор у пользовательницы. Не включай музыку до её ответа.",
    }


def scenario_key(text: str) -> str:
    value = normal(text) or "default"
    aliases = {
        "default": ("default", "основной", "моя музыка", "по умолчанию", "мой плейлист"),
        "study": ("study", "учеб", "учеба"),
        "work": ("work", "работ"),
        "rest": ("rest", "отдых", "вечер", "спокой"),
        "sleep": ("sleep", "сон", "сна"),
        "mood": ("mood", "настроени"),
        "travel": ("travel", "дорог", "поездк"),
        "favourites": ("favourites", "favorites", "любим"),
    }
    for key, words in aliases.items():
        if any(w in value for w in words):
            return key
    return value


class MusicAPI:
    def __init__(self, s: Settings) -> None:
        self.config = s.media.youtube_music

    async def call(self, method: str, *args, **kwargs):
        auth_file = self.config.auth_file
        if auth_file:
            auth_file = Path(auth_file).expanduser()
            if not auth_file.is_absolute():
                auth_file = ROOT_DIR / auth_file
            if not auth_file.is_file():
                raise ToolError(
                    "Файл авторизации YouTube Music не найден. Секреты в чат не присылай."
                )

        def fetch():
            import requests
            from ytmusicapi import YTMusic

            class Session(requests.Session):
                def request(self, *a, **kw):
                    kw["timeout"] = min(self_timeout, 8)
                    return super().request(*a, **kw)

            self_timeout = self.config.timeout_s
            with Session() as session:
                api = YTMusic(
                    str(auth_file) if auth_file else None, language="ru", requests_session=session
                )
                return getattr(api, method)(*args, **kwargs)

        try:
            return await asyncio.wait_for(asyncio.to_thread(fetch), self.config.timeout_s)
        except Exception as exc:
            # Do not log exception bodies, request headers or credentials.
            if auth_file:
                raise ToolError(
                    "YouTube Music не ответил: проверь соединение и локальную авторизацию."
                ) from None
            raise ToolError(
                "Поиск YouTube Music недоступен: проверь интернет и повтори запрос."
            ) from exc


class YouTubeMusic:
    def __init__(
        self,
        s: Settings,
        store: MemoryStore | None = None,
        app: SafariMusic | None = None,
        api: MusicAPI | None = None,
    ) -> None:
        self.s, self.store = s, store
        self.app, self.api = app or SafariMusic(s), api or MusicAPI(s)
        self.lock = asyncio.Lock()

    def preference(self, purpose: str, value: dict | None = None) -> dict | None:
        store = self.store or MemoryStore(self.s.db_path)
        try:
            if value is not None:
                store.set_preference("youtube_music", purpose, value)
            return store.preference("youtube_music", purpose)
        finally:
            if self.store is None:
                store.close()

    async def library(self) -> dict:
        if self.s.media.youtube_music.auth_file:
            data = await self.api.call("get_library_playlists", limit=None)
            return {
                "playlists": [
                    {"id": p["playlistId"], "title": p["title"], "kind": "playlist"}
                    for p in data
                    if p.get("playlistId")
                ],
                "source": "ytmusicapi",
            }
        async with self.lock:
            nodes = await self.app.library_page()
            items = {}
            for n in nodes:
                if n.get("role") != "AXLink" or "/playlist?" not in n.get("url", ""):
                    continue
                title = label(n)
                if title.casefold() in ("меню действий", "action menu"):
                    continue
                ids = parse_qs(urlparse(n["url"]).query).get("list", [])
                if ids and title:
                    items[ids[0]] = {
                        "id": ids[0],
                        "title": title.split("\n")[0],
                        "kind": "playlist",
                    }
            return {
                "playlists": list(items.values()),
                "source": "safari_accessibility",
                "complete": False,
                "note": "Видимые плейлисты; длинный список может быть загружен частично.",
            }

    async def search(self, query: str, kind: str = "song") -> dict:
        if not query.strip():
            raise ToolError("Назови, что найти.")
        if kind in ("playlist", "library"):
            data = await self.library()
            return {**data, **candidates(query, data["playlists"])}
        filters = {"song": "songs", "artist": "artists", "album": "albums"}
        if kind not in filters:
            raise ToolError("Тип поиска: song, artist, album, playlist или library.")
        try:
            results = await self.api.call("search", query, filter=filters[kind], limit=6)
        except ToolError:
            if self.s.media.youtube_music.auth_file:
                raise
            results = []
        items = []
        for r in results[:6]:
            item_id = r.get("videoId") if kind == "song" else r.get("browseId")
            if not item_id:
                continue
            artists = ", ".join(a["name"] for a in r.get("artists", []) if a.get("name"))
            items.append(
                {
                    "id": item_id,
                    "title": r.get("title") or r.get("artist", ""),
                    "artist": artists,
                    "kind": kind,
                }
            )
        source = "ytmusicapi"
        if not items:
            async with self.lock:
                items = await self.app_search(query, kind)
            source = "safari_accessibility"
        return {"results": items, "source": source, **candidates(query, items)}

    async def app_search(self, query: str, kind: str) -> list[dict]:
        nodes = await self.app.search(query)
        for _ in range(8):
            bar = player_nodes(nodes)
            links = [
                n
                for n in nodes
                if n.get("role") == "AXLink"
                and n not in bar
                and label(n)
                and label(n).casefold() != "меню действий"
            ]
            items = {}
            for n in links:
                url = urlparse(n.get("url", ""))
                item_id = ""
                if kind == "song" and url.path == "/watch":
                    item_id = parse_qs(url.query).get("v", [""])[0]
                elif kind == "artist" and url.path.startswith("/channel/"):
                    item_id = url.path.rsplit("/", 1)[-1]
                elif kind == "album" and url.path.startswith("/browse/MPRE"):
                    item_id = url.path.rsplit("/", 1)[-1]
                if not item_id:
                    continue
                artist = ""
                if kind != "artist":
                    for length in range(len(n["path"]) - 1, max(0, len(n["path"]) - 5), -1):
                        related = [
                            a
                            for a in links
                            if "/channel/" in a.get("url", "")
                            and a["path"][:length] == n["path"][:length]
                        ]
                        if related:
                            artist = label(related[0])
                            break
                items[item_id] = {"id": item_id, "title": label(n), "artist": artist, "kind": kind}
            if items:
                return list(items.values())[:8]
            await asyncio.sleep(0.25)
            nodes = await self.app.snapshot()
        return []

    async def playlist(
        self,
        action: str = "list",
        query: str = "",
        playlist_id: str = "",
        situation: str = "default",
    ) -> dict:
        purpose = scenario_key(situation)
        if action == "list":
            return await self.library()
        if action == "get":
            known = self.preference(purpose)
            return {
                "situation": purpose,
                "playlist": known,
                "message": "Сначала назначь плейлист." if not known else "",
            }
        if action == "tracks":
            if not playlist_id:
                raise ToolError("Нужен ID плейлиста из библиотеки.")
            music_url("playlist", playlist_id)
            try:
                data = await self.api.call("get_playlist", playlist_id, limit=100)
            except ToolError:
                if self.s.media.youtube_music.auth_file:
                    raise
                async with self.lock:
                    nodes = await self.app.navigate(music_url("playlist", playlist_id))
                    tracks = {}
                    for node in nodes:
                        if node.get("role") != "AXLink":
                            continue
                        video = parse_qs(urlparse(node.get("url", "")).query).get("v", [])
                        title = label(node)
                        if video and title and title.casefold() != "меню действий":
                            tracks[video[0]] = {"id": video[0], "title": title}
                    return {
                        "tracks": list(tracks.values()),
                        "complete": False,
                        "source": "safari_accessibility",
                    }
            return {
                "title": data.get("title", ""),
                "tracks": [
                    {
                        "id": t.get("videoId"),
                        "title": t.get("title"),
                        "artist": ", ".join(a["name"] for a in t.get("artists", [])),
                    }
                    for t in data.get("tracks", [])
                ],
            }
        if action != "assign":
            raise ToolError("Действие: list, get, tracks или assign.")
        text = user_text.get() or ""
        requested = re.search(
            r"\b(?:запомни|назначь|сделай|поменяй|замени|поставь|выбери)\b", text, re.I
        )
        if not requested or re.search(
            r"\bне\s+(?:запоминай|назначай|делай|меняй|заменяй|ставь|выбирай)\b", text, re.I
        ):
            raise ToolError("Назначать плейлист можно только по явной просьбе пользовательницы.")
        if not query and not playlist_id:
            nodes = await self.app.snapshot()
            url = next((n.get("url", "") for n in nodes if n.get("role") == "AXWebArea"), "")
            playlist_id = parse_qs(urlparse(url).query).get("list", [""])[0]
        data = await self.library()
        choice = candidates(playlist_id or query, data["playlists"])
        if "item" not in choice:
            return choice
        item = {**choice["item"], "purpose": purpose}
        self.preference(purpose, item)
        return {"ok": True, "situation": purpose, "playlist": item}

    async def play(
        self,
        query: str = "",
        kind: str = "auto",
        item_id: str = "",
        situation: str = "",
        open_only: bool = False,
    ) -> dict:
        said = normal(user_text.get() or "")
        if situation and said:
            requested = {
                "study": r"учеб|учиться",
                "work": r"работ",
                "rest": r"отдых|вечер|спокой",
                "sleep": r"сон|сна|спать",
                "mood": r"настроени",
                "travel": r"дорог|поездк",
                "favourites": r"любим",
            }
            key = scenario_key(situation)
            pattern = requested.get(key)
            if (pattern and not re.search(pattern, said)) or (
                not pattern and normal(situation) not in said
            ):
                situation = ""  # a model's guess (e.g. time of day) isn't the user's choice
        q = normal(query)
        use_default = (not q and not item_id) or q in (
            "моя музыка",
            "мою музыку",
            "музыку",
            "музыка",
            "мой плейлист",
            "мои любимые песни",
            "любимые песни",
            "что нибудь мое",
        )
        if situation or use_default:
            purpose = scenario_key(situation)
            known = self.preference(purpose)
            configured = (
                self.s.media.youtube_music.main_playlist
                if purpose == "default"
                else self.s.media.youtube_music.scenarios.get(purpose)
            )
            query = (known or {}).get("id") or (
                configured.id or configured.title if configured else ""
            )
            if not query:
                return {
                    "status": "choose",
                    **await self.library(),
                    "message": "Попроси выбрать плейлист для этого сценария; не выбирай сама.",
                }
            kind, item_id = "playlist", ""
        if kind in ("auto", "playlist"):
            lib = await self.library()
            choice = candidates(item_id or query, lib["playlists"])
            if "item" in choice:
                item = choice["item"]
                if use_default or situation:
                    self.preference(
                        scenario_key(situation), {**item, "purpose": scenario_key(situation)}
                    )
                async with self.lock:
                    if open_only:
                        await self.app.navigate(music_url("playlist", item["id"]))
                        return {"ok": True, "action": "opened", "playlist": item}
                    return await self.app.play_page(music_url("playlist", item["id"]))
            if kind == "playlist":
                if (situation or use_default) and known:
                    return {
                        "status": "unavailable",
                        "playlist": known,
                        "message": "Назначенный плейлист не найден в доступной библиотеке.",
                    }
                return choice
            kind = "song"
        if item_id:
            # IDs are accepted only after a fresh search resolves the same item.
            result = await self.search(query, kind)
            choice = next((r for r in result.get("results", []) if r["id"] == item_id), None)
            if not choice:
                raise ToolError("ID не найден в результатах поиска. Уточни название.")
            item = choice
        else:
            result = await self.search(query, kind)
            if "item" not in result:
                return result
            item = result["item"]
        async with self.lock:
            if open_only:
                await self.app.navigate(music_url(kind, item["id"]))
                return {"ok": True, "action": "opened", "item": item}
            if kind == "artist":
                # Radio browse IDs aren't necessarily navigable playlist pages.
                return await self.app.play_page(music_url("artist", item["id"]), item["title"])
            if kind == "album":
                try:
                    album = await self.api.call("get_album", item["id"])
                except ToolError:
                    album = {}
                playlist_id = album.get("audioPlaylistId")
                if not playlist_id:
                    return await self.app.play_page(music_url("album", item["id"]), item["title"])
                return await self.app.play_page(music_url("playlist", playlist_id))
            return await self.app.play_page(
                music_url(kind, item["id"]), item["title"], kind == "song"
            )

    async def volume(
        self, direction: str = "", level: int | None = None, change: int | None = None
    ) -> dict:
        said = normal(user_text.get() or "")
        if said and not re.search(r"\d|процент|\bдо\b", said):
            if re.search(r"тише|убав|уменьш|сниз", said):
                direction, level, change = "down", None, None
            elif re.search(r"громче|прибав|увелич|повысь", said):
                direction, level, change = "up", None, None
        if direction not in ("", "up", "down"):
            raise ToolError("Направление громкости: up или down.")
        if level is None and not direction and change is None:
            raise ToolError("Назови уровень громкости или направление изменения.")
        async with self.lock:
            current = (await self.app.now_playing()).get("volume")
            if current is None:
                raise ToolError("Громкость YouTube Music недоступна.")
            if direction:
                level = current + (1 if direction == "up" else -1) * abs(change or 10)
            elif level is None:
                level = current + (change or 0)
            return await self.app.set_volume(level)

    async def control(self, action: str) -> dict:
        async with self.lock:
            return await self.app.control(action)

    async def seek(self, seconds: float, relative: bool = False) -> dict:
        async with self.lock:
            return await self.app.seek(seconds, relative)

    def tools(self) -> list[Tool]:
        kinds = {"type": "string", "enum": ["song", "artist", "album", "playlist"]}
        cap = {
            "ru": "управлять YouTube Music, искать музыку и назначать плейлисты для разных ситуаций"
        }
        return [
            Tool(
                "play_music",
                "Включить музыку в YouTube Music. Без названия или «мою музыку» — "
                "основной плейлист. kind=artist для исполнителя, album для альбома, playlist для "
                "своего плейлиста; situation — только явно названная ситуация, "
                "study/work/rest/sleep/mood/travel/favourites. «Мою музыку» — основной плейлист, "
                "не выбирай сценарий по времени суток. Не передавай ненужные параметры. "
                "При status=choose уточни выбор. Не объявляй успех без ok=true.",
                self.play,
                {
                    "query": {
                        "type": "string",
                        "description": "Исходное название для поиска; "
                        "при выборе item_id сохрани тот же поисковый запрос",
                    },
                    "kind": {"type": "string", "enum": ["auto", *kinds["enum"]]},
                    "item_id": {"type": "string", "description": "ID из результатов поиска"},
                    "situation": {"type": "string"},
                    "open_only": {
                        "type": "boolean",
                        "description": "Только открыть страницу, без воспроизведения",
                    },
                },
                capability=cap,
                slow=True,
            ),
            Tool(
                "music_search",
                "Найти песню, исполнителя, альбом или свой плейлист YouTube Music. "
                "Верни варианты при неоднозначности; поиск сам не включает музыку.",
                self.search,
                {"query": {"type": "string"}, "kind": kinds},
                required=["query"],
                slow=True,
            ),
            Tool(
                "music_playlist",
                "Свои плейлисты: action=list — список; tracks — содержимое по "
                "playlist_id; get — назначение situation; assign — назначить основной или сценарий "
                "только по прямой просьбе. Передай query (название) или playlist_id из списка. "
                "Для «этот плейлист» оба поля пустые. Не выдумывай и не озвучивай ID; "
                "назови только названия плейлистов.",
                self.playlist,
                {
                    "action": {"type": "string", "enum": ["list", "tracks", "get", "assign"]},
                    "query": {"type": "string"},
                    "playlist_id": {"type": "string"},
                    "situation": {"type": "string"},
                },
                slow=True,
            ),
            Tool(
                "media_control",
                "Кнопки YouTube Music. stop останавливает звук паузой, resume "
                "продолжает. open открывает приложение. Не используй глобальные медиаклавиши.",
                self.control,
                {
                    "action": {
                        "type": "string",
                        "enum": [
                            "open",
                            "pause",
                            "stop",
                            "resume",
                            "next",
                            "previous",
                            "shuffle_on",
                            "shuffle_off",
                            "repeat_on",
                            "repeat_off",
                            "repeat_one",
                            "like",
                        ],
                    }
                },
                required=["action"],
            ),
            Tool(
                "now_playing",
                "Что сейчас играет в YouTube Music: свежие данные плеера.",
                self.app.now_playing,
            ),
            Tool(
                "media_seek",
                "Перемотать трек YouTube Music. seconds — секунды от начала, "
                "relative=true — сдвиг от текущего места (назад отрицательное число).",
                self.seek,
                {"seconds": {"type": "number"}, "relative": {"type": "boolean"}},
                required=["seconds"],
            ),
            Tool(
                "media_volume",
                "Громкость музыки в YouTube Music, отдельно от set_volume (громкость "
                "Mac). level — только если пользовательница назвала процент; иначе direction.",
                self.volume,
                {
                    "direction": {"type": "string", "enum": ["up", "down"]},
                    "level": {"type": "integer"},
                    "change": {"type": "integer"},
                },
            ),
        ]
