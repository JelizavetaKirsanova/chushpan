"""Bounded Safari discovery with ownership proved by a unique navigation marker."""

import asyncio
import contextlib
import json
from dataclasses import dataclass
from urllib.parse import urlencode
from uuid import uuid4

from backend.cinema.models import web_url
from backend.permission_policy import requests_allowed
from backend.tools.registry import ToolError

SEARCH_ORIGIN = "https://duckduckgo.com/"
REGIONS = {"EE": "ee-et", "RU": "ru-ru", "US": "us-en", "GB": "uk-en"}

RESULT_LINKS = """(() => {
  const params = new URLSearchParams(location.search);
  if (location.hostname !== 'duckduckgo.com' ||
      params.get('chushpan_session') !== __MARKER__ ||
      params.get('q') !== __QUERY__) return JSON.stringify({loading:true});
  const text = (document.body?.innerText || '').slice(0,3500);
  if (/verify you are human|unusual traffic|captcha|подтвердите.*человек/i.test(text))
    return JSON.stringify({error:'challenge'});
  const links = [...document.querySelectorAll('a[href]')].filter(a =>
    a.querySelector('h2,h3') || a.closest('h2,h3') || a.dataset.testid==='result-title-a');
  return JSON.stringify({urls:links.map(a=>a.href).filter(u=>{
    try {const p=new URL(u);return p.protocol==='https:'&&
      !p.hostname.endsWith('duckduckgo.com')&&p.pathname!=='/';}catch{return false;}
  }).slice(0,8), empty:/no results|ничего не найдено|нет результатов/i.test(text)});
})()"""


@dataclass
class SearchSession:
    marker: str
    window: str = ""
    created_window: bool = False
    prefix: str = SEARCH_ORIGIN
    companion_urls: list[str] | None = None

    @property
    def tag(self):
        return "chushpan_session=" + self.marker


class BrowserSearch:
    configured = True

    def __init__(self, executor, *, poll_seconds=0.6, polls=15):
        self.executor = executor
        self.lock = asyncio.Lock()
        self.sessions = {}
        self.poll_seconds, self.polls = poll_seconds, polls

    @staticmethod
    def queries(query, prefs):
        from backend.cinema.internet import InternetSearch

        queries = InternetSearch.queries(query, prefs)[:1]
        metadata = query.get("metadata") or {}
        title = metadata.get("original_title") or query["title"]
        if query["kind"] == "series":
            episode = f"season {query['season']} episode {query['episode']}"
        else:
            episode = "film"
        queries.append(f'"{title}" {query.get("year") or ""} {episode} watch official streaming')
        return queries

    async def search(self, query, region, language):
        return await self.discover([query], region, language)

    async def _run(self, code):
        token = requests_allowed.set(False)
        try:
            return await self.executor._run('tell application "Safari"\n' + code + "\nend tell")
        finally:
            requests_allowed.reset(token)

    async def _owned(self, session, action, *, any_window=False):
        # Safari's tab scripting class has no stable ID. Match both the owning
        # window (once known) and our unpredictable marker, never the active tab.
        window = (
            f"{{window id {session.window}}}" if session.window and not any_window else "windows"
        )
        return await self._run(f"""
set ownedTabs to {{}}
set ownerID to ""
repeat with w in {window}
    repeat with t in tabs of w
        set u to URL of t
        if u is not missing value then
            if u starts with {json.dumps(session.prefix)} and u contains "{session.tag}" then
                set end of ownedTabs to t
                set ownerID to (id of w) as text
            end if
        end if
    end repeat
end repeat
if (count of ownedTabs) is not 1 then return "unowned"
set ownedTab to item 1 of ownedTabs
set currentAddress to URL of ownedTab
if currentAddress is missing value then return "unowned"
if currentAddress does not start with {json.dumps(session.prefix)} then return "unowned"
if currentAddress does not contain "{session.tag}" then return "unowned"
{action}
""")

    async def _open(self, session, url, *, own_window=False):
        address = json.dumps(url)
        owner = "no_window"
        previous = None
        if not own_window:
            owner = await self._run(f"""
if (count of windows) is 0 then return "no_window"
set ownerID to id of window 1
tell window id ownerID
    make new tab at end of tabs with properties {{URL:{address}}}
end tell
return ownerID as text
""")
        if owner == "no_window":
            previous = await self._run("""
set inventory to ""
repeat with w in windows
    set inventory to inventory & "WINDOW " & (id of w as text) & linefeed
    repeat with t in tabs of w
        set u to URL of t
        if u is not missing value then set inventory to inventory & u & linefeed
    end repeat
end repeat
return inventory
""")
            await self._run(f'make new document with properties {{URL:{address}}}\nreturn "sent"')
        elif owner.isdigit():
            session.window = owner
        else:
            raise ToolError("Safari не подтвердил создание временной вкладки поиска.")
        for _ in range(15):
            owner = await self._owned(session, "return ownerID")
            if owner.isdigit():
                session.window = owner
                if previous is not None and "WINDOW " + owner not in previous.splitlines():
                    session.created_window = True
                    companions = await self._owned(
                        session,
                        """
set addresses to ""
repeat with t in tabs of window id (ownerID as integer)
    set u to URL of t
    if u is missing value then return "unknown"
    if u does not contain """
                        + json.dumps(session.tag)
                        + """ then
        set addresses to addresses & u & linefeed
    end if
end repeat
return addresses
""",
                    )
                    # Safari mirrors pinned tabs in a newly created window.
                    # Close that window only while its original companions
                    # still exist in their original windows and are unchanged.
                    addresses = companions.splitlines()
                    if all(u in previous.splitlines() for u in addresses):
                        session.companion_urls = addresses
                return
            await asyncio.sleep(0.15)
        raise ToolError("Safari не подтвердил метку временной вкладки поиска.")

    async def open_marked(self, session, url):
        self.sessions[session.marker] = session
        task = asyncio.create_task(self._open(session, url, own_window=True))
        try:
            await asyncio.shield(task)
        except BaseException:
            with contextlib.suppress(ToolError, OSError, TimeoutError):
                await asyncio.shield(task)
            await asyncio.shield(self._cleanup(session))
            raise

    async def close_marked(self, session):
        async with self.lock:
            await self._cleanup(session)

    async def _cleanup(self, session):
        close_tab = 'close ownedTab\nreturn "closed"'
        action = close_tab
        if session.created_window and session.window and session.companion_urls is not None:
            companions = "{" + ", ".join(json.dumps(u) for u in session.companion_urls) + "}"
            action = f"""
set companions to {{}}
repeat with t in tabs of window id (ownerID as integer)
    set u to URL of t
    if u is missing value then set u to ""
    if u does not contain "{session.tag}" then set end of companions to u
end repeat
set originalsPresent to true
repeat with address in {companions}
    set foundOriginal to false
    repeat with w in windows
        if (id of w as text) is not ownerID then
            repeat with t in tabs of w
                if URL of t is (address as text) then set foundOriginal to true
            end repeat
        end if
    end repeat
    if not foundOriginal then set originalsPresent to false
end repeat
if companions is {companions} and originalsPresent then
    close window id (ownerID as integer)
else
    close ownedTab
end if
return "closed"
"""
        try:
            result = await self._owned(session, action)
        except ToolError:
            if not session.window:
                return
            result = "unowned"
        if result == "unowned" and session.window:
            # Safari can move the marked tab into another window. Only close
            # the tab in that case, even if it is now alone.
            try:
                result = await self._owned(session, close_tab, any_window=True)
            except ToolError:
                return
        if result in {"closed", "unowned"}:
            self.sessions.pop(session.marker, None)

    async def close(self):
        async with self.lock:
            for session in list(self.sessions.values()):
                await self._cleanup(session)

    async def discover(self, queries, region, language):
        if not queries:
            return []
        async with self.lock:
            for stale in list(self.sessions.values()):
                await self._cleanup(stale)
            if self.sessions:
                raise ToolError("Safari пока не подтвердил закрытие прежней вкладки поиска.")
            session = SearchSession(uuid4().hex)
            self.sessions[session.marker] = session
            rows = []
            opened = None
            try:
                for query in dict.fromkeys(queries):
                    query = query[:500]
                    url = (
                        SEARCH_ORIGIN
                        + "?"
                        + urlencode(
                            {
                                "q": query,
                                "ia": "web",
                                "kl": REGIONS.get(region, "wt-wt"),
                                "chushpan_session": session.marker,
                            }
                        )
                    )
                    if not opened:
                        # A cancellation while the Apple Event is in flight must
                        # still wait for the created window before cleaning up.
                        opened = asyncio.create_task(self._open(session, url))
                        await asyncio.shield(opened)
                    else:
                        result = await self._owned(
                            session, f'set URL of ownedTab to {json.dumps(url)}\nreturn "sent"'
                        )
                        if result != "sent":
                            break
                    script = RESULT_LINKS.replace("__MARKER__", json.dumps(session.marker)).replace(
                        "__QUERY__", json.dumps(query, ensure_ascii=False)
                    )
                    for _ in range(self.polls):
                        await asyncio.sleep(self.poll_seconds)
                        raw = await self._owned(
                            session,
                            "return do JavaScript "
                            + json.dumps(script, ensure_ascii=False)
                            + " in ownedTab",
                        )
                        if raw == "unowned":
                            if rows:
                                return list(dict.fromkeys(rows))
                            raise ToolError(
                                "Safari сменил страницу поиска или вкладка была закрыта."
                            )
                        with contextlib.suppress(TypeError, ValueError):
                            result = json.loads(raw)
                            if result.get("error"):
                                raise ToolError("Поиск в Safari остановлен на проверке браузера.")
                            if result.get("urls") or result.get("empty"):
                                rows.extend(web_url(u) for u in result.get("urls", []))
                                break
                    else:
                        if not rows:
                            raise ToolError("Safari: истекло время ожидания результатов поиска.")
                return list(dict.fromkeys(rows))
            finally:
                if opened:
                    with contextlib.suppress(ToolError, OSError, TimeoutError):
                        await asyncio.shield(opened)
                await asyncio.shield(self._cleanup(session))
