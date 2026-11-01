"""Long-term memory: facts the user asked Jarvis to keep across conversations."""

from __future__ import annotations

import re

from backend.context import request_context
from backend.memory.store import MemoryStore
from backend.security import command_text, explicit_request
from backend.tools.registry import Tool, ToolError, user_text

MAX_FACTS = 100  # all of them go into the system prompt
MAX_FACT_CHARS = 300
# Without it, qwen3 confirmed with "A nota foi salva com sucesso. Se precisar de algo mais...".
CONFIRM = (
    "confirm briefly in the reply language, e.g. 'Запомнил.' / 'Noted.'; do not repeat the fact"
)


_ASKS = re.compile(
    r"\b(?:lembr|record|anot|guard|grav|memoriz|registr|esque[cç]|corrig|corrij|atualiz"
    r"|remember|note|jot|keep in mind|forget|correct|update|save"
    r"|запомн|запиш|сохран|забуд|удали|исправ|обнов)",
    re.IGNORECASE,
)
_VOCATIVE = r"^(?:[\w ]{1,20},\s*)?"
_RECALL_QUESTION = re.compile(
    _VOCATIVE + r"(?:(?:você|vc|tu)\s+)?(?:(?:ainda|se)\s+)*(?:lembra|recorda|sabe)\b"
    r"|" + _VOCATIVE + r"o\s+que\s+(?:você\s+)?(?:lembra|sabe|guardou|anotou)\b"
    r"|" + _VOCATIVE + r"(?:do|did)\s+you\s+(?:still\s+)?(?:remember|recall|know)\b"
    r"|" + _VOCATIVE + r"what\s+do\s+you\s+(?:remember|know)\b"
    r"|" + _VOCATIVE + r"(?:(?:ты|вы)\s+)?(?:помнишь|помните|знаешь|знаете)\b"
    r"|" + _VOCATIVE + r"что\s+(?:(?:ты|вы)\s+)?(?:помнишь|помните|знаешь|знаете|сохранено)\b",
    re.IGNORECASE,
)


def asks_to_remember(text: str) -> bool:
    """True when the user asked to keep or correct something, not just whether Jarvis knows.

    Asked "Você lembra como eu tomo café?" with nothing on coffee in memory, qwen3 called
    remember 3 times in 3 and stored a fact it made up.
    """
    t = " ".join(text.split())
    if t.endswith("?") and _RECALL_QUESTION.search(t):
        return False
    return _ASKS.search(t) is not None


class LongTermMemory:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def prepare_remember(self, arguments: dict) -> dict:
        from backend.memory.profile import safe_memory

        if not safe_memory(arguments["fact"]):
            raise ToolError("Секреты нельзя записывать в память.")
        return arguments

    async def remember(
        self, fact: str, replaces: int | None = None, category: str = "profile"
    ) -> dict:
        fact = " ".join((fact or "").split())
        if not fact:
            raise ToolError("nothing to remember: pass the fact as a short sentence")
        if len(fact) > MAX_FACT_CHARS:
            raise ToolError(f"too long: keep it under {MAX_FACT_CHARS} characters")
        if (said := user_text.get()) is not None and not asks_to_remember(said):
            raise ToolError(
                "not saved: the user did not ask you to remember anything. Answer from the "
                "memory list; if it is not there, say you have nothing noted on it"
            )
        # One call for a correction: asked to "fix it", qwen3 added the new fact and kept
        # the old one, then said the user had two daughters, "Ana and Ana Luísa".
        replaced = None
        if replaces is not None:
            replaces = _as_id(replaces)
            old = dict(self.store.facts()).get(replaces)
            if old is None:
                raise ToolError(f"no memory with id {replaces}")
            replaced = old
        elif len(self.store.facts()) >= MAX_FACTS:
            raise ToolError("memory is full; ask the user which memory to forget first")
        from backend.memory.profile import _SENSITIVE, ProfileMemory

        sensitive = bool(_SENSITIVE.search(fact))
        with self.store.memory_transaction():
            fact_id, new = (
                self.store.replace_fact(replaces, fact)
                if replaces is not None
                else self.store.save_memory(fact, category=category, sensitive=sensitive)
            )
            self.store.db.execute(
                "UPDATE facts SET sensitive=?, source=? WHERE id=?",
                (int(sensitive), ProfileMemory(self.store).source("explicit_user"), fact_id),
            )
        out: dict = {"ok": True, "id": fact_id, "already_known": not new}
        if replaced:
            out["replaced"] = replaced
        return {**out, "note": CONFIRM}

    def prepare_forget(self, arguments: dict) -> dict:
        """Resolve one real fact before asking for confirmation, then bind its identity."""
        id, text = arguments.get("id"), arguments.get("text")
        facts = self.store.facts()
        if id is not None:
            targets = [f for f in facts if f[0] == _as_id(id)]
        elif text and text.strip():
            needle = text.strip().casefold()
            targets = [f for f in facts if needle in f[1].casefold()]
        else:
            raise ToolError("say which memory to forget (its id or part of its text)")
        if not targets:
            raise ToolError("no memory matches")
        if len(targets) > 1:
            raise ToolError("several memories match; pick one by id")
        context = request_context.get()
        said = context.text if context else user_text.get() or ""
        command = command_text(said)
        match = re.match(
            r"(?:забудь|удали|сотри|forget|delete|remove|esqueça|esqueca|apague)\b[ ,:]*",
            command,
        )
        if not match or not explicit_request("forget", arguments, said):
            raise ToolError("Нужна явная просьба удалить конкретный факт из памяти.")
        target_id, content = targets[0]
        target = command[match.end() :]
        ids = re.findall(r"(?<!\w)\d+(?!\w)", target)
        if ids:
            specific = ids == [str(target_id)]
        else:
            stop = {
                "что",
                "это",
                "этот",
                "факт",
                "памяти",
                "память",
                "меня",
                "про",
                "люблю",
                "любит",
                "remember",
                "memory",
                "the",
                "that",
                "this",
            }
            words = [w for w in re.findall(r"\w+", target) if len(w) >= 3 and w not in stop]
            specific = bool(words) and any(w in content.casefold() for w in words)
        if not specific:
            raise ToolError("Уточни, какой факт удалить: назови его номер или содержание.")
        return {"id": target_id}

    def forget_summary(self, arguments: dict) -> str:
        content = dict(self.store.facts()).get(arguments["id"])
        if content is None:
            raise ToolError("no memory matches")
        return f"удалить факт №{arguments['id']} из памяти: «{content}»"

    def remember_summary(self, arguments: dict) -> str:
        if arguments.get("replaces"):
            old = dict(self.store.facts()).get(arguments["replaces"])
            if old is None:
                raise ToolError("no memory with id")
            return f"заменить факт №{arguments['replaces']} «{old}» на «{arguments['fact']}»"
        return f"сохранить факт: «{arguments['fact']}»"

    async def forget(self, id: int | None = None, text: str | None = None) -> dict:
        target = self.prepare_forget({"id": id, "text": text})["id"]
        content = dict(self.store.facts())[target]
        self.store.forget_fact(target)
        return {"ok": True, "forgotten": content, "note": CONFIRM}


def _as_id(value: object) -> int:
    """Models sometimes send ids as strings ("3", "[3]")."""
    try:
        return int(str(value).strip("[] "))
    except ValueError:
        raise ToolError(f"not a memory id: {value!r}") from None


def memory_tools(store: MemoryStore) -> list[Tool]:
    memory = LongTermMemory(store)
    cap = {
        "ru": "запоминать, исправлять и удалять факты по просьбе пользователя",
        "pt": "guardar e esquecer fatos sobre a pessoa entre conversas (memória de longo prazo)",
        "en": "remember and forget facts about the user across conversations (long-term memory)",
    }
    return [
        Tool(
            name="remember",
            description=(
                "Keep a fact for future conversations. Call only when the user asks you to "
                "remember or note something ('lembre que...', 'guarde isso', 'remember that...'), "
                "not for anything said in passing."
            ),
            handler=memory.remember,
            prepare=memory.prepare_remember,
            summary=memory.remember_summary,
            parameters={
                "fact": {
                    "type": "string",
                    "description": (
                        "One short, self-contained sentence in the conversation's language, "
                        "about the user in the third person, e.g. 'Prefere café sem açúcar.'"
                    ),
                },
                "replaces": {
                    "type": "integer",
                    "description": (
                        "When this corrects or updates a remembered fact, that fact's [id]: "
                        "the old one is deleted"
                    ),
                },
                "category": {
                    "type": "string",
                    "enum": ["profile", "preference", "habit", "project", "episode", "correction"],
                },
            },
            required=["fact"],
            capability=cap,
        ),
        Tool(
            name="forget",
            description=(
                "Delete a remembered fact when the user asks you to forget it or says it is no "
                "longer true. Pick it by its [id] in the memory list, or by part of its text. "
                "To correct a fact, use remember with `replaces` instead."
            ),
            handler=memory.forget,
            prepare=memory.prepare_forget,
            summary=memory.forget_summary,
            parameters={
                "id": {"type": "integer"},
                "text": {"type": "string"},
            },
            capability=cap,
        ),
    ]
