"""`python -m backend memory`: see and delete what Jarvis remembers, without asking it."""

from __future__ import annotations

import sqlite3

from rich.console import Console

from backend.brain.privacy import redact
from backend.config import Settings
from backend.memory.store import MemoryStore

console = Console(highlight=False)


def run_memory(s: Settings, action: str = "list", fact_id: int | None = None) -> int:
    store = None
    try:
        store = MemoryStore(s.db_path)
        if action == "show":
            row = next((r for r in store.memories() if r["id"] == fact_id), None)
            if row is None:
                console.print("[red]Воспоминание не найдено.[/]")
                return 1
            console.print(redact(row), markup=False)
            return 0
        if action == "forget":
            if fact_id is None:
                console.print("[red]Укажите номер: `memory forget <id>` (список: `memory`)[/]")
                return 1
            if not store.forget_fact(fact_id):
                console.print(f"[red]Нет факта с номером {fact_id}[/]")
                return 1
            console.print(f"[green]Удалён факт {fact_id}[/]")
            return 0
        if action == "clear":
            n = store.forget_all_facts()
            console.print(f"[green]Удалено фактов: {n}[/]")
            return 0
        facts = store.facts()
        if not facts:
            console.print(
                f"[dim]Память пока пуста. Скажите: «{s.assistant_name}, запомни, что…»[/]"
            )
        for i, text in facts:
            console.print(f"[cyan]{i:>4}[/]  {redact(text)}")
        return 0
    except sqlite3.Error:
        console.print("[red]SQLite недоступна. Память не изменена.[/]")
        return 1
    finally:
        if store is not None:
            store.close()
