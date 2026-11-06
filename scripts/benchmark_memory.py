"""Offline synthetic memory benchmark: 10,000 facts, 50,000 messages, fake model."""

import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

from backend.brain.conversation import Conversation
from backend.brain.llm import ChatChunk
from backend.config import Settings
from backend.memory.library import search
from backend.memory.profile import ProfileMemory
from backend.memory.store import MemoryStore, _now
from backend.tools import build_registry


class Model:
    cloud_context = False

    async def chat_stream(self, messages, tools=None):
        yield ChatChunk(content="Понял.", done=True)


def measure(fn, n=100):
    values = []
    for _ in range(n):
        start = time.perf_counter()
        fn()
        values.append((time.perf_counter() - start) * 1000)
    return {
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(sorted(values)[int(n * 0.95) - 1], 3),
    }


async def turns(conv):
    first = []
    total = []
    for _ in range(30):
        start = time.perf_counter()
        seen = False
        async for piece in conv.reply("Расскажи про проект Орион"):
            if piece and not seen:
                first.append((time.perf_counter() - start) * 1000)
                seen = True
        total.append((time.perf_counter() - start) * 1000)
    return {
        k: {"p50_ms": round(statistics.median(v), 3), "p95_ms": round(sorted(v)[27], 3)}
        for k, v in [("first_reply", first), ("total_reply", total)]
    }


def main():
    with tempfile.TemporaryDirectory() as folder:
        store = MemoryStore(Path(folder) / "memory.sqlite")
        cid = store.new_conversation()
        with store.memory_transaction():
            store.db.executemany(
                "INSERT INTO facts(content,created_at,updated_at,category) VALUES(?,?,?,?)",
                [
                    (
                        f"Проект {'Орион' if i % 100 == 0 else 'Сириус'} задача {i}",
                        _now(),
                        _now(),
                        "project",
                    )
                    for i in range(10000)
                ],
            )
            store.db.executemany(
                "INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)",
                [(cid, "user", f"Обсуждали алгоритм {i}", _now()) for i in range(50000)],
            )
        memory = ProfileMemory(store)
        result = {
            "facts": 10000,
            "messages": 50000,
            "search": measure(lambda: search(store, "Орион", context=True)),
            "context": measure(lambda: memory.relevant("Орион")),
            "retrieved": len(memory.relevant("Орион")),
        }
        settings = Settings(language="ru-RU", tools={"enabled": ["memory"]})
        conv = Conversation(settings, Model(), store, tools=build_registry(settings, store=store))
        result.update(asyncio.run(turns(conv)))
        # Small deterministic relevance set; this measures lexical fixtures, not user usefulness.
        quality = MemoryStore(":memory:")
        positive = quality.save_memory("Я занимаюсь алгоритмами и люблю тёплый свет")[0]
        for content in ("Люблю чай", "Работаю над сайтом", "Мой телефон iPhone"):
            quality.save_memory(content)
        cases = [
            ("алгоритмы", {positive}),
            ("алгоритмов", {positive}),
            ("тёплый", {positive}),
            ("теплый", {positive}),
            ("алгортмы", {positive}),
            ("космический вакуум", set()),
        ]
        true_positive = false_positive = false_negative = 0
        for query, expected in cases:
            found = {r["id"] for r in ProfileMemory(quality).relevant(query)}
            true_positive += len(found & expected)
            false_positive += len(found - expected)
            false_negative += len(expected - found)
        result["quality_fixture"] = {
            "queries": len(cases),
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "precision": true_positive / max(1, true_positive + false_positive),
            "recall": true_positive / max(1, true_positive + false_negative),
        }
        quality.close()
        store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
