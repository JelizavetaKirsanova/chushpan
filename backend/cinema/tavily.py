"""Official Tavily REST API; bearer secret never appears in URLs or error messages."""

import asyncio
import json

import httpx

from backend.cinema.models import web_url
from backend.permission_policy import requests_allowed
from backend.tools.registry import ToolError

TAVILY = "https://api.tavily.com"


class TavilyError(ToolError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class TavilySearch:
    def __init__(self, credentials, *, transport=None):
        self.credentials, self.transport = credentials, transport

    @staticmethod
    def status_error(status):
        messages = {
            401: (
                "invalid_key",
                "Ключ Tavily недействителен или отозван. Сохрани новый ключ в настройках.",
            ),
            403: ("forbidden", "Tavily запретил запрос. Проверь доступ ключа в кабинете Tavily."),
            429: (
                "rate_limit",
                "Tavily: слишком много запросов. Подожди и повтори вручную; "
                "автоматический повтор отключён.",
            ),
            432: (
                "credits_exhausted",
                "Лимит кредитов Tavily исчерпан. Проверь тариф и лимит ключа в кабинете Tavily. "
                "Сохранённые ссылки доступны без поиска.",
            ),
            433: (
                "paygo_limit",
                "Достигнут лимит оплаты по использованию Tavily. "
                "Измени его в кабинете Tavily; автоматического повтора нет.",
            ),
            434: (
                "temporarily_unavailable",
                "Tavily временно ограничил получение контента. "
                "Повтори позже; сохранённые ссылки доступны.",
            ),
        }
        code, message = messages.get(
            status,
            (
                "request_failed",
                f"Tavily: HTTP {status}. Запрос не выполнен; "
                "повтори позже или проверь настройки API.",
            ),
        )
        return TavilyError(code, message)

    async def request(self, method, path, payload=None):
        token = await asyncio.to_thread(self.credentials.read)
        try:
            async with httpx.AsyncClient(
                transport=self.transport, trust_env=False, timeout=15, follow_redirects=False
            ) as client:
                async with client.stream(
                    method,
                    TAVILY + path,
                    json=payload if method == "POST" else None,
                    headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                ) as response:
                    if response.status_code != 200:
                        raise self.status_error(response.status_code)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 2_000_000:
                            raise TavilyError(
                                "invalid_response", "Ответ Tavily превышает допустимый размер."
                            )
        except httpx.HTTPError:
            raise TavilyError(
                "network",
                "Tavily не ответил: проверь интернет. Автоматический повтор не выполнялся.",
            ) from None
        try:
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, TypeError):
            raise TavilyError("invalid_response", "Tavily вернул неподдерживаемый ответ.") from None

    @staticmethod
    def queries(query, prefs):
        meta = query.get("metadata", {})
        names = list(
            dict.fromkeys(
                t
                for t in [
                    query["title"],
                    meta.get("original_title"),
                    *meta.get("alternative_titles", []),
                ]
                if t
            )
        )[:3]
        # Reserve space for year and exact coordinates even for very long titles.
        text = " ".join(f'"{name[:80]}"' for name in names)
        text += f" {query.get('year') or ''}"
        if query["kind"] == "series":
            text += (
                f" S{query['season']:02d}E{query['episode']:02d} "
                f"{query['season']} сезон {query['episode']} серия"
            )
        text += " смотреть легально бесплатно" if prefs.prefer_free else " смотреть легально"
        if prefs.prefer_russian:
            text += " русская озвучка"
        return [text[:400]]

    async def search(self, query, region, language):
        permission_token = requests_allowed.set(False)
        try:
            data = await self.request(
                "POST",
                "/search",
                {
                    "query": query[:400],
                    "search_depth": "basic",
                    "auto_parameters": False,
                    "topic": "general",
                    "max_results": 5,
                    "include_answer": False,
                    "include_raw_content": False,
                    "include_images": False,
                    "safe_search": True,
                    "language": language,
                    "include_usage": True,
                },
            )
        finally:
            requests_allowed.reset(permission_token)
        rows = data.get("results")
        if not isinstance(rows, list):
            raise TavilyError("invalid_response", "В ответе Tavily нет списка результатов поиска.")
        urls = []
        for row in rows[:5]:
            try:
                if isinstance(row, dict) and row.get("url"):
                    urls.append(web_url(str(row["url"])))
            except ValueError:
                continue
        return urls

    async def check(self):
        permission_token = requests_allowed.set(True)
        try:
            data = await self.request("GET", "/usage")
        finally:
            requests_allowed.reset(permission_token)
        key = data.get("key")
        if not isinstance(key, dict):
            raise TavilyError("invalid_response", "Tavily не подтвердил сведения о ключе.")
        used, limit = key.get("usage"), key.get("limit")
        if type(used) not in {int, float} or (
            limit is not None and type(limit) not in {int, float}
        ):
            raise TavilyError(
                "invalid_response", "Tavily вернул неподдерживаемые сведения о лимите ключа."
            )
        exhausted = limit is not None and used >= limit
        return {
            "provider": "tavily",
            "ok": not exhausted,
            "authenticated": True,
            "code": "credits_exhausted" if exhausted else "connected",
            "message": "Подключение подтверждено, но лимит кредитов этого ключа Tavily исчерпан."
            if exhausted
            else "Tavily подключён. Ключ принят; проверка выполнена без поискового запроса.",
            "usage": used,
            "limit": limit,
        }
