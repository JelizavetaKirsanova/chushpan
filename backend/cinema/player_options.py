"""Interpret displayed player choices; never infer quality from the Mac screen."""

import re


def quality_height(value):
    if re.fullmatch(r"\s*4K(?:\s*UHD)?\s*", value, re.I):
        return 2160
    match = re.fullmatch(
        r"\s*(4320|2160|1440|1080|900|720|576|540|480|360|240|144)p?(?:60(?:fps)?)?(?:\s*(?:HD|FHD|UHD|4K|HDR))?\s*",
        value,
        re.I,
    )
    return int(match[1]) if match else 0


def russian_voice(value, original_language=""):
    if re.search(r"укр|ukr|eng\b|english|commentary|субтитр|subtitles", value, re.I):
        return False
    if re.search(r"original|ориг", value, re.I):
        return original_language == "ru" or bool(re.search(r"рус", value, re.I))
    return bool(
        re.search(
            r"[а-яё]|lost\W?film|newstudio|hdrezka|alexfilm|baibako|coldfilm|jaskier", value, re.I
        )
    )


def preferred_voice(current, available, *, preferred="", original_language="", playing=False):
    """A suitable playing track wins; LostFilm is a default, never a requirement."""
    choices = list(dict.fromkeys(available))

    def suitable(value):
        return russian_voice(value, original_language)

    if playing and suitable(current):
        return current
    if preferred in choices and suitable(preferred):
        return preferred
    lostfilm = next((v for v in choices if re.fullmatch(r"lost\W?film", v, re.I)), "")
    return lostfilm or next((v for v in choices if suitable(v)), current or next(iter(choices), ""))


class QualityController:
    """Bounded ordinary menu actions in the owner's already verified player."""

    def __init__(self, owner):
        self.owner = owner
        self.button = None
        self.previous = ""

    @staticmethod
    def choices(nodes):
        result = {}
        for node in nodes:
            value = node.get("value") or node.get("title") or ""
            if quality_height(value) or value.lower() in {"auto", "авто"}:
                if value in result:
                    return {}  # Ambiguous display is not an actionable menu.
                result[value] = node
        return result

    async def close(self):
        if self.button:
            await self.owner.press_settings(self.button)
            self.button = None

    async def menu(self):
        import re

        p = self.owner
        await p.reveal()
        nodes = await p.nodes()
        self.button = await p.action_button(nodes, r"Настройки|Settings|Качество|Quality")
        if not self.button:
            return {}, ""
        await p.press_settings(self.button)
        nodes = await p.wait_nodes(
            lambda tree: (
                bool(self.choices(tree))
                or any(re.fullmatch(r"Качество|Quality", n.get("value", ""), re.I) for n in tree)
            )
        )
        values = self.choices(nodes)
        current = next(iter(values), "") if len(values) == 1 else ""
        if current and any(
            re.fullmatch(r"Качество|Quality", n.get("value", ""), re.I) for n in nodes
        ):
            await p.press(values[current])
            nodes = await p.wait_nodes(lambda tree: bool(self.choices(tree)))
            values = self.choices(nodes)
        return values, current

    async def apply(self, wanted):
        import asyncio

        from backend.tools.registry import ToolError

        values, current = await self.menu()
        if wanted not in values:
            raise ToolError("Выбранное качество исчезло из меню плеера.")
        await self.owner.press(values[wanted])
        # In the reviewed settings panel a single displayed resolution is the
        # applied value. A menu merely containing the option proves nothing.
        for _ in range(15):
            rows = self.choices(await self.owner.nodes())
            if list(rows) == [wanted]:
                return current
            await asyncio.sleep(0.2)
        raise ToolError("Плеер не подтвердил применение выбранного качества.")

    async def select_maximum(self):
        import asyncio
        import contextlib

        from backend.tools.registry import ToolError

        p = self.owner
        before = await p.sample()
        available = []
        wanted = ""
        error = ""
        for _attempt in range(2):
            try:
                async with asyncio.timeout(24):
                    values, current = await self.menu()
                    available = list(values)
                    if not values:
                        if _attempt == 0:
                            # Controls can still be changing after a seek or
                            # stream reload. Retry discovery once on this page.
                            await asyncio.sleep(0.5)
                            continue
                        return {"status": "unavailable", "maximum_confirmed": False}
                    if not self.previous:
                        self.previous = current
                    wanted = max(values, key=quality_height)
                    if not quality_height(wanted):
                        return {
                            "status": "auto",
                            "selected": current or wanted,
                            "available": available,
                            "maximum_confirmed": False,
                        }
                    if current != wanted:
                        await p.press(values[wanted])
                        await p.wait_nodes(
                            lambda tree, value=wanted: list(self.choices(tree)) == [value]
                        )
                    await self.close()
                    result = await p.confirm(await p.nodes())
                    if abs(result["position"] - before["position"]) > 4:
                        await p.seek(before["position"])
                        result = await p.confirm(await p.nodes())
                    p.last_sample = result
                    return {
                        "status": "selected",
                        "selected": wanted,
                        "available": available,
                        "maximum_confirmed": True,
                        "playback_confirmed": True,
                    }
            except (ToolError, TimeoutError) as exc:
                error = str(exc) or "Истекло ожидание качества плеера."
            finally:
                with contextlib.suppress(ToolError, TimeoutError):
                    async with asyncio.timeout(3):
                        await self.close()
        # At most one rollback to the previously displayed working value.
        if self.previous and self.previous != wanted:
            try:
                async with asyncio.timeout(24):
                    await self.apply(self.previous)
                    await self.close()
                    result = await p.confirm(await p.nodes())
                    if abs(result["position"] - before["position"]) > 4:
                        await p.seek(before["position"])
                        result = await p.confirm(await p.nodes())
                    p.last_sample = result
                    return {
                        "status": "fallback",
                        "selected": self.previous,
                        "available": available,
                        "maximum_confirmed": False,
                        "playback_confirmed": True,
                        "error": error,
                    }
            except (ToolError, TimeoutError):
                pass
            finally:
                with contextlib.suppress(ToolError, TimeoutError):
                    async with asyncio.timeout(3):
                        await self.close()
        return {
            "status": "unverified",
            "available": available,
            "maximum_confirmed": False,
            "error": error,
        }
