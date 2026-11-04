"""No live websites, microphone, Safari or physical devices are touched by these tests."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.cinema.adapters import HTML5Adapter, ManualAdapter, SafariBridge
from backend.cinema.commands import command
from backend.cinema.models import Entry
from backend.cinema.service import Cinema
from backend.cinema.store import MIGRATIONS, CinemaStore
from backend.config import Settings
from backend.context import DeviceContext, RequestContext, request_scope, voice_addressed
from backend.hud.bus import HudBus
from backend.hud.server import create_app
from backend.mac_agent import LocalMacAgent
from backend.memory.store import MemoryStore
from backend.tools.registry import ToolError, ToolRegistry


def seed(store, adapter="html5"):
    source = store.save_source(
        {"name": "Локальный HTML5", "base_url": "http://localhost:8765", "adapter": adapter}
    )
    entry = {
        "title": "Тестовый сериал",
        "year": 2026,
        "source_id": source["id"],
        "url": "http://localhost:8765/one.html",
    }
    item = store.add(entry)
    store.add({**entry, "episode": 2, "url": "http://localhost:8765/two.html"})
    return store.detail(item["id"])


def test_persistent_migration_dedup_and_episode_progress(tmp_path):
    path = tmp_path / "assistant.sqlite"
    memory = MemoryStore(path)
    original = memory.new_conversation()
    store = CinemaStore(path)
    item = seed(store)
    second_source = store.save_source(
        {"name": "Другой источник", "base_url": "https://example.org"}
    )
    same = store.add(
        {
            "title": "Тестовый  СЕРИАЛ",
            "year": 2026,
            "source_id": second_source["id"],
            "url": "https://example.org/one",
            "voice": "Русская",
        }
    )
    assert same["id"] == item["id"]
    ep1, ep2 = item["episodes"]
    alternate = store.detail(item["id"])["episodes"][0]["links"][1]
    store.progress(ep1["id"], 127, 1000, "pause", alternate["id"])
    store.progress(ep2["id"], 950, 1000, "ended", ep2["links"][0]["id"])
    store.close()
    reopened = CinemaStore(path)
    resumed = reopened.target(item["id"], ep1["id"])
    assert resumed["episode"]["position"] == 127
    assert resumed["link"]["id"] == alternate["id"]
    assert reopened.latest()["current"]["id"] == ep2["id"]
    assert reopened.detail(item["id"])["status"] == "watching"
    assert reopened.detail(item["id"])["episodes"][1]["watched"]
    assert memory.db.execute("SELECT id FROM conversations").fetchone()[0] == original
    assert len(reopened.library("СЕРИАЛ")) == 1
    assert len(reopened.history()) == 2
    assert reopened.db.execute("SELECT COUNT(*) FROM cinema_migrations").fetchone()[0] == len(
        MIGRATIONS
    )
    reopened.close()
    memory.close()


def test_validation_threshold_selection_and_source_boundaries(tmp_path):
    store = CinemaStore(tmp_path / "assistant.sqlite")
    item = seed(store)
    first, second = item["episodes"]
    for position, duration in [(float("nan"), 1), (float("inf"), 1), (-1, 1), (200, 1), (True, 1)]:
        with pytest.raises(ValueError):
            store.progress(first["id"], position, duration)
    with pytest.raises(ValueError):
        store.progress(first["id"], 1, 2, link_id=second["links"][0]["id"])
    with pytest.raises(ValueError):
        store.add(
            {
                "title": "Другой",
                "source_id": first["links"][0]["source_id"],
                "url": "https://other.example/",
            }
        )
    with pytest.raises(ValueError):
        store.configure({"auto_next": "false"})
    store.configure({"watched_threshold": 0.9})
    store.progress(first["id"], 901, 1000)
    assert store.detail(item["id"])["episodes"][0]["watched"]
    assert store.next_target(item["id"], first["id"])["episode"]["id"] == second["id"]
    with pytest.raises(ValueError):
        store.next_target(item["id"], second["id"])
    for url in ["file:///etc/passwd", "javascript:alert(1)", "https://user:pass@example.org"]:
        with pytest.raises(ValueError):
            Entry(title="X", source_id="x", url=url)
    for url in ["http://example.org/a.png", "https://127.0.0.1/a.png"]:
        with pytest.raises(ValueError):
            Entry(title="X", source_id="x", url="https://example.org", poster_url=url)
    store.close()


@pytest.mark.parametrize(
    ("text", "active", "expected"),
    [
        ("Чушпан, продолжи мой сериал", False, {"action": "continue"}),
        ("Продолжи то, что я смотрела", False, {"action": "continue"}),
        (
            "Продолжи сериал и включи на телевизоре",
            False,
            {"action": "continue", "device": "airplay"},
        ),
        ("Поставь фильм на паузу", True, {"action": "pause"}),
        ("Продолжай", True, {"action": "play"}),
        ("Продолжай", False, None),
        ("Перемотай на пять минут", True, {"action": "seek", "seconds": 300}),
        ("Верни на тридцать секунд назад", True, {"action": "seek", "seconds": -30}),
        ("Включи это на телевизоре", True, {"action": "tv", "device": "airplay"}),
        ("Не включи сериал", True, None),
        ("Он сказал «продолжи сериал»", True, None),
        ("Продолжи сериал и удали файлы", True, None),
        ("Включи это", True, None),
    ],
)
def test_local_commands(text, active, expected):
    assert command(text, active) == expected


class FakePlayer:
    def __init__(self):
        self.position, self.duration, self.paused = 120, 1000, False
        self.opened = []

    async def open(self, target):
        self.opened.append(target)
        self.position = target["episode"]["position"]
        return {**await self.sample(), "playback_confirmed": True, "message": "Подтверждено"}

    async def sample(self):
        return {
            "position": self.position,
            "duration": self.duration,
            "paused": self.paused,
            "ended": False,
        }

    async def control(self, action, position=0):
        if action == "seek":
            self.position = position
        self.paused = action == "pause"
        return {
            **await self.sample(),
            "message": "Проверено",
            "playback_confirmed": action == "play",
        }

    async def detach(self):
        pass


def test_service_restore_switch_pause_telemetry_permissions_and_airplay(tmp_path):
    async def run():
        memory = MemoryStore(tmp_path / "assistant.sqlite")
        tools = ToolRegistry()
        tools.mac_agent = LocalMacAgent()
        player = FakePlayer()
        service = Cinema(tools, Settings(), memory, adapter_factory=lambda _: player)
        item = await service.db("library")
        assert item == []
        item = await asyncio.get_running_loop().run_in_executor(service.worker, seed, service.store)
        ep = item["episodes"][0]
        await service.db("progress", ep["id"], 321, 1000, "manual", ep["links"][0]["id"])
        bus = HudBus()
        conv = SimpleNamespace(device=DeviceContext.local_mac())
        service.bind(bus, conv)
        bus.cinema_url = "http://127.0.0.1:9000"
        with request_scope(RequestContext(conv.device, "Продолжи мой сериал")):
            result = json.loads(
                await tools.call("cinema_command", {"command": "Продолжи мой сериал"})
            )
        assert result["playback_confirmed"]
        assert player.opened[-1]["episode"]["position"] == 318
        token = service.session["token"]
        event = {"sequence": 1, "position": 422, "duration": 1000, "event": "pause"}
        assert not await service.telemetry("bad", "http://localhost:8765", event)
        assert not await service.telemetry(token, "https://unrelated.example", event)
        assert await service.telemetry(token, "http://localhost:8765", event)
        assert not await service.telemetry(token, "http://localhost:8765", event)
        assert (await service.db("target", item["id"]))["episode"]["position"] == 422
        with request_scope(RequestContext(conv.device, "Поставь фильм на паузу")):
            result = json.loads(
                await tools.call("cinema_command", {"command": "Поставь фильм на паузу"})
            )
        assert player.paused
        with request_scope(RequestContext(conv.device, "Включи следующую серию")):
            result = json.loads(
                await tools.call("cinema_command", {"command": "Включи следующую серию"})
            )
        assert result["playback_confirmed"]
        assert player.opened[-1]["episode"]["episode"] == 2
        before = len(player.opened)
        with request_scope(RequestContext(conv.device, "Продолжи сериал и включи на телевизоре")):
            result = json.loads(
                await tools.call(
                    "cinema_command", {"command": "Продолжи сериал и включи на телевизоре"}
                )
            )
        assert result["playback_confirmed"] is False
        assert len(player.opened) == before
        # A registry refusal must reach the HUD instead of disappearing in a snapshot.
        memory.set_preference("settings", "access", {"disabled_tools": ["cinema_command"]})
        await service.handle({"op": "control", "command": "Поставь фильм на паузу"})
        assert next(event for event in bus.snapshot() if event["type"] == "cinema")["error"]
        memory.set_preference("settings", "access", {"disabled_tools": []})
        denied = voice_addressed.set(False)
        try:
            with request_scope(RequestContext(conv.device, "Продолжи мой сериал")):
                result = json.loads(
                    await tools.call("cinema_command", {"command": "Продолжи мой сериал"})
                )
            assert result.get("error")
        finally:
            voice_addressed.reset(denied)
        active_source = await service.db("source", service.session["target"]["source"]["id"])
        await service.handle(
            {"op": "source", "source": {**active_source.model_dump(), "enabled": False}}
        )
        assert service.session is None
        await service.close()
        memory.close()

    asyncio.run(run())


def test_html5_adapter_requires_time_advancing_and_restores_position():
    class Bridge:
        def __init__(self):
            self.position, self.paused, self.advance = 0, True, True

        async def evaluate(self, url, request):
            if request["action"] == "seek":
                self.position = request["position"]
            if request["action"] == "play":
                self.paused = False
            if not self.paused and self.advance:
                self.position += 0.5
            return {"position": self.position, "duration": 1000, "paused": self.paused}

    async def run():
        bridge = Bridge()
        adapter = HTML5Adapter(bridge)
        result = await adapter.open(
            {"link": {"url": "https://example.org/one"}, "episode": {"position": 300}}
        )
        assert result["playback_confirmed"]
        assert bridge.position > 300
        # A static play button / paused=false alone is not sufficient proof.
        bridge.advance = False
        with pytest.raises(ToolError, match="не подтвердил"):
            await adapter.control("play")

    asyncio.run(run())


def test_manual_and_safari_url_binding():
    async def run():
        opened = []

        async def opener(url):
            opened.append(url)

        manual = ManualAdapter(opener)
        result = await manual.open({"link": {"url": "https://example.org/one"}})
        assert opened == ["https://example.org/one"]
        assert not result["playback_confirmed"]
        scripts = []

        class Executor:
            async def _run(self, script):
                scripts.append(script)
                return '{"position":1,"duration":2}'

        await SafariBridge(Settings(), Executor()).evaluate(
            "https://example.org/one", {"action": "status"}
        )
        assert 'URL of t is "https://example.org/one"' in scripts[0]
        assert "count of matches) is not 1" in scripts[0]
        assert "current tab" not in scripts[0]

    asyncio.run(run())


def test_progress_endpoint_is_capability_scoped_and_size_limited():
    calls = []

    class Receiver:
        async def telemetry(self, token, origin, data):
            calls.append((token, origin, data))
            return token == "allowed" and origin == "https://example.org"

    bus = HudBus()
    bus.cinema = Receiver()
    with TestClient(create_app(bus, {"http://127.0.0.1:8765"})) as client:
        assert (
            client.post(
                "/cinema/progress/bad", headers={"Origin": "https://example.org"}, json={}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/cinema/progress/allowed", headers={"Origin": "https://example.org"}, json={}
            ).status_code
            == 204
        )
        assert client.post("/cinema/progress/allowed", content="x" * 5000).status_code == 413
        assert client.post("/cinema/progress/allowed", content="not json").status_code == 403
    assert len(calls) == 2


def test_cinema_storage_failure_does_not_break_the_assistant(tmp_path, monkeypatch):
    import backend.cinema.service as module

    real_store = module.CinemaStore

    def failing_disk(path):
        if str(path) != ":memory:":
            raise sqlite3.OperationalError("test: read-only cinema tables")
        return real_store(path)

    monkeypatch.setattr(module, "CinemaStore", failing_disk)

    async def run():
        memory = MemoryStore(tmp_path / "assistant.sqlite")
        tools = ToolRegistry()
        tools.mac_agent = LocalMacAgent()
        service = module.Cinema(tools, Settings(), memory)
        assert memory.memory_available and not service.available
        assert "cinema_command" in tools
        with pytest.raises(ToolError, match="SQLite"):
            await service.execute("Продолжи мой сериал")
        await service.close()
        memory.close()

    asyncio.run(run())


def test_v1_upgrade_preserves_library_links_history_and_memory(tmp_path, monkeypatch):
    import backend.cinema.store as module

    path = tmp_path / "existing.sqlite"
    monkeypatch.setattr(module, "MIGRATIONS", MIGRATIONS[:1])
    old = CinemaStore(path)
    # Seed v1 directly: the v2 store methods intentionally require the new schema.
    old.db.execute(
        "INSERT INTO cinema_titles VALUES "
        "('old','movie:2026:old','Old','movie',2026,'','','','watched',1,'ep','2026-11-01')"
    )
    old.db.execute(
        "INSERT INTO cinema_sources VALUES ('source',?)",
        (
            json.dumps(
                {
                    "id": "source",
                    "name": "Official",
                    "base_url": "https://example.org",
                }
            ),
        ),
    )
    old.db.execute(
        "INSERT INTO cinema_episodes VALUES ('ep','old',0,0,90,100,1,'2026-11-01','link')"
    )
    old.db.execute(
        "INSERT INTO cinema_links VALUES ('link','ep','source','https://example.org/film','')"
    )
    old.db.execute("INSERT INTO cinema_history VALUES (1,'ep','link',90,100,'pause','2026-11-01')")
    old.db.execute("INSERT INTO cinema_settings VALUES ('preferences',?)", ('{"auto_next":false}',))
    old.db.commit()
    old.close()
    memory = MemoryStore(path)
    conversation = memory.new_conversation()
    monkeypatch.setattr(module, "MIGRATIONS", MIGRATIONS)
    store = CinemaStore(path)
    item = store.detail("old")
    assert item["status"] == "watched" and item["favorite"] == 1
    assert item["completed_at"] == "2026-11-01"
    assert item["current"]["position"] == 90
    assert item["current"]["links"][0]["id"] == "link"
    assert len(store.history()) == 1
    assert memory.db.execute("SELECT id FROM conversations").fetchone()[0] == conversation
    assert store.preferences().watch_region == "EE"
    store.close()
    memory.close()


def test_metadata_status_replay_delete_and_source_less_watchlist(tmp_path):
    store = CinemaStore(tmp_path / "library.sqlite")
    values = {
        "title": "Интерстеллар",
        "kind": "movie",
        "year": 2014,
        "catalogue_key": "tmdb:movie:157336",
        "metadata": {"genres": ["фантастика"], "runtime": 169, "rating": 8.5},
    }
    item = store.add(values)
    assert item["status"] == "planned" and not item["current"]["links"]
    assert store.options(item["id"])["options"] == []
    with pytest.raises(ValueError, match="страницу"):
        store.target(item["id"])
    store.update_title(item["id"], {"status": "watching", "user_rating": 9})
    store.progress(item["current"]["id"], 60, 1000)
    enriched = store.add({**values, "metadata": {**values["metadata"], "rating": 8.6}})
    assert enriched["id"] == item["id"] and enriched["status"] == "watching"
    assert enriched["current"]["position"] == 60 and enriched["user_rating"] == 9
    store.update_title(item["id"], {"status": "watched"})
    assert store.detail(item["id"])["completed_at"]
    store.update_title(item["id"], {"status": "planned"})
    assert store.detail(item["id"])["current"]["position"] == 60
    store.replay(item["id"])
    assert store.detail(item["id"])["current"]["position"] == 0
    assert len(store.history()) == 1
    for invalid in (True, 0, 11, 3.25, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            store.update_title(item["id"], {"user_rating": invalid})
    store.delete(item["id"])
    assert store.library() == [] and store.history() == []
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    store.close()


def test_local_files_bind_once_check_availability_and_refuse_web_urls(tmp_path):
    store = CinemaStore(tmp_path / "library.sqlite")
    source = store.save_source({"name": "Мои файлы", "adapter": "local"})
    video = tmp_path / "my movie.mp4"
    video.write_bytes(b"fixture, never played")
    item = store.add(
        {"title": "Local", "kind": "movie", "source_id": source["id"], "url": str(video)}
    )
    assert store.options(item["id"])["options"][0]["available"]
    video.unlink()
    assert not store.options(item["id"])["options"][0]["available"]
    with pytest.raises(ValueError):
        store.add({"title": "X", "source_id": source["id"], "url": "https://example.org/a.mp4"})
    with pytest.raises(ValueError):
        store.add({"title": "X", "source_id": source["id"], "url": "/tmp/not-a-video.txt"})
    store.save_source({**source, "enabled": False})
    with pytest.raises(ValueError, match="отключён"):
        store.target(item["id"])
    store.close()


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("Чушпан, добавь Интерстеллар в список хочу посмотреть", "plan"),
        ("Чушпан, что у меня в списке на просмотр?", "planned"),
        ("Чушпан, включи последнюю серию, которую я смотрела", "continue"),
        ("Чушпан, найди мне какой-нибудь триллер", "discover"),
        ("Чушпан, отметь этот фильм просмотренным", "title_watched"),
        ("Чушпан, продолжи мой сериал", "continue"),
    ],
)
def test_new_voice_routes_are_deterministic(text, action):
    assert command(text)["action"] == action


def test_voice_watchlist_and_mark_watched_through_conversation_without_llm(tmp_path):
    from backend.brain.conversation import Conversation
    from scripts.check_cinema_browser import NoLLM

    async def run():
        memory = MemoryStore(tmp_path / "assistant.sqlite")
        registry = ToolRegistry()
        registry.mac_agent = LocalMacAgent()
        service = Cinema(registry, Settings(data_dir=tmp_path / "data"), memory)
        registry.cinema = service
        conv = Conversation(Settings(language="ru-RU"), NoLLM(), memory, tools=registry)
        bus = HudBus()
        service.bind(bus, conv)
        text = "".join(
            [
                part
                async for part in conv.reply("Чушпан, добавь Интерстеллар в список хочу посмотреть")
            ]
        )
        assert "Добавила" in text
        item = (await service.db("library"))[0]
        text = "".join(
            [part async for part in conv.reply("Чушпан, что у меня в списке на просмотр?")]
        )
        assert item["title"] in text
        service.focused = item["id"]
        text = "".join(
            [part async for part in conv.reply("Чушпан, отметь этот фильм просмотренным")]
        )
        assert "просмотренным" in text
        assert (await service.db("title", item["id"]))["status"] == "watched"
        await service.close()
        memory.close()

    asyncio.run(run())


def test_unavailable_sources_never_start_or_create_progress(tmp_path):
    async def run():
        memory = MemoryStore(tmp_path / "assistant.sqlite")
        registry = ToolRegistry()
        registry.mac_agent = LocalMacAgent()
        player = FakePlayer()
        service = Cinema(
            registry, Settings(data_dir=tmp_path / "data"), memory, adapter_factory=lambda _: player
        )
        item = await service.db("add", {"title": "No video", "kind": "movie"})
        result = await service.open(item["id"])
        assert not result["playback_confirmed"] and not player.opened
        assert not (await service.db("detail", item["id"]))["current"]["last_viewed"]
        assert await service.db("history") == []
        await service.close()
        memory.close()

    asyncio.run(run())


def test_failed_player_and_progress_opt_out_are_truthful(tmp_path):
    class BrokenPlayer(FakePlayer):
        async def open(self, target):
            raise ToolError("Плеер не подтвердил действие.")

    async def run():
        memory = MemoryStore(tmp_path / "assistant.sqlite")
        registry = ToolRegistry()
        registry.mac_agent = LocalMacAgent()
        service = Cinema(
            registry,
            Settings(data_dir=tmp_path / "data"),
            memory,
            adapter_factory=lambda _: BrokenPlayer(),
        )
        item = await asyncio.get_running_loop().run_in_executor(service.worker, seed, service.store)
        result = await service.open(item["id"])
        assert not result["playback_confirmed"]
        assert await service.db("history") == []
        assert (await service.db("detail", item["id"]))["current"]["last_link_id"] is None
        await service.db("configure", {"save_progress": False})
        await service.save({"position": 25, "duration": 100}, "periodic")
        assert await service.db("history") == []
        await service.close()
        memory.close()

    asyncio.run(run())


def test_last_series_and_genre_requests_never_reach_llm_and_reuse_last_source(tmp_path):
    from backend.brain.conversation import Conversation
    from scripts.check_cinema_browser import NoLLM

    class HTTP:
        async def request(self, url, **kwargs):
            return SimpleNamespace(status_code=200, headers={"content-type": "text/html"})

    class Catalogue:
        credentials = SimpleNamespace(configured=True)

        async def search(self, query="", **filters):
            assert filters.get("genre") == "триллер"
            return [{"title": "Триллер из каталога"}]

    async def run():
        memory = MemoryStore(tmp_path / "assistant.sqlite")
        registry = ToolRegistry()
        registry.mac_agent = LocalMacAgent()
        player = FakePlayer()
        service = Cinema(
            registry,
            Settings(data_dir=tmp_path / "data"),
            memory,
            adapter_factory=lambda _: player,
            tmdb=Catalogue(),
        )
        registry.cinema = service
        conv = Conversation(Settings(language="ru-RU"), NoLLM(), memory, tools=registry)
        service.bind(HudBus(), conv)
        item = await asyncio.get_running_loop().run_in_executor(service.worker, seed, service.store)
        second = await service.db(
            "save_source", {"name": "Другой", "base_url": "https://example.org", "adapter": "html5"}
        )
        item = await service.db(
            "add",
            {
                "title_id": item["id"],
                "title": item["title"],
                "kind": "series",
                "source_id": second["id"],
                "url": "https://example.org/watch",
            },
        )
        episode = item["episodes"][0]

        async def genre_page(query, **kwargs):
            return {"items": await service.tmdb.search(genre=query.genre, kind=query.kind)}

        service.catalogue.page = genre_page
        preferred = episode["links"][-1]
        service.sources.http = HTTP()
        await service.db("progress", episode["id"], 66, 1000, "manual", preferred["id"])
        expected_position = 66
        for text in (
            "Чушпан, включи последнюю серию, которую я смотрела",
            "Чушпан, продолжи мой сериал",
        ):
            answer = "".join([part async for part in conv.reply(text)])
            assert "Подтверждено" in answer
            assert player.opened[-1]["link"]["id"] == preferred["id"]
            expected_position -= 3
            assert player.opened[-1]["episode"]["position"] == expected_position
        answer = "".join(
            [part async for part in conv.reply("Чушпан, найди мне какой-нибудь триллер")]
        )
        assert "Триллер из каталога" in answer
        await service.close()
        memory.close()

    asyncio.run(run())


def test_half_ratings_favorites_persist_independently_and_survive_metadata_replay(tmp_path):
    path = tmp_path / "library.sqlite"
    store = CinemaStore(path)
    values = {"title": "Любимая история", "kind": "movie", "catalogue_key": "tmdb:movie:10"}
    item = store.add(values)
    identifier = item["id"]
    store.progress(item["current"]["id"], 25, 100, "manual")
    store.update_title(identifier, {"status": "watched", "favorite": True, "user_rating": 9.5})
    added = store.detail(identifier)["favorite_added_at"]
    store.update_title(identifier, {"user_rating": 7.5})
    store.add({**values, "metadata": {"rating": 8.2}})
    store.close()
    store = CinemaStore(path)
    item = store.detail(identifier)
    assert item["user_rating"] == 7.5 and item["favorite"] == 1 and item["status"] == "watched"
    assert item["current"]["position"] == 25 and item["favorite_added_at"] == added
    store.replay(identifier)
    item = store.detail(identifier)
    assert item["status"] == "watching" and item["user_rating"] == 7.5 and item["favorite"] == 1
    assert len(store.history()) == 1
    store.update_title(identifier, {"favorite": False, "user_rating": None})
    item = store.detail(identifier)
    assert item["user_rating"] is None and item["favorite_added_at"] is None
    assert item["status"] == "watching" and not item["favorite"]
    store.update_title(identifier, {"favorite": True})
    assert store.detail(identifier)["status"] == "watching"
    store.close()


def test_v2_migration_preserves_integer_rating_dates_links_and_progress(tmp_path, monkeypatch):
    import backend.cinema.store as module

    path = tmp_path / "old.sqlite"
    monkeypatch.setattr(module, "MIGRATIONS", MIGRATIONS[:2])
    old = CinemaStore(path)
    with old.db:
        old.db.execute(
            "INSERT INTO cinema_titles VALUES "
            "('old','movie:2020:film','Film','movie',2020,'','','tmdb:movie:1',"
            "'planned',0,NULL,'2026-11-01')"
        )
        old.db.execute("INSERT INTO cinema_library VALUES ('old','watched',1,9,'2026-10-30')")
        old.db.execute("INSERT INTO cinema_episodes VALUES ('ep','old',0,0,50,100,0,NULL,NULL,'')")
        old.db.execute(
            "INSERT INTO cinema_history VALUES (1,'ep',NULL,50,100,'manual','2026-10-30')"
        )
    old.close()
    monkeypatch.setattr(module, "MIGRATIONS", MIGRATIONS)
    store = CinemaStore(path)
    item = store.detail("old")
    assert (item["status"], item["favorite"], item["user_rating"]) == ("watched", 1, 9)
    assert item["completed_at"] == "2026-10-30"
    assert item["favorite_added_at"] == item["added_at"] == "2026-11-01"
    assert item["current"]["position"] == 50 and len(store.history()) == 1
    store.update_title("old", {"user_rating": 9.5})
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    store.close()
    store = CinemaStore(path)
    assert store.detail("old")["user_rating"] == 9.5
    store.close()


def test_tmdb_identity_distinguishes_types_and_same_named_releases(tmp_path):
    store = CinemaStore(tmp_path / "library.sqlite")
    a = store.add({"title": "Same", "year": 2020, "kind": "movie", "catalogue_key": "tmdb:movie:1"})
    b = store.add({"title": "Same", "year": 2020, "kind": "series", "catalogue_key": "tmdb:tv:1"})
    c = store.add({"title": "Same", "year": 2020, "kind": "movie", "catalogue_key": "tmdb:movie:2"})
    assert len({a["id"], b["id"], c["id"]}) == 3
    assert (
        store.add(
            {"title": "Localized", "year": 2020, "kind": "movie", "catalogue_key": "tmdb:movie:1"}
        )["id"]
        == a["id"]
    )
    store.close()
