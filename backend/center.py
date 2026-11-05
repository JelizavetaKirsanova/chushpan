"""Bounded asynchronous request boundary for local desktop modules."""

import asyncio
import sqlite3

from backend.tools.registry import ToolError


class Center:
    channel = ""
    timeout = 30

    def bind(self, bus):
        self.bus = bus
        self.task = None
        bus.handle(self.channel, self.request)

    def request(self, value):
        if not isinstance(value, dict):
            return
        if self.task and not self.task.done():
            if value.get("op") == "cancel":
                self.task.cancel()
            else:
                self.publish(error="Дождись текущей операции или останови её.")
            return
        self.task = asyncio.create_task(self.run(value))

    def publish(self, **value):
        self.bus.publish({"type": self.channel, **value})

    async def run(self, value):
        self.publish(busy=True)
        try:
            async with asyncio.timeout(self.timeout):
                result = await self.handle(value)
            self.publish(busy=False, **(result or {}))
        except asyncio.CancelledError:
            self.publish(busy=False, error="Операция остановлена. Результат не подтверждён.")
        except TimeoutError:
            self.publish(busy=False, error="Время ожидания истекло. Результат не подтверждён.")
        except (ValueError, ToolError) as exc:
            self.publish(busy=False, error=str(exc))
        except sqlite3.Error:
            self.publish(busy=False, error="Локальная база недоступна. Изменения не подтверждены.")
        except Exception as exc:
            # No raw native/network error or user content in diagnostics.
            self.publish(busy=False, error="Операция недоступна: " + type(exc).__name__)

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


def identifier(value):
    if type(value) is not int or value < 1:
        raise ValueError("Нужен корректный идентификатор.")
    return value


def text(value, limit=300):
    from backend.brain.privacy import contains_secret

    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Укажи текст от 1 до {limit} символов.")
    if contains_secret(value):
        raise ValueError("Секреты нельзя сохранять в этом разделе.")
    return value.strip()


def rows(db, sql, parameters=()):
    try:
        return [dict(row) for row in db.execute(sql, parameters)]
    except sqlite3.Error:
        raise ValueError("Локальная база данных недоступна.") from None
