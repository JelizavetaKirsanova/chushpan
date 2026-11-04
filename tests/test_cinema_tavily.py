"""No real API keys, network credits or user Keychain entries used by these tests."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.cinema.internet import InternetSearch
from backend.cinema.keychain import TavilyCredentials
from backend.cinema.models import Preferences
from backend.cinema.service import Cinema
from backend.cinema.tavily import TavilyError, TavilySearch
from backend.config import Settings
from backend.context import DeviceContext
from backend.hud.bus import HudBus
from backend.mac_agent import LocalMacAgent
from backend.memory.store import MemoryStore
from backend.tools.registry import ToolError, ToolRegistry

TOKEN = "tvly-fixture-unit-key"
URL = "https://series.example/sezon-2/seriya-3"
QUERY = {
    "title": "Тестовый сериал",
    "kind": "series",
    "year": 2025,
    "season": 2,
    "episode": 3,
    "metadata": {"original_title": "Test Series", "alternative_titles": ["Test Alias"]},
}


class Security:
    kSecClass = "class"
    kSecClassGenericPassword = "generic"
    kSecAttrService = "service"
    kSecAttrAccount = "account"
    kSecAttrLabel = "label"
    kSecReturnAttributes = "attributes"
    kSecReturnData = "data"
    kSecValueData = "value"
    kSecUseAuthenticationUI = "ui"
    kSecUseAuthenticationUIFail = "fail"
    errSecSuccess = 0
    errSecItemNotFound = -25300
    errSecInteractionNotAllowed = -25308

    def __init__(self):
        self.value = None
        self.calls = []
        self.denied = False

    def SecItemCopyMatching(self, q, result):
        self.calls.append(("copy", q))
        if self.denied:
            return self.errSecInteractionNotAllowed, None
        if self.value is None:
            return self.errSecItemNotFound, None
        return 0, self.value if q.get(self.kSecReturnData) else {"account": "tavily"}

    def SecItemUpdate(self, q, value):
        self.calls.append(("update", q))
        if self.denied:
            return -25293
        if self.value is None:
            return self.errSecItemNotFound
        self.value = value[self.kSecValueData]
        return 0

    def SecItemAdd(self, q, result):
        self.calls.append(("add", q))
        self.value = q[self.kSecValueData]
        return 0, None

    def SecItemDelete(self, q):
        self.calls.append(("delete", q))
        if self.denied:
            return -25293
        self.value = None
        return 0


def credentials():
    return TavilyCredentials(security=Security())


def test_keychain_add_update_status_no_password_data_delete_and_no_file_fallback(tmp_path):
    before = set(tmp_path.iterdir())
    c = credentials()
    assert not c.configured
    c.save(TOKEN)
    assert c.configured and c.read() == TOKEN
    status = c.status(refresh=True)
    query = c.security().calls[-1][1]
    assert query["attributes"] and "data" not in query and query["ui"] == "fail"
    assert TOKEN not in json.dumps(status)
    c.save(TOKEN + "-updated")
    assert c.read() == TOKEN + "-updated"
    c.security().denied = True
    assert c.status(refresh=True)["state"] == "locked"
    with pytest.raises(ToolError, match="Keychain"):
        c.save(TOKEN)
    with pytest.raises(ToolError, match="Keychain"):
        c.read()
    assert set(tmp_path.iterdir()) == before
    c.security().denied = False
    c.save("")
    assert not c.configured
    with pytest.raises(ToolError, match="Добавь"):
        c.read()
    for value in ["bad", TOKEN + "\n", None]:
        with pytest.raises(ToolError):
            c.save(value)


def test_tavily_post_header_basic_single_query_and_no_secret_in_logs(caplog):
    async def run():
        c = credentials()
        c.save(TOKEN)
        calls = []

        def handle(request):
            calls.append(request)
            assert request.method == "POST" and str(request.url) == "https://api.tavily.com/search"
            assert request.headers["authorization"] == "Bearer " + TOKEN
            body = json.loads(request.content)
            assert TOKEN not in request.content.decode() and TOKEN not in str(request.url)
            assert body["search_depth"] == "basic" and not body["auto_parameters"]
            assert not body["include_answer"] and not body["include_raw_content"]
            assert body["language"] == "ru" and "include_domains" not in body
            assert "Тестовый сериал" in body["query"] and "Test Series" in body["query"]
            assert "2025" in body["query"] and "S02E03" in body["query"]
            return httpx.Response(
                200,
                json={"results": [{"url": URL}, {"url": "javascript:bad"}, {"title": "No URL"}]},
            )

        provider = TavilySearch(c, transport=httpx.MockTransport(handle))
        queries = provider.queries(QUERY, Preferences())
        assert len(queries) == 1
        assert await provider.search(queries[0], "EE", "ru") == [URL]
        assert len(calls) == 1 and TOKEN not in caplog.text

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "invalid_key"),
        (403, "forbidden"),
        (429, "rate_limit"),
        (432, "credits_exhausted"),
        (433, "paygo_limit"),
        (434, "temporarily_unavailable"),
        (500, "request_failed"),
        (302, "request_failed"),
    ],
)
def test_api_errors_are_specific_redacted_and_never_retried(status, code, caplog):
    async def run():
        c = credentials()
        c.save(TOKEN)
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(
                status,
                json={"detail": {"error": TOKEN}},
                headers={"location": "https://attacker.example/"},
            )

        provider = TavilySearch(c, transport=httpx.MockTransport(handle))
        with pytest.raises(TavilyError) as error:
            await provider.search("Film", "EE", "ru")
        assert error.value.code == code and TOKEN not in str(error.value)
        assert len(calls) == 1 and TOKEN not in caplog.text

    asyncio.run(run())


@pytest.mark.parametrize("limit,used,ok", [(None, 200, True), (10, 2, True), (10, 10, False)])
def test_connection_check_is_get_usage_and_does_not_search(limit, used, ok):
    async def run():
        c = credentials()
        c.save(TOKEN)
        calls = []

        def handle(request):
            calls.append(request)
            assert request.method == "GET" and request.url.path == "/usage"
            assert request.content == b"" and TOKEN not in str(request.url)
            return httpx.Response(
                200, json={"key": {"limit": limit, "usage": used}, "account": {"private": TOKEN}}
            )

        result = await TavilySearch(c, transport=httpx.MockTransport(handle)).check()
        assert result["ok"] is ok and result["authenticated"]
        assert result["usage"] == used and TOKEN not in json.dumps(result) and len(calls) == 1

    asyncio.run(run())


def test_network_malformed_and_oversized_errors_never_contain_secrets():
    async def run():
        c = credentials()
        c.save(TOKEN)

        def fail(request):
            raise httpx.ConnectError(TOKEN, request=request)

        with pytest.raises(TavilyError, match="проверь интернет"):
            await TavilySearch(c, transport=httpx.MockTransport(fail)).check()
        for body in [b"[]", b"invalid", json.dumps({"results": TOKEN}).encode(), b"x" * 2_000_001]:
            provider = TavilySearch(
                c,
                transport=httpx.MockTransport(
                    lambda r, body=body: httpx.Response(200, content=body)
                ),
            )
            with pytest.raises(TavilyError) as exc:
                await provider.search("Film", "EE", "ru")
            assert TOKEN not in str(exc.value)

    asyncio.run(run())


class Pages:
    async def request(self, url, **kwargs):
        if url.endswith("robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if kwargs.get("method") == "HEAD":
            return httpx.Response(200)
        entity = {
            "@type": "TVEpisode",
            "name": "Episode 3",
            "partOfSeries": {"name": QUERY["title"], "datePublished": "2025"},
            "partOfSeason": {"seasonNumber": 2},
            "episodeNumber": 3,
            "inLanguage": "ru",
        }
        return httpx.Response(
            200, text='<script type="application/ld+json">' + json.dumps(entity) + "</script>"
        )


class Player:
    def __init__(self):
        self.opened = []

    async def open(self, target):
        self.opened.append(target)
        return {
            "mode": "manual",
            "page_opened": True,
            "playback_confirmed": False,
            "message": "Страница открыта; видео не подтверждено.",
        }

    async def detach(self):
        pass


def test_tavily_service_connection_search_save_restart_no_more_credits_and_brave_preserved(
    tmp_path,
):
    async def run():
        c = credentials()
        calls = []
        player = Player()

        def transport(request):
            calls.append(request.url.path)
            return httpx.Response(
                200,
                json={"key": {"usage": 1, "limit": 100}}
                if request.url.path == "/usage"
                else {"results": [{"url": URL}]},
            )

        def setup():
            memory = MemoryStore(tmp_path / "assistant.sqlite")
            tools = ToolRegistry()
            tools.mac_agent = LocalMacAgent()
            service = Cinema(
                tools,
                Settings(data_dir=tmp_path / "data"),
                memory,
                adapter_factory=lambda _: player,
            )
            service.tavily_credentials = c
            service.tavily = TavilySearch(c, transport=httpx.MockTransport(transport))
            service.sources.web.providers["tavily"] = InternetSearch(
                c, Pages(), provider=service.tavily, name="Tavily"
            )
            service.sources.http = Pages()
            service.bind(HudBus(), SimpleNamespace(device=DeviceContext.local_mac()))
            return memory, service

        memory, service = setup()
        try:
            await service.handle({"op": "tavily_credentials", "credential": TOKEN})
            assert c.read() == TOKEN
            await service.handle({"op": "tavily_check"})
            assert calls == ["/usage"]
            service.search_credentials.save("BSA_fixture_old_key")
            await service.db(
                "save_source", {"name": "KION", "base_url": "https://kion.ru", "enabled": False}
            )
            item = await service.db(
                "add",
                {k: v for k, v in QUERY.items() if k not in {"metadata"}}
                | {"metadata": QUERY["metadata"]},
            )
            await service.resume(item["id"])
            assert not player.opened and len(service.watch_confirmations) == 1
            await service.handle(
                {"op": "watch_confirm", "token": next(iter(service.watch_confirmations))}
            )
            assert player.opened[-1]["link"]["url"] == URL and not service.session.get("counted")
            assert calls.count("/search") == 1
            assert (await service.db("detail", item["id"]))["current"]["links"][0]["url"] == URL
            assert TOKEN not in json.dumps(await service.db("export_library"))
            assert service.search_credentials.read() == "BSA_fixture_old_key"
        finally:
            await service.close()
            memory.close()
        memory, service = setup()
        try:
            await service.resume(item["id"])
            assert len(player.opened) == 2 and calls.count("/search") == 1
            assert (await service.db("preferences")).search_provider == "tavily"
            assert service.search_credentials.configured
        finally:
            await service.close()
            memory.close()

    asyncio.run(run())


def test_tavily_quota_is_visible_without_brave_fallback_or_repeated_requests():
    from backend.cinema.internet import SearchProviders

    async def run():
        c = credentials()
        c.save(TOKEN)
        calls = []

        def transport(request):
            calls.append(request.url.path)
            return httpx.Response(432, json={"detail": {"error": TOKEN}})

        tavily = InternetSearch(
            c,
            Pages(),
            provider=TavilySearch(c, transport=httpx.MockTransport(transport)),
            name="Tavily",
        )

        class ForbiddenFallback:
            async def search(self, *args):
                raise AssertionError("Brave fallback is not authorized")

        router = SearchProviders(Pages(), {"tavily": tavily, "brave": ForbiddenFallback()})
        rows = await router.search(QUERY, Preferences(), [])
        assert len(calls) == 1 and rows[0]["source_name"].endswith("Tavily")
        assert "Лимит кредитов" in rows[0]["reason"] and TOKEN not in json.dumps(rows)
        assert not rows[0]["allowed"] and not rows[0]["match"]

    asyncio.run(run())


def test_tavily_snippet_does_not_validate_wrong_episode():
    async def run():
        c = credentials()
        c.save(TOKEN)
        provider = TavilySearch(
            c,
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={"results": [{"url": URL, "title": "Тестовый сериал S02E03", "score": 1}]},
                )
            ),
        )

        class WrongEpisode(Pages):
            async def request(self, url, **kwargs):
                response = await super().request(url, **kwargs)
                if not url.endswith("robots.txt"):
                    return httpx.Response(
                        200, text=response.text.replace('"episodeNumber": 3', '"episodeNumber": 4')
                    )
                return response

        rows = await InternetSearch(c, WrongEpisode(), provider=provider, name="Tavily").search(
            QUERY, Preferences(), []
        )
        assert rows[0]["kind"] == "error" and rows[0]["match"] == 0 and not rows[0]["allowed"]

    asyncio.run(run())
