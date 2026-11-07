"""Free search through an owned Safari window; no browser profiles or CAPTCHA bypass."""

import asyncio
import json
from urllib.parse import quote

from backend.cinema.models import web_url
from backend.permission_policy import requests_allowed
from backend.tools.registry import ToolError


class BrowserSearch:
    configured = True

    def __init__(self, executor):
        self.executor = executor
        self.lock = asyncio.Lock()

    @staticmethod
    def queries(query, prefs):
        from backend.cinema.internet import InternetSearch

        return InternetSearch.queries(query, prefs)[:2]

    async def search(self, query, region, language):
        # A normal public search page, opened only after explicit cinema search.
        url = "https://duckduckgo.com/?q=" + quote(query[:500]) + "&ia=web"
        url += "&kl=" + {"EE": "ee-et", "RU": "ru-ru", "US": "us-en", "GB": "uk-en"}.get(
            region, "wt-wt"
        )
        script = """(()=>{
          if(location.hostname!=='duckduckgo.com')return JSON.stringify({error:'redirect'});
          const text=document.body?.innerText||'';
          if(/verify you are human|unusual traffic|captcha|подтвердите.*человек/i.test(text))
            return JSON.stringify({error:'captcha'});
          const links=[...document.querySelectorAll('a[href]')].filter(a=>
            a.querySelector('h2,h3')||a.closest('h2,h3')||a.dataset.testid==='result-title-a');
          return JSON.stringify({urls:links.map(a=>a.href).filter(u=>{
            try {const p=new URL(u);return p.protocol==='https:'&&
              !p.hostname.endsWith('duckduckgo.com')&&p.pathname!=='/';}catch{return false;}
          }).slice(0,8)});
        })()"""
        async with self.lock:
            window = None
            token = requests_allowed.set(False)
            try:
                window = str(
                    await self.executor._run(
                        'tell application "Safari"\nmake new document with properties {URL:'
                        + json.dumps(url)
                        + "}\nreturn id of front window\nend tell"
                    )
                ).strip()
                if not window.isdigit():
                    raise ToolError("Safari не подтвердил отдельное окно поиска.")
                for _ in range(15):
                    await asyncio.sleep(0.7)
                    value = await self.executor._run(
                        'tell application "Safari"\nreturn do JavaScript '
                        + json.dumps(script)
                        + " in current tab of window id "
                        + window
                        + "\nend tell"
                    )
                    try:
                        result = json.loads(value)
                    except (TypeError, ValueError):
                        continue
                    if result.get("error"):
                        raise ToolError(
                            "Поиск в Safari требует проверки человеком или сменил сайт. "
                            "Автоматизация остановлена; защита не обходилась."
                        )
                    if result.get("urls"):
                        return list(dict.fromkeys(web_url(u) for u in result["urls"]))
                raise ToolError(
                    "Safari: поисковая страница не предоставила результаты за 11 секунд."
                )
            finally:
                if window and window.isdigit():
                    try:
                        await self.executor._run(
                            'tell application "Safari"\nif (count of tabs of window id '
                            + window
                            + ") is 1 then\n"
                            + "if URL of current tab of window id "
                            + window
                            + ' starts with "https://duckduckgo.com/" then close window id '
                            + window
                            + "\nend if\nend tell"
                        )
                    except ToolError:
                        pass
                requests_allowed.reset(token)
