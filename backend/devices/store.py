"""Device records in the same private SQLite store as the assistant."""

from backend.devices.models import Device


class DeviceStore:
    def __init__(self, memory):
        self.memory, self.db = memory, memory.db
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS device_registry (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )

    def list(self):
        return [
            Device.model_validate_json(r[0])
            for r in self.db.execute("SELECT data FROM device_registry ORDER BY rowid")
        ]

    def get(self, identifier):
        row = self.db.execute(
            "SELECT data FROM device_registry WHERE id=?", (identifier,)
        ).fetchone()
        if not row:
            raise ValueError("Устройство удалено или не найдено.")
        return Device.model_validate_json(row[0])

    def save(self, device):
        self.memory._require_memory()
        with self.memory.memory_transaction():
            row = self.db.execute(
                "SELECT data FROM device_registry WHERE id=?", (device.id,)
            ).fetchone()
            revision = Device.model_validate_json(row[0]).revision if row else 0
            if revision != device.revision:
                raise ValueError("Устройство изменилось в другом окне. Обнови список.")
            if any(
                d.id != device.id and (d.kind, d.endpoint) == (device.kind, device.endpoint)
                for d in self.list()
            ):
                raise ValueError("Это устройство уже добавлено.")
            saved = device.model_copy(update={"revision": revision + 1})
            self.db.execute(
                "INSERT INTO device_registry VALUES (?,?) ON CONFLICT(id) "
                "DO UPDATE SET data=excluded.data",
                (saved.id, saved.model_dump_json()),
            )
        return saved

    def delete(self, identifier, revision):
        self.memory._require_memory()
        with self.memory.memory_transaction():
            if self.get(identifier).revision != revision:
                raise ValueError("Устройство изменилось в другом окне. Обнови список.")
            self.db.execute("DELETE FROM device_registry WHERE id=?", (identifier,))

    def observe(self, device):
        """Telemetry must not invalidate a user's pending metadata edit."""
        self.memory._require_memory()
        with self.memory.memory_transaction():
            current = self.get(device.id)
            for key in ("state", "sent", "last_contact", "state_at", "result_status"):
                setattr(current, key, getattr(device, key))
            self.db.execute(
                "UPDATE device_registry SET data=? WHERE id=?",
                (current.model_dump_json(), current.id),
            )
        return current
