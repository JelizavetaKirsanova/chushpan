"""Reviewed page strategies and data-only, reusable player profiles."""

import asyncio
import json
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

from backend.cinema.adapters import PLAYER, PLAYER_ERRORS
from backend.cinema.browser_search import SearchSession
from backend.cinema.models import origin
from backend.cinema.playback import playback_error
from backend.tools.registry import ToolError


@dataclass(frozen=True)
class PageAdapter:
    id: str
    hosts: tuple[str, ...]
    preferred_players: tuple[str, ...] = ()

    def accepts(self, url):
        return urlsplit(url).hostname in self.hosts


KINOGO = PageAdapter("kinogo", ("kinogo.online",), ("Плеер 1", "Смотреть онлайн", "Плеер 3"))
PAGE_ADAPTERS = (KINOGO,)
PLAYER_ENGINES = ("auto", "html5", "semantic_ax", "venom_ax")
VENOM_HOSTS = {"api.nextembed.ws"}


def page_adapter(url):
    return next((adapter for adapter in PAGE_ADAPTERS if adapter.accepts(url)), None)


def player_engine(host, requested="auto"):
    return (
        requested if requested != "auto" else "venom_ax" if host in VENOM_HOSTS else "semantic_ax"
    )


async def discover_pages(tabs, source, item):
    """Read same-site search results; playback is checked separately."""
    base = origin(source["base_url"])
    adapter = page_adapter(base)
    search = source.get("search_url", "")
    if not search and (not adapter or adapter.id != "kinogo"):
        return []
    marker = uuid4().hex
    if search:
        url = search.replace("{query}", quote(item["title"]))
        url += ("&" if "?" in url else "?") + "chushpan_session=" + marker
    else:
        url = (
            base
            + "/index.php?"
            + urlencode(
                {
                    "do": "search",
                    "subaction": "search",
                    "story": item["title"],
                    "chushpan_session": marker,
                }
            )
        )
    session = SearchSession(marker, prefix=base + "/")
    request = {
        "marker": marker,
        "host": urlsplit(base).hostname,
        "names": [item["title"], (item.get("metadata") or {}).get("original_title", "")],
    }
    script = (
        r"""(r=>{
      if(location.hostname!==r.host||
         new URL(location.href).searchParams.get('chushpan_session')!==r.marker)
        return JSON.stringify({changed:true});
      const text=document.title+' '+(document.body?.innerText||'').slice(0,500);
      if(/captcha|verify you are human|just a moment|access denied/i.test(text))
        return JSON.stringify({restricted:true});
      const norm=s=>(s||'').toLowerCase().replace(/ё/g,'е').replace(/\([^)]*\)/g,'')
        .replace(/смотреть онлайн.*|\b(?:19|20)\d{2}\b.*|\d+\s*сезон.*/gi,'')
        .replace(/[^\p{L}\p{N}]+/gu,' ').trim();
      const names=r.names.filter(Boolean).map(norm);
      const rows=[...document.querySelectorAll('a[href]')].flatMap(a=>{
        const title=(a.getAttribute('title')||a.textContent||'').trim();
        try {const u=new URL(a.href);
          if(u.hostname!==r.host||!u.pathname.endsWith('.html')||
             !names.includes(norm(title)))return [];
          u.hash='';return [{url:u.href,title}];
        }catch{return [];}
      });
      return JSON.stringify({rows:[...new Map(rows.map(r=>[r.url,r])).values()].slice(0,4)});
    })"""
        + "("
        + json.dumps(request, ensure_ascii=False)
        + ")"
    )
    try:
        async with asyncio.timeout(18):
            await tabs.open_marked(session, url)
            for _ in range(12):
                await asyncio.sleep(0.5)
                raw = await tabs._owned(
                    session,
                    "return do JavaScript "
                    + json.dumps(script, ensure_ascii=False)
                    + " in ownedTab",
                )
                if raw == "unowned":
                    raise ToolError("Сайт перенаправил временную страницу поиска.")
                state = json.loads(raw)
                if state.get("restricted"):
                    raise ToolError(
                        "Внутренний поиск сайта требует проверки браузера или ограничил доступ."
                    )
                if state.get("changed"):
                    raise ToolError("Адрес внутреннего поиска изменился.")
                if state.get("rows"):
                    return state["rows"]
            return []
    finally:
        await asyncio.shield(tabs.close_marked(session))


class OwnedHTML5Bridge:
    """Use the existing HTML5 implementation in the already-owned tab."""

    def __init__(self, owner):
        self.owner = owner

    async def ensure_page(self, url, opener):
        if url != self.owner.url:
            raise ToolError("Страница плеера изменилась.")

    async def evaluate(self, url, request):
        if url != self.owner.url:
            raise ToolError("Страница плеера изменилась.")
        payload = {**request, "url": url}
        script = PLAYER + "(" + json.dumps(payload, ensure_ascii=False) + ")"
        result = await self.owner.javascript(script)
        if result.get("error"):
            code = result["error"]
            raise playback_error(code, PLAYER_ERRORS.get(code, "Плеер не подтвердил действие."))
        return result
