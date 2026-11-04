"""Device records in the same private SQLite store as the assistant."""

import json

from backend.devices.models import Device, DeviceGroup, Room


class DeviceStore:
    def __init__(self, memory):
        self.memory, self.db = memory, memory.db
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS device_registry (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )

        # Idempotent additive migration; older JSON device records use model defaults.
        with self.memory.memory_transaction():
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS device_schema (version INTEGER PRIMARY KEY)"
            )
            self.db.execute("INSERT OR IGNORE INTO device_schema VALUES (2)")
            for table in ("device_rooms", "device_groups"):
                self.db.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
                )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS device_commands (id INTEGER PRIMARY KEY, "
                "device_id TEXT NOT NULL, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "data TEXT NOT NULL)"
            )

    def collections(self, kind):
        table, model = {"room": ("device_rooms", Room), "group": ("device_groups", DeviceGroup)}[
            kind
        ]
        return [
            model.model_validate_json(r[0])
            for r in self.db.execute(f"SELECT data FROM {table} ORDER BY rowid")
        ]

    def save_collection(self, kind, value, delete=False):
        self.memory._require_memory()
        table, model = {"room": ("device_rooms", Room), "group": ("device_groups", DeviceGroup)}[
            kind
        ]
        item = model.model_validate(value)
        item.name = item.name.strip()
        if not item.name or any(ord(c) < 32 for c in item.name):
            raise ValueError("Укажи название обычным текстом.")
        with self.memory.memory_transaction():
            existing = next((v for v in self.collections(kind) if v.id == item.id), None)
            if item.revision != (existing.revision if existing else 0):
                raise ValueError("Список изменился. Обнови данные.")
            if any(
                v.id != item.id
                and (v.place_id, v.name.casefold()) == (item.place_id, item.name.casefold())
                for v in self.collections(kind)
            ):
                raise ValueError("Такое название уже используется в этом местоположении.")
            if kind == "room" and existing:
                for device in self.list():
                    if (device.place_id, device.room) == (existing.place_id, existing.name):
                        device.place_id = item.place_id if not delete else existing.place_id
                        device.room = item.name if not delete else ""
                        self.save(device)
            if delete:
                self.db.execute(f"DELETE FROM {table} WHERE id=?", (item.id,))
            else:
                if kind == "group":
                    item.device_ids = list(dict.fromkeys(item.device_ids))
                    for identifier in item.device_ids:
                        self.get(identifier)
                item.revision += 1
                self.db.execute(
                    f"INSERT INTO {table} VALUES (?,?) ON CONFLICT(id) "
                    "DO UPDATE SET data=excluded.data",
                    (item.id, item.model_dump_json()),
                )
        return item

    def log(self, identifier, action, parameters, result, elapsed_ms):
        self.memory._require_memory()
        # No endpoints, tokens, credentials or raw protocol responses in the journal.
        data = {
            "action": action,
            "parameters": parameters,
            "result_status": result.get("result_status", "unverified"),
            "elapsed_ms": elapsed_ms,
        }
        with self.memory.memory_transaction():
            self.db.execute(
                "INSERT INTO device_commands(device_id,data) VALUES (?,?)",
                (identifier, json.dumps(data, ensure_ascii=False)),
            )
            self.db.execute(
                "DELETE FROM device_commands WHERE id NOT IN "
                "(SELECT id FROM device_commands ORDER BY id DESC LIMIT 2000)"
            )

    def history(self, identifier):
        return [
            {"at": r[0], **json.loads(r[1])}
            for r in self.db.execute(
                "SELECT at,data FROM device_commands WHERE device_id=? ORDER BY id DESC LIMIT 30",
                (identifier,),
            )
        ]

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
            self.db.execute("DELETE FROM device_commands WHERE device_id=?", (identifier,))
            for group in self.collections("group"):
                if identifier in group.device_ids:
                    group.device_ids.remove(identifier)
                    self.save_collection("group", group.model_dump())

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
