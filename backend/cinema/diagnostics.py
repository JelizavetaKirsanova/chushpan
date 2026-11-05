"""Source checks separate HTTP/identity evidence from opt-in real player trials.

Trials use adapter sessions without Cinema.save: no viewing history, ratings or
episode positions are written by a diagnostic. Results remain in private SQLite.
"""

import asyncio
import contextlib
import hashlib
import json
import secrets
from datetime import UTC, datetime

import httpx

from backend.cinema.adapters import HTML5Adapter, SafariBridge
from backend.cinema.classification import FULL_TYPES, PROMO_TYPES
from backend.cinema.playback import valid_sample
from backend.permission_policy import requests_allowed
from backend.tools.registry import ToolError

STAGES = ("site", "content", "player", "playback", "pause", "seek", "fullscreen", "resume")


class DiagnosticSafariBridge(SafariBridge):
    """Refuse to disturb a tab that was already open before the explicit trial."""

    def __init__(self, settings, executor=None):
        super().__init__(settings, executor)
        self.owned = set()

    async def ensure_page(self, url, opener):
        script = (
            'tell application "Safari"\nset matches to 0\nrepeat with w in windows\n'
            "repeat with t in tabs of w\nif URL of t is "
            + json.dumps(url)
            + " then set matches to matches + 1\nend repeat\nend repeat\n"
            "return matches as string\nend tell"
        )
        token = requests_allowed.set(False)
        try:
            existing = await self.executor._run(script)
        finally:
            requests_allowed.reset(token)
        if url not in self.owned and existing != "0":
            raise ToolError(
                "Эта страница уже открыта в Safari. Проверка не меняла её воспроизведение. "
                "Закрой её перед отдельным испытанием или используй обычный просмотр."
            )
        await super().ensure_page(url, opener)
        self.owned.add(url)

    async def release(self):
        self.owned.clear()


def stamp():
    return datetime.now(UTC).isoformat(timespec="seconds")


def fingerprint(source):
    return hashlib.sha256(
        json.dumps(
            {k: v for k, v in source.items() if k not in {"health", "capabilities"}}, sort_keys=True
        ).encode()
    ).hexdigest()


class SourceDiagnostics:
    def __init__(self, sources, db, changed, safari_factory, native_factory):
        self.sources, self.db, self.changed = sources, db, changed
        self.safari_factory, self.native_factory = safari_factory, native_factory
        self.running = False
        self.rows = []

    async def publish(self):
        for row in self.rows:
            if row.get("last_error"):
                error = row["last_error"]
                error.setdefault("checked_at", stamp())
                history = row.setdefault("error_history", [])
                if not history or history[-1] != error:
                    history.append(dict(error))
                row["error_history"] = history[-20:]
            stages = row["stages"]
            row["verification_status"] = (
                "requires_authorization"
                if stages["site"].get("http_status") == 401
                else "unavailable"
                if stages["site"]["status"] == "failed"
                else "working"
                if all(stages[k]["status"] == "confirmed" for k in STAGES)
                else "partial"
                if any(s["status"] == "confirmed" for s in stages.values())
                else "not_verified"
            )
        await self.db("source_diagnostics", {"running": self.running, "rows": self.rows})
        await self.changed()

    def stage(self, row, name, status, message, **evidence):
        row["stages"][name] = {
            "status": status,
            "message": message,
            "method": "http" if name in {"site", "content"} else "player",
            "checked_at": stamp(),
            **evidence,
        }

    def error(self, row, stage, exc, status="failed"):
        code = getattr(exc, "code", type(exc).__name__)
        # Do not persist exception strings containing scripts, tokens or response bodies.
        message = (
            str(exc)[:700]
            if isinstance(exc, ToolError)
            else f"Проверка не завершена: {type(exc).__name__}."
        )
        self.stage(row, stage, status, message, code=code)
        row["last_error"] = {"stage": stage, "code": code, "message": message}

    async def check(self, title_id=None):
        if self.running:
            raise ToolError("Дождись завершения проверки источников.")
        item = await self.db("detail", title_id) if title_id else None
        episode = item["current"] if item else None
        query = (
            {**item, "season": episode["season"], "episode": episode["episode"]} if item else None
        )
        sources = [s for s in await self.db("sources") if s["enabled"] and s["adapter"] != "local"][
            :50
        ]
        if self.running:
            raise ToolError("Дождись завершения проверки источников.")
        self.running = True
        previous = {r["source_id"]: r for r in (await self.db("source_diagnostics"))["rows"]}
        self.rows = [
            {
                "source_id": s["id"],
                "name": s["name"],
                "url": s["base_url"],
                "fingerprint": fingerprint(s),
                "title_id": title_id,
                "episode_id": episode["id"] if episode else None,
                "title": item["title"] if item else "",
                "stages": {
                    k: {"status": "not_checked", "message": "Проверка ещё не выполнена."}
                    for k in STAGES
                },
                "last_error": None,
                "candidate": None,
                "playback_confirmed_at": None,
                "error_history": previous.get(s["id"], {}).get("error_history", [])[-20:],
                "last_successful_at": previous.get(s["id"], {}).get("last_successful_at"),
            }
            for s in sources
        ]
        semaphore = asyncio.Semaphore(3)

        async def probe(source, row):
            async with semaphore:
                try:
                    async with asyncio.timeout(80):
                        adapter = self.sources.adapter(source)
                        check = await adapter.checkAvailability()
                        self.stage(
                            row,
                            "site",
                            "confirmed"
                            if check["availability"] == "available"
                            else "needs_user"
                            if check["availability"] in {"unknown", "restricted"}
                            else "failed",
                            check["reason"],
                            http_status=check.get("http_status"),
                        )
                        if check["availability"] != "available":
                            row["last_error"] = {
                                "stage": "site",
                                "code": check.get(
                                    "error_code", str(check.get("http_status", "unknown"))
                                ),
                                "message": check["reason"],
                            }
                        await self.publish()
                        if not query:
                            self.stage(
                                row,
                                "content",
                                "needs_user",
                                "Выбери фильм или сериал для проверки поиска.",
                            )
                            return
                        links = [
                            link for link in episode["links"] if link["source_id"] == source["id"]
                        ]
                        results = []
                        for link in links[:3]:
                            results.append(
                                await adapter.resolve(
                                    {
                                        "url": link["url"],
                                        "kind": "page",
                                        "match": 1.0,
                                        "_expected": query,
                                    }
                                )
                            )
                        if not results or not any(r.get("content_verified") for r in results):
                            results.extend(await adapter.search(query))
                        candidates = [
                            r
                            for r in results
                            if r.get("kind") == "page"
                            and r.get("url")
                            and r.get("match", 0) >= 0.95
                            and r.get("video_type") not in PROMO_TYPES
                        ]
                        if not candidates:
                            reasons = [r.get("reason", "") for r in results if r.get("reason")]
                            self.stage(
                                row,
                                "content",
                                "needs_user",
                                " ".join(reasons[:2])
                                or "Точная страница не найдена; нужен ручной выбор.",
                            )
                            return
                        row["candidate"] = max(
                            candidates,
                            key=lambda r: (
                                r.get("content_verified") is True,
                                r.get("availability") == "available",
                            ),
                        )
                        row["page_url"] = row["candidate"]["url"]
                        row["video_type"] = row["candidate"].get("video_type", "unknown")
                        self.stage(
                            row,
                            "content",
                            "confirmed"
                            if row["candidate"].get("content_verified")
                            else "needs_user",
                            "Найдена страница выбранного названия. "
                            + (
                                "Проверена привязка к полному видео."
                                if row["candidate"].get("content_verified")
                                else "Полное видео ещё не подтверждено; "
                                "трейлер запускаться не будет."
                            ),
                        )
                        self.stage(
                            row,
                            "player",
                            "needs_user",
                            "Нажми «Проверить плеер»: откроется выбранный источник "
                            "и будет проверено реальное видео.",
                        )
                except (ToolError, OSError, TimeoutError, ValueError, httpx.HTTPError) as exc:
                    self.error(row, "content" if query else "site", exc, "needs_user")
                finally:
                    await self.publish()

        try:
            await self.publish()
            await asyncio.gather(
                *(probe(s, row) for s, row in zip(sources, self.rows, strict=True))
            )
        finally:
            self.running = False
            await self.publish()

    async def live(self, source_id, engine="safari"):
        if self.running:
            raise ToolError("Дождись завершения предыдущей проверки.")
        if engine not in {"safari", "native"}:
            raise ValueError("Неизвестный плеер диагностики.")
        report = await self.db("source_diagnostics")
        self.rows = report.get("rows", [])
        row = next((r for r in self.rows if r["source_id"] == source_id), None)
        source = (await self.db("source", source_id)).model_dump()
        if not source["enabled"]:
            raise ToolError("Источник отключён. Включи его перед проверкой.")
        if not row or not row.get("candidate") or row.get("fingerprint") != fingerprint(source):
            raise ToolError("Сначала проверь источники для выбранного названия заново.")
        item = await self.db("detail", row["title_id"])
        episode = next(e for e in item["episodes"] if e["id"] == row["episode_id"])
        query = {**item, "season": episode["season"], "episode": episode["episode"]}
        candidate = await self.sources.adapter(source).resolve(
            {**row["candidate"], "_expected": query}
        )
        if engine == "native" and not (
            candidate.get("embedded")
            and candidate.get("content_verified")
            and candidate.get("video_type") in FULL_TYPES
        ):
            raise ToolError("Для AVKit нужен разрешённый проверенный прямой видеоисточник.")
        if self.running:
            raise ToolError("Дождись завершения предыдущей проверки.")
        adapter = self.native_factory() if engine == "native" else self.safari_factory()
        trial_bridge = adapter.bridge if isinstance(adapter, HTML5Adapter) else None
        original = max(0, episode["position"])
        target = {
            "item": item,
            "episode": {**episode, "position": original},
            "link": {
                "url": candidate.get("media_url", candidate["url"])
                if engine == "native"
                else candidate.get("safari_url", candidate["url"]),
                "voice": "",
            },
            "tracking": {"session": secrets.token_urlsafe(24), "callback": "", "poll": 5},
            "autoplay": False,
            "direct_verification": candidate
            if candidate.get("provider") or source["adapter"] == "direct"
            else None,
        }
        row["engine"] = engine
        row["last_error"] = None
        row["playback_confirmed_at"] = None
        for phase in STAGES[2:]:
            self.stage(row, phase, "not_checked", "Проверка ещё не выполнена.")
        phase = "player"
        self.running = True
        try:
            await self.publish()
            async with asyncio.timeout(120):
                await adapter.open(target)
                state = await adapter.sample()
                if not valid_sample(state):
                    raise ToolError("Плеер не вернул корректную позицию и длительность.")
                self.stage(
                    row,
                    "player",
                    "confirmed",
                    "Обнаружен доступный плеер с конечной длительностью.",
                    duration=state["duration"],
                )
                phase = "content"
                if engine == "safari" and not state.get("content_verified"):
                    raise ToolError(
                        state.get(
                            "classification_reason", "Соответствие полного видео не подтверждено."
                        )
                    )
                await adapter.control("pause")
                phase = "playback"
                before = await adapter.sample()
                played = await adapter.control("play")
                if (
                    not played.get("playback_confirmed")
                    or played["position"] <= before["position"] + 0.05
                ):
                    raise ToolError("Время видео не увеличивается; запуск не подтверждён.")
                row["playback_confirmed_at"] = stamp()
                self.stage(
                    row,
                    "playback",
                    "confirmed",
                    "Начало воспроизведения подтверждено движением времени.",
                    before=before["position"],
                    after=played["position"],
                )
                await self.publish()
                for phase, action, position in [
                    ("pause", "pause", 0),
                    ("seek", "seek", min(state["duration"] - 1, max(original + 10, 20))),
                    ("fullscreen", "fullscreen", 0),
                ]:
                    try:
                        observed = await adapter.control(action, position)
                        self.stage(
                            row,
                            phase,
                            "confirmed",
                            "Плеер подтвердил действие.",
                            position=observed.get("position"),
                        )
                    except (ToolError, OSError, TimeoutError) as exc:
                        self.error(
                            row, phase, exc, "needs_user" if phase == "fullscreen" else "failed"
                        )
                phase = "resume"
                resume_at = min(state["duration"] - 1, original if original > 0 else 30)
                await adapter.detach()
                adapter = (
                    self.native_factory()
                    if engine == "native"
                    else HTML5Adapter(trial_bridge)
                    if trial_bridge
                    else self.safari_factory()
                )
                await adapter.open(
                    {**target, "episode": {**target["episode"], "position": resume_at}}
                )
                observed = await adapter.sample()
                if not valid_sample(observed) or abs(observed["position"] - resume_at) >= 2:
                    raise ToolError("Плеер не подтвердил восстановление сохранённой позиции.")
                self.stage(
                    row,
                    "resume",
                    "confirmed",
                    "Новая сессия адаптера восстановила позицию.",
                    expected=resume_at,
                    observed=observed["position"],
                )
                if all(row["stages"][k]["status"] == "confirmed" for k in STAGES):
                    row["last_successful_at"] = stamp()
                    await self.db("source_health", source_id, "available", playback=True)
        except (ToolError, OSError, TimeoutError, ValueError, httpx.HTTPError) as exc:
            self.error(row, phase, exc, "needs_user")
        finally:
            may_touch_player = (
                engine == "native"
                or trial_bridge is None
                or bool(getattr(trial_bridge, "owned", set()))
            )
            with contextlib.suppress(ToolError, OSError, TimeoutError, ValueError):
                if may_touch_player:
                    state = await adapter.sample()
                    if valid_sample(state) and (
                        engine == "native" or state.get("content_verified")
                    ):
                        await adapter.control("pause")
                        await adapter.control("seek", min(original, state["duration"] - 0.1))
            try:
                if may_touch_player:
                    await adapter.detach()
            finally:
                if trial_bridge and hasattr(trial_bridge, "release"):
                    await trial_bridge.release()
                self.running = False
                await self.publish()
