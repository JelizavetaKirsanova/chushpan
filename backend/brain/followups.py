"""Bounded context and explicit proposals for a small allowlist of safe actions."""

import json
import re
import time
from uuid import uuid4

from backend.brain.privacy import redact
from backend.dialogue import normalize
from backend.security import command_text

SHORT = {
    "да",
    "давай",
    "давай включай",
    "давай включи",
    "да включай",
    "включай",
    "давай открой",
    "да открой",
    "давай сделай",
    "да сделай",
    "давай попробуй",
    "да пожалуйста",
    "давай пожалуйста",
    "давай посмотри",
    "посмотри",
    "сделай",
    "открой",
    "попробуй",
    "попробуй еще раз",
    "попробуй ещё раз",
    "повтори попытку",
    "да посмотри",
}


def is_short(text):
    return not re.search(r'[«»"“”`\n;]', text) and normalize(text) in SHORT


def playlist_proposal(text, tools):
    # A read-only suggestion is data, never a grant to play, assign or launch a Shortcut.
    if "music_playlist" not in tools:
        return None
    if re.search(r"\b(?:или|либо)\b", text, re.I):
        return None
    if re.search(
        r"(?:^|[.!?]\s+)(?:я )?могу (?:просто )?"
        r"(?:посмотреть|показать|открыть|попробовать (?:ещ[её] раз )?посмотреть) "
        r"(?:список )?(?:доступные |доступных |твои |твоих |ваши |ваших )?плейлист(?:ы|ов)"
        r"(?:[.!?]|$)",
        " ".join(text.split()),
        re.I,
    ):
        return {
            "tool": "music_playlist",
            "arguments": {"action": "list"},
            "expires": time.time() + 600,
        }
    return None


def safe_followup(name, arguments):
    """A proposal cannot authorize arbitrary tools, parameters or dangerous actions."""
    if name == "music_playlist":
        return arguments == {"action": "list"}
    if name == "open_website":
        return arguments == {"site": "youtube"}
    if name == "led_control" and set(arguments) == {"command"}:
        from backend.led import parse

        return isinstance(arguments["command"], str) and parse(arguments["command"]) is not None
    if name == "music_command" and set(arguments) == {"command"}:
        from backend.music import parse

        intent = parse(arguments["command"]) if isinstance(arguments["command"], str) else None
        return bool(
            intent and intent.action == "play" and intent.kind == "playlist" and intent.query
        )
    return False


def action_proposal(text, tools):
    proposal = playlist_proposal(text, tools)
    from backend.music import FAVOURITE_NAME

    if (
        proposal is None
        and "music_command" in tools
        and re.search(FAVOURITE_NAME, text, re.I)
        and re.search(r"(?:могу|хочешь|давай) (?:просто )?включить", text, re.I)
        and not re.search(r"\bили\b(?! выбрать что-то другое)", text, re.I)
    ):
        proposal = {
            "tool": "music_command",
            "arguments": {"command": "Включи плейлист Favourite Songs"},
            "expires": time.time() + 600,
        }
    if proposal is None and not re.search(r"\b(?:или|либо)\b", text, re.I):
        # A direct question is also a proposal. Parsing supplies the device/action;
        # a quoted instruction, alternative or destructive action cannot grant it.
        question = re.fullmatch(
            r"(?:Хочешь[, ]+(?:я )?)?"
            r"((?:Сделать|Включить|Выключить|Поставить|Установить) [^.!?]+)\?",
            text.strip(),
            re.I,
        )
        match = re.search(
            r"(?:^|[.!?]\s+)(?:я )?могу (?:просто )?([^.!?]+)(?:[.!?]|$)",
            " ".join(text.split()),
            re.I,
        )
        if question or match:
            from backend.local_commands import route

            command = command_text((question or match)[1])
            routed = route(command)
            if routed and routed[0] in tools and safe_followup(*routed):
                proposal = {"tool": routed[0], "arguments": routed[1], "expires": time.time() + 600}
    if proposal:
        if proposal["tool"] == "led_control":
            proposal["expires"] = time.time() + 120
        proposal.update(
            {
                "id": uuid4().hex,
                "state": "proposed",
                "source": "assistant",
                "offer": redact(text),
                "created": time.time(),
            }
        )
    return proposal


def playlist_reply(result):
    if result.get("error") or result.get("ok") is False:
        return str(result.get("error") or "Список плейлистов недоступен").rstrip(".!? ") + (
            ". Могу попробовать ещё раз посмотреть доступные плейлисты."
        )
    rows = result.get("playlists")
    if not isinstance(rows, list):
        return "Инструмент не вернул список плейлистов. Результат не подтверждён."
    titles = [str(row["title"]) for row in rows if isinstance(row, dict) and row.get("title")]
    if not titles:
        return "В доступной части библиотеки плейлисты не найдены."
    suffix = " Список может быть неполным." if not result.get("complete", True) else ""
    return "Доступные плейлисты: " + ", ".join(titles) + "." + suffix


def estimated_tokens(value):
    """Conservative UTF-8 estimate, without JSON's artificial \\uXXXX expansion.

    This is a budget heuristic, not a replacement for a model's tokenizer.
    """
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=lambda _: "[image]")
    return (len(text.encode("utf-8")) + 3) // 4


def older_dialogue_excerpt(history, query, max_chars=1200):
    """Extractive summary, never an invented fact or an additional model request.

    Tool payloads and instructions are not promoted to a system message. Keep a
    few original question/answer fragments; the full originals remain in SQLite.
    """
    words = set(re.findall(r"[а-яёa-z]{4,}", query.casefold()))
    turns, current = [], []
    for row in history:
        if row["role"] == "user":
            if current:
                turns.append(current)
            current = []
        if row["role"] in {"user", "assistant"} and row.get("content"):
            current.append(row)
    if current:
        turns.append(current)
    ranked = sorted(
        enumerate(turns),
        key=lambda item: (
            len(
                words
                & set(
                    re.findall(
                        r"[а-яёa-z]{4,}", " ".join(row["content"] for row in item[1]).casefold()
                    )
                )
            ),
            item[0],
        ),
        reverse=True,
    )[:2]
    lines = []
    for _, turn in sorted(ranked):
        for row in turn:
            label = "Пользователь" if row["role"] == "user" else "Ассистент"
            content = " ".join(row["content"].split())
            fragment = content[:240]
            if len(content) > 240:
                fragment = fragment.rsplit(" ", 1)[0] + "…"
            lines.append(f"{label}: {fragment}")
    return "\n".join(lines)[:max_chars]


def fit_local_context(messages, tools, num_ctx):
    """Keep system/current tool round intact; trim only older complete turns."""
    if not messages:
        return messages
    last_user = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=-1)
    if last_user < 1:
        return messages
    prefix = messages[:1] if messages[0]["role"] == "system" else []
    current_turn = messages[last_user:]
    image_reserve = 1024 * sum(len(m.get("images", [])) for m in current_turn)
    available = max(
        0,
        num_ctx
        - estimated_tokens([*prefix, *current_turn])
        - estimated_tokens(tools or [])
        - 2048
        - image_reserve,
    )
    history = bounded_history(
        messages[len(prefix) : last_user],
        num_ctx * 4,
        '{"historical":true,"result_omitted":true}',
        token_budget=available,
    )
    return [*prefix, *history, *current_turn]


def bounded_history(history, budget, stale_result, result_limit=300, *, token_budget=None):
    """Compact only the outbound copy; SQLite retains complete redacted tool results."""
    history = [dict(message) for message in history]
    for message in history:
        if message["role"] == "tool" and len(message["content"]) > result_limit:
            try:
                result = json.loads(message["content"])
            except (ValueError, TypeError):
                result = {}
            if isinstance(result, dict) and isinstance(result.get("playlists"), list):
                message["content"] = json.dumps(
                    {
                        "playlists": [
                            {key: row[key] for key in ("id", "title") if key in row}
                            for row in result["playlists"][:20]
                            if isinstance(row, dict)
                        ],
                        "complete": False,
                        "historical": True,
                    },
                    ensure_ascii=False,
                )
            else:
                message["content"] = stale_result

    def size():
        return len(json.dumps(history, ensure_ascii=False))

    while history and (
        size() > budget or (token_budget is not None and estimated_tokens(history) > token_budget)
    ):
        history.pop(0)
        while history and history[0]["role"] != "user":
            history.pop(0)
    return history
