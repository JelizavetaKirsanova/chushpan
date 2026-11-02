"""Bounded, local extraction and retrieval. Only current user words can create facts."""

from __future__ import annotations

import json
import re
import sqlite3

from backend.brain.privacy import contains_secret, redact
from backend.context import request_context
from backend.memory.store import MemoryStore, _now
from backend.security import command_text
from backend.tools.registry import Tool, ToolError

DEFAULT_CHARACTER = dict(
    sarcasm=7, profanity=3, boldness=6, verbosity=3, initiative=2, technical_detail=7
)
LABELS = dict(
    sarcasm="Сарказм",
    profanity="Мат",
    boldness="Дерзость",
    verbosity="Болтливость",
    initiative="Инициативность",
    technical_detail="Технические объяснения",
)
CATEGORY_LABELS = {
    "profile": "Профиль",
    "preference": "Предпочтение",
    "habit": "Привычка",
    "project": "Проект",
    "episode": "Событие",
    "correction": "Исправление",
    "personality": "Характер",
}
_SENSITIVE = re.compile(
    r"диагноз|болезн|здоровь|беремен|религи|политическ|паспорт|"
    r"адрес|телефон|банков|кредит|сексу|интим|зарплат|доход|аллерг|"
    r"депресс|аутиз|инвалид|лечен|психи|онкол|лекарств|медицин|\bвич\b|\bспид\b|"
    r"этнич|вероиспов|ориентац|национальн|\biban\b|\+?\d[\d ()-]{8,}\d",
    re.I,
)
_UNSURE = re.compile(
    r"может быть|возможно|наверное|кажется|шутк|шучу|допустим|представь|"
    r'цитат|например|если бы|\?|```|[«»"]',
    re.I,
)


def safe_memory(text: str) -> bool:
    return not contains_secret(text)


def character_change(text: str, current: dict) -> dict:
    t = command_text(text).rstrip(".! ")
    exact = {
        "будь понаглее": {"boldness": min(10, current["boldness"] + 2)},
        "поменьше мата": {"profanity": max(0, current["profanity"] - 2)},
        "без мата": {"profanity": 0},
        "отвечай короче": {"verbosity": max(0, current["verbosity"] - 2)},
        "объясняй программирование подробнее": {"technical_detail": 9},
        "хватит меня подкалывать": {"sarcasm": 0, "boldness": 0},
    }
    if t in exact:
        return exact[t]
    for key, label in LABELS.items():
        match = re.fullmatch(re.escape(label.casefold()) + r"\s*(?:на|=|:)\s*(10|[0-9])", t)
        if match:
            return {key: int(match[1])}
    return {}


def extract(text: str) -> list[tuple[str, str, str]]:
    """Conservative supported statements, not an LLM summary or a transcript backfill."""
    t = command_text(text).strip().rstrip(".!")
    t = re.sub(r"^(?:нет,\s*|на самом деле\s+)", "", t)
    if len(t) > 300 or "\n" in text or _UNSURE.search(t) or _SENSITIVE.search(t):
        return []
    if not safe_memory(t):
        return []
    if re.fullmatch(r"я (?:теперь |больше )?(?:не )?учусь (?:в|на) .+", t):
        return [("profile", "education", t)]
    if re.fullmatch(r"я (?:больше )?не хочу .*мобильн.*", t):
        return [("project", "chushpan:mobile", t)]
    if re.fullmatch(r"(?:мой проект|я работаю над проектом) .+", t):
        name = re.match(r"(?:мой проект|я работаю над проектом)\s+([\w-]+)", t)[1]
        return [("project", "project:" + name, t)]
    if re.fullmatch(r"я (?:терпеть не могу|не люблю|предпочитаю|люблю)[, ]+.+", t):
        if not re.search(r"отвеча|официаль|объясня|совет|подкал|кофе|чай|чаю|программирован", t):
            return []
        key = "response_style" if re.search(r"отвеча|официаль|объясня|совет|подкал", t) else t
        if "кофе" in t:
            key = "coffee"
        elif re.search(r"\bча[йюя]\b", t):
            key = "tea"
        return [("preference", key, t)]
    return []


def tokens(text: str) -> set[str]:
    skip = {
        "что",
        "как",
        "где",
        "когда",
        "про",
        "для",
        "или",
        "это",
        "мне",
        "нас",
        "вас",
        "его",
        "она",
        "они",
        "него",
        "была",
        "было",
        "были",
        "будет",
        "так",
        "мой",
        "моя",
        "моих",
        "мои",
        "свои",
        "всех",
        "ещё",
        "еще",
        "какой",
        "какая",
        "меня",
        "тебя",
        "вчера",
        "сегодня",
        "помнишь",
        "напомни",
        "делали",
        "чушпан",
        "который",
        "почему",
        "продолжим",
        "воспоминания",
    }
    result = {w[:6] for w in re.findall(r"[\w]+", text.casefold()) if len(w) >= 3 and w not in skip}
    if re.search(r"уч[её]б|учусь|учится|университет|\bвуз|taltech", text, re.I):
        result.add("education")
    return result


class ProfileMemory:
    def __init__(self, store: MemoryStore):
        self.store = store
        self.selected: dict[tuple[str, str], list[int]] = {}

    def observe(self, name: str, args: dict, result: dict) -> str | None:
        if name not in {"open_app", "night_mode"} or result.get("error"):
            return None
        if not self.store.preference("memory", "habits_opt_in"):
            return None
        if name == "night_mode" and not (result.get("shortcut_started") and result.get("ok")):
            return None
        if name == "open_app" and not result.get("opened") and not result.get("ok"):
            return None
        key = name + (":" + str(args.get("name", "")).casefold() if name == "open_app" else "")
        if len(key) > 140 or not safe_memory(key):
            return None
        event = self.store.preference("habits", key) or {"count": 0, "offered": False}
        event["count"] = min(event["count"] + 1, 100)
        offer = event["count"] >= 3 and not event["offered"] and self.character()["initiative"] > 0
        if offer:
            event["offered"] = True
        self.store.set_preference("habits", key, event)
        if offer:
            self.store.save_memory(
                "Повторяющееся действие: " + key,
                category="habit",
                key="habit:" + key,
                source="opt_in_successful_actions",
                confidence=0.5,
                automatic=True,
            )
            return (
                "Могу предложить отдельный сценарий для этого действия. "
                "Создавать его без твоего согласия не буду."
            )
        return None

    def binding(self) -> tuple[str, str]:
        c = request_context.get()
        return c.binding if c else ("local", "cli")

    def source(self, kind: str) -> str:
        c = request_context.get()
        return json.dumps(
            {
                "kind": kind,
                "request_id": c.request_id if c else None,
                "device": c.device.device_type if c else "local",
            },
            ensure_ascii=False,
        )

    def character(self) -> dict:
        values = DEFAULT_CHARACTER.copy()
        for row in self.store.memories():
            key = row["semantic_key"].removeprefix("character:")
            if row["category"] == "personality" and row["status"] == "active" and key in values:
                try:
                    value = int(row["content"].rsplit(":", 1)[1])
                    if 0 <= value <= 10:
                        values[key] = value
                except (ValueError, IndexError):
                    pass
        return values

    def capture(self, text: str) -> None:
        c = request_context.get()
        if c is None or not c.device.authenticated:
            return
        for category, key, value in extract(text):
            self.store.save_memory(
                value,
                category=category,
                key=key,
                automatic=True,
                source=self.source("current_user_statement"),
                confidence=1,
            )

    def relevant(self, text: str, limit: int = 6, budget: int = 1600) -> list[dict]:
        query = tokens(text)
        rows = [
            r
            for r in self.store.memories()
            if r["status"] == "active"
            and not r["sensitive"]
            and not _SENSITIVE.search(r["content"])
            and safe_memory(r["content"])
            and r["category"] != "personality"
        ]
        resume = bool(re.search(r"продолжим|вчера делали|активн.{0,8}проект", text, re.I))
        global_keys = {"response_style", "chushpan:mobile"}
        ranked = sorted(
            rows,
            key=lambda r: (
                len(query & tokens(r["content"]))
                + (3 if resume and r["category"] == "project" else 0)
                + (2 if r["semantic_key"] in global_keys else 0),
                r["updated_at"],
            ),
            reverse=True,
        )
        result, used = [], 0
        for r in ranked:
            if not (query & tokens(r["content"])) and not (
                (resume and r["category"] == "project") or r["semantic_key"] in global_keys
            ):
                continue
            if len(result) >= limit or used + len(r["content"]) > budget:
                break
            result.append(r)
            used += len(r["content"])
        selected = [
            r["id"]
            for r in result
            if query & tokens(r["content"]) or (resume and r["category"] == "project")
        ]
        if selected:
            self.selected[self.binding()] = selected
            if len(self.selected) > 32:
                del self.selected[next(iter(self.selected))]
        else:
            self.selected.pop(self.binding(), None)
        return result

    def route(self, text: str) -> dict | None:
        t = command_text(text).rstrip(".!? ")
        if t in {
            "что ты только что запомнил",
            "что только что запомнил",
            "что ты сейчас запомнил",
            "что-то только что запомнил",
        }:
            return {"action": "last_saved"}
        changes = character_change(text, self.character())
        if changes:
            return {"action": "character", "value": json.dumps(changes)}
        if t in {"что ты обо мне помнишь", "что ты помнишь обо мне", "покажи память"}:
            return {"action": "list"}
        if t in {"покажи настройки характера", "настройки характера"}:
            return {"action": "character_list"}
        if t in {"очисти память", "удали все воспоминания"}:
            return {"action": "clear"}
        if t in {"можешь замечать мои привычки", "не замечай мои привычки"}:
            return {"action": "habits", "value": "on" if t.startswith("можешь") else "off"}
        if t in {"нет, ты неправильно запомнил", "ты неправильно запомнил"}:
            return {"action": "clarify"}
        if t == "запомни, что теперь я хочу по-другому":
            return {"action": "clarify"}
        match = re.fullmatch(r"(?:исправь|обнови) (?:факт|воспоминание) (\d+)\s*:\s*(.+)", t)
        if match:
            return {"action": "correct", "id": int(match[1]), "value": match[2]}
        match = re.fullmatch(
            r"(?:почему ты так решил|почему ты это помнишь|забудь это|"
            r"это уже неактуально)(?:\s+(?:факт\s+)?(\d+))?",
            t,
        )
        if match:
            action = (
                "explain"
                if t.startswith("почему")
                else ("delete" if t.startswith("забудь") else "stale")
            )
            ids = self.selected.get(self.binding(), [])
            if not match[1] and len(ids) != 1:
                return {"action": "clarify"}
            return {"action": action, "id": int(match[1]) if match[1] else ids[0]}
        if re.match(r"продолжим|напомни,? что мы делали", t):
            return {"action": "recall", "value": text[:300]}
        return None

    def prepare(self, args: dict) -> dict:
        c = request_context.get()
        if c is None or not c.device.authenticated or not self.intent(args, c.text):
            raise ToolError("Нужна точная текущая просьба от авторизованной сессии.")
        if args["action"] in {"delete", "correct", "stale", "explain"}:
            rows = [r for r in self.store.memories() if r["id"] == args.get("id")]
            if not rows:
                raise ToolError("Такого воспоминания нет. Уточни номер.")
            if args.get("revision", rows[0]["revision"]) != rows[0]["revision"]:
                raise ToolError("Воспоминание изменилось. Повтори просьбу.")
            args = {**args, "revision": rows[0]["revision"]}
        if args["action"] == "correct" and (
            not safe_memory(args.get("value", "")) or _SENSITIVE.search(args.get("value", ""))
        ):
            raise ToolError("Для чувствительного факта нужно явное согласие; секреты запрещены.")
        return args

    def summary(self, args: dict) -> str:
        action = args["action"]
        if action == "last_saved":
            return "показать последний действительно сохранённый факт текущего разговора"
        if action == "character":
            values = json.loads(args["value"])
            if character_change(request_context.get().text, self.character()) != values:
                raise ToolError("Параметры не соответствуют текущей просьбе.")
            return "сохранить характер: " + ", ".join(
                f"{LABELS[k]}={v}/10" for k, v in values.items()
            )
        if action == "clear":
            return f"удалить все воспоминания ({len(self.store.memories())}); историю не стирать"
        if args.get("id"):
            row = next(r for r in self.store.memories() if r["id"] == args["id"])
        label = {
            "delete": "удалить",
            "stale": "отметить неактуальным",
            "correct": "исправить",
            "explain": "показать источник",
        }.get(action, action)
        return f"{label} факт №{row['id']} «{redact(row['content'])}»" + (
            " → «" + args["value"] + "»" if action == "correct" else ""
        )
        return "настройка наблюдения привычек: " + args.get("value", action)

    def version(self, args: dict) -> str:
        if args["action"] in {"clear", "character"}:
            return self.store.memory_digest(json.dumps(self.store.memories(), sort_keys=True))
        return str(args.get("revision", ""))

    def intent(self, args: dict, text: str) -> bool:
        routed = self.route(text)
        return routed is not None and all(args.get(k) == v for k, v in routed.items())

    async def execute(
        self, action: str, id: int | None = None, value: str = "", revision: int | None = None
    ) -> dict:
        try:
            return self._execute(action, id, value, revision)
        except sqlite3.Error:
            raise ToolError("SQLite недоступна. Память не изменена; попробуй позже.") from None

    def _execute(self, action: str, id: int | None, value: str, revision: int | None) -> dict:
        if not self.store.memory_available:
            raise ToolError("Долговременная память недоступна; изменения не сохранены.")
        if action == "last_saved":
            context = request_context.get()
            conversation = self.store.session_conversation(context.device.session_id)
            saved = self.store.db.execute(
                "SELECT content FROM messages WHERE conversation_id=? AND role='tool' "
                "AND tool_name='remember' ORDER BY id DESC LIMIT 1",
                (conversation,),
            ).fetchone()
            if saved is None:
                return {"message": "В этом разговоре ещё не было подтверждённого сохранения факта."}
            result = json.loads(saved["content"])
            if not result.get("ok"):
                return {
                    "message": "Последнее сохранение ещё не подтверждено или завершилось ошибкой."
                }
            fact = next((r for r in self.store.memories() if r["id"] == result.get("id")), None)
            if not fact or fact["status"] != "active" or not safe_memory(fact["content"]):
                return {"message": "Последний сохранённый факт уже удалён или недоступен."}
            return {"id": fact["id"], "message": "Запомнил: «" + fact["content"] + "»."}
        if action == "clarify":
            return {"message": "Уточни номер воспоминания и правильный факт. Я не буду гадать."}
        if action == "character_list":
            return {
                "message": "; ".join(f"{LABELS[k]}: {v}/10" for k, v in self.character().items())
            }
        if action == "character":
            with self.store.memory_transaction():
                for key, number in json.loads(value).items():
                    self.store.save_memory(
                        f"{LABELS[key]}: {number}",
                        category="personality",
                        key="character:" + key,
                        source=self.source("explicit_character"),
                    )
            return {"message": "Характер обновил и сохранил."}
        if action == "habits":
            self.store.set_preference("memory", "habits_opt_in", value == "on")
            if value == "off":
                with self.store.db:
                    self.store.db.execute("DELETE FROM preferences WHERE namespace='habits'")
                    rows = self.store.db.execute(
                        "SELECT * FROM facts WHERE category='habit' "
                        "AND source='opt_in_successful_actions'"
                    ).fetchall()
                    for saved in rows:
                        self.store._suppress(saved["content"], saved["semantic_key"])
                    self.store.db.execute(
                        "DELETE FROM facts WHERE category='habit' "
                        "AND source='opt_in_successful_actions'"
                    )
                    if rows:
                        self.store.invalidate_memory_context()
            return {
                "message": "Наблюдение привычек "
                + ("включено." if value == "on" else "выключено; счётчики удалены.")
            }
        rows = self.store.memories()
        row = next((r for r in rows if r["id"] == id), None)
        if action == "list":
            selected = [r for r in rows if not r["sensitive"] and safe_memory(r["content"])][:10]
            self.selected[self.binding()] = [r["id"] for r in selected]
            message = "\n".join(
                f"[{r['id']}] {CATEGORY_LABELS.get(r['category'], 'Факт')} "
                f"({'актуально' if r['status'] == 'active' else 'неактуально'}): {r['content']}"
                for r in selected
            )
            return {"message": message or "О тебе пока нет сохранённых фактов."}
        if action == "recall":
            query = tokens(value)
            selected = [
                r
                for r in self.relevant(value)
                if r["semantic_key"] not in {"response_style", "chushpan:mobile"}
                or query & tokens(r["content"])
            ]
            if len([r for r in selected if r["category"] == "project"]) > 1:
                return {
                    "message": "Есть несколько проектов. Уточни: "
                    + "; ".join(r["content"] for r in selected)
                }
            if selected:
                return {
                    "message": "\n".join(
                        f"[{r['id']}] {r['content']} ({r['updated_at']})" for r in selected
                    )
                }
            terms = sorted(tokens(value))[:4]
            history = []
            if terms:
                floor = self.store.preference("memory", "context_floor") or 0
                q = " OR ".join("lower(content) LIKE ?" for _ in terms)
                history = self.store.db.execute(
                    "SELECT content, created_at FROM messages "
                    "WHERE role='user' AND id>? AND (" + q + ") ORDER BY id DESC LIMIT 3",
                    (floor, *("%" + w + "%" for w in terms)),
                ).fetchall()
            lines = [
                f"{r['created_at']}: {redact(r['content'])[:300]}"
                for r in history
                if safe_memory(r["content"]) and not _SENSITIVE.search(r["content"])
            ]
            return {
                "message": "\n".join(lines)
                if lines
                else "Не нашёл сохранённого контекста. Напомни проект или последнее состояние."
            }
        if action == "clear":
            count = self.store.forget_all_facts()
            return {"message": f"Удалил воспоминания: {count}. История и музыка сохранены."}
        if row is None or row["revision"] != revision:
            raise ToolError("Воспоминание изменилось или удалено.")
        if action == "explain":
            try:
                kind = json.loads(row["source"]).get("kind")
            except (ValueError, AttributeError):
                kind = None
            source = {
                "explicit_correction": "твоё явное исправление",
                "current_user_statement": "твоя реплика в диалоге",
                "explicit_user": "твоя просьба запомнить",
                "explicit_character": "твоя настройка характера",
            }.get(kind, "источник старой записи неизвестен")
            return {
                "message": f"Источник: {source}; создано {row['created_at']}; "
                f"обновлено {row['updated_at']}; уверенность {row['confidence']}. "
                "Содержимое не проверялось независимо."
            }
        if action == "delete":
            if not self.store.forget_fact(id, expected_revision=revision):
                raise ToolError("Воспоминание изменилось; повтори просьбу.")
        elif action == "stale":
            with self.store.db:
                self.store._suppress(row["content"], row["semantic_key"])
                cursor = self.store.db.execute(
                    "UPDATE facts SET status='stale', updated_at=?, revision=revision+1 "
                    "WHERE id=? AND revision=?",
                    (_now(), id, revision),
                )
                if cursor.rowcount != 1:
                    raise ToolError("Воспоминание изменилось; повтори просьбу.")
                self.store.invalidate_memory_context()
        elif action == "correct":
            with self.store.db:
                self.store._suppress(row["content"])
                cursor = self.store.db.execute(
                    "UPDATE facts SET content=?, source=?, updated_at=?, "
                    "revision=revision+1, status='active' WHERE id=? AND revision=?",
                    (value, self.source("explicit_correction"), _now(), id, revision),
                )
                if cursor.rowcount != 1:
                    raise ToolError("Воспоминание изменилось; повтори просьбу.")
                self.store.invalidate_memory_context()
        return {"message": "Воспоминание " + ("удалил." if action == "delete" else "обновил.")}

    def tool(self) -> Tool:
        return Tool(
            "manage_memory",
            "Профиль, просмотр, исправление, удаление и параметры характера. "
            "Изменения только по точной текущей просьбе и коду подтверждения.",
            self.execute,
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "list",
                        "recall",
                        "last_saved",
                        "explain",
                        "correct",
                        "delete",
                        "clear",
                        "stale",
                        "character",
                        "character_list",
                        "clarify",
                        "habits",
                    ],
                },
                "id": {"type": "integer", "minimum": 1},
                "revision": {"type": "integer", "minimum": 1},
                "value": {"type": "string", "maxLength": 300},
            },
            ["action"],
            intent=self.intent,
            prepare=self.prepare,
            summary=self.summary,
            version=self.version,
            reply=lambda r: r["message"],
        )
