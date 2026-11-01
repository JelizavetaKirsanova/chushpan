"""Bounded dialogue context and a single, explicit read-only playlist proposal."""

import json
import re
import time

from backend.dialogue import normalize

SHORT = {
    "да",
    "давай",
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
    return normalize(text) in SHORT


def playlist_proposal(text, tools):
    # A read-only suggestion is data, never a grant to play, assign or launch a Shortcut.
    if "music_playlist" not in tools:
        return None
    if re.search(r"\b(?:или|либо)\b", text, re.I):
        return None
    if re.search(
        r"(?:^|[.!?]\s+)(?:я )?могу (?:посмотреть|показать|попробовать (?:ещ[её] раз )?посмотреть) "
        r"(?:список )?(?:доступные |доступных |твои |твоих |ваши |ваших )?плейлист(?:ы|ов)",
        " ".join(text.split()),
        re.I,
    ):
        return {
            "tool": "music_playlist",
            "arguments": {"action": "list"},
            "expires": time.time() + 600,
        }
    return None


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


def bounded_history(history, budget, stale_result, result_limit=300):
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

    while history and size() > budget:
        history.pop(0)
        while history and history[0]["role"] != "user":
            history.pop(0)
    return history
