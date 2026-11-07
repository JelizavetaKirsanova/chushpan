"""Candidate discovery in a short-lived Chromium context using public search forms."""

import asyncio
import contextlib
from urllib.parse import urlsplit

from backend.cinema.browser_diagnostics import (
    INSPECT,
    BrowserDiagnostics,
    attach_public_routes,
    configure_browser_cache,
)
from backend.cinema.evidence import public_url, safe_reason
from backend.cinema.internet import InternetSearch, PageVerifier
from backend.cinema.models import origin
from backend.tools.registry import ToolError

# Only configured, enabled sites with a reviewed public navigation flow are
# searched automatically. The separate diagnostic tool supports other sources.
SUPPORTED = {"filmix.gg", "kinogo.is", "hdrezka.sh", "kion.ru"}


class SiteBrowserSearch:
    async def search(self, query, prefs, sources, *, explicit=False):
        known = [
            source
            for source in sources
            if source.get("enabled")
            and (host := urlsplit(source.get("base_url", "")).hostname) in SUPPORTED
            and (explicit or host != "kion.ru")
        ][:4]
        if not known:
            return []
        from playwright.async_api import Error as BrowserError
        from playwright.async_api import async_playwright

        configure_browser_cache()
        errors = InternetSearch(None, None, name="Поиск на сайтах")
        driver = async_playwright()
        try:
            async with asyncio.timeout(28), driver as runtime:
                browser = await runtime.chromium.launch(headless=True)
                try:
                    return await asyncio.gather(
                        *(
                            self.probe(
                                browser, source, {**query, "region": prefs.watch_region}, errors
                            )
                            for source in known
                        )
                    )
                finally:
                    await browser.close()
        except (BrowserError, OSError, TimeoutError) as exc:
            if isinstance(exc, TimeoutError):
                message = "Истекло время браузерного поиска по сайтам."
            elif "Executable doesn't exist" in str(exc):
                message = "Для поиска по сайтам установи Chromium в настройках диагностики."
            else:
                message = "Изолированный браузер не удалось запустить."
            return [errors.error(message)]
        finally:
            # Also stop the driver if cancellation interrupted __aenter__.
            with contextlib.suppress(Exception):
                await driver.__aexit__(None, None, None)

    @staticmethod
    async def probe(browser, source, query, errors):
        base = origin(source["base_url"])
        context = None
        page = None
        try:
            async with asyncio.timeout(22):
                context = await browser.new_context(
                    accept_downloads=False, service_workers="block", locale="ru-RU"
                )
                await attach_public_routes(context)
                page = await context.new_page()
                page.set_default_timeout(4000)
                context.on("page", lambda popup: asyncio.create_task(popup.close()))
                response = await page.goto(base, wait_until="domcontentloaded", timeout=10000)
                if response and response.status >= 400:
                    raise ToolError(f"Браузер: HTTP {response.status} при открытии сайта.")
                if origin(page.url) != base:
                    raise ToolError("Сайт перенаправил в другой домен.")
                state = await page.evaluate(INSPECT)
                if state["restricted"] or state["geo_restricted"] or state["login"]:
                    raise ToolError("Сайт требует входа, проверки браузера или ограничил доступ.")
                await BrowserDiagnostics.find_title(page, query)
                if origin(page.url) != base:
                    raise ToolError("Результат поиска перенаправляет в другой домен.")
                state = await page.evaluate(INSPECT)
                if state["restricted"] or state["geo_restricted"] or state["login"]:
                    raise ToolError("Результат требует входа или ограничивает доступ.")
                row = PageVerifier.inspect(await page.content(), page.url, query)
                row.update(
                    source_id=source["id"],
                    source_name=source["name"],
                    search_provider="Публичная форма сайта · Playwright",
                    access_type=source["access_type"],
                    access_status=source["access_status"]
                    if source["access_status"] != "MIXED"
                    else row["access_status"],
                )
                return row
        except (ToolError, TimeoutError, ValueError) as exc:
            row = errors.error(safe_reason(exc) or "Истекло время проверки страницы.")
        except Exception as exc:
            row = errors.error("Браузерная проверка сайта: " + type(exc).__name__)
        finally:
            if context:
                with contextlib.suppress(Exception):
                    await context.close()
        row.update(
            source_name=source["name"],
            search_provider="Публичная форма сайта · Playwright",
            checked_url=public_url(page.url) if page else public_url(base),
        )
        return row
