"""Lighting over the existing device catalogue, guards, BLE drivers and private SQLite."""

from __future__ import annotations

import asyncio
import colorsys
import json
import math
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from backend.context import RequestContext, request_scope, voice_addressed
from backend.devices.colors import ColorInput, hex_rgb, rgb_hex
from backend.devices.lighting_models import LightAction, LightJob, Preset
from backend.devices.lighting_schedule import next_occurrence
from backend.tools.registry import ToolError


def now():
    return datetime.now(UTC)


class Lighting:
    def __init__(self, devices):
        self.d = devices
        self.memory, self.db = devices.memory, devices.memory.db
        self.worker = None
        self.current = None
        self.cancelled = set()
        with self.memory.memory_transaction():
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS lighting_documents "
                "(kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(kind,id))"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS lighting_events "
                "(id INTEGER PRIMARY KEY, at TEXT NOT NULL, job_id TEXT, data TEXT NOT NULL)"
            )
        # A process crash during a BLE write is ambiguous. Never replay that occurrence.
        for job in self.list("job"):
            if job.status == "running":
                self.log(
                    job, "interrupted", "Приложение завершилось во время команды; повтора нет."
                )
                self.advance(job, now(), "interrupted")

    def list(self, kind):
        model = Preset if kind == "preset" else LightJob
        return [
            model.model_validate_json(r[0])
            for r in self.db.execute(
                "SELECT data FROM lighting_documents WHERE kind=? ORDER BY rowid", (kind,)
            )
        ]

    def get(self, kind, identifier):
        value = next((v for v in self.list(kind) if v.id == identifier), None)
        if value is None:
            raise ValueError("Запись удалена или не существует.")
        return value

    def put(self, kind, item):
        self.memory._require_memory()
        with self.memory.memory_transaction():
            self.db.execute(
                "INSERT INTO lighting_documents VALUES (?,?,?) "
                "ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                (kind, item.id, item.model_dump_json()),
            )

    def log(self, job, status, message, **extra):
        with self.memory.memory_transaction():
            self.db.execute(
                "INSERT INTO lighting_events(at,job_id,data) VALUES (?,?,?)",
                (
                    now().isoformat(),
                    job.id if job else None,
                    json.dumps({"status": status, "message": message, **extra}, ensure_ascii=False),
                ),
            )
            self.db.execute(
                "DELETE FROM lighting_events WHERE id NOT IN "
                "(SELECT id FROM lighting_events ORDER BY id DESC LIMIT 500)"
            )

    def snapshot(self):
        return {
            "presets": [p.model_dump() for p in self.list("preset")],
            "jobs": [j.model_dump() for j in self.list("job")],
            "flags": self.memory.preference("lighting", "conditions") or {},
            "journal": [
                {"at": r[0], "job_id": r[1], **json.loads(r[2])}
                for r in self.db.execute(
                    "SELECT at,job_id,data FROM lighting_events ORDER BY id DESC LIMIT 60"
                )
            ],
            "scheduler": "running" if self.worker and not self.worker.done() else "waiting",
            "notice": "Таймеры выполняет работающий Чушпан на Mac. После сна проверяются "
            "пропуски; при выключенном Mac команды не выполняются. "
            "BLE подтверждает отправку, но не физическое состояние ленты.",
        }

    def targets(self, identifiers):
        if not isinstance(identifiers, list) or len(identifiers) > 100:
            raise ValueError("Нужен список не более 100 лент/групп.")
        groups = {g.id: g for g in self.d.store.collections("group")}
        result = []
        for identifier in identifiers:
            for member in groups[identifier].device_ids if identifier in groups else [identifier]:
                device = self.d.store.get(member)
                if device.kind not in {"led_strip", "wled"}:
                    raise ValueError("Выбери только LED-ленты или группы освещения.")
                if device.id not in result:
                    result.append(device.id)
        if not result:
            raise ValueError("Выбери хотя бы одну ленту.")
        return result

    def resolve(self, action):
        return self.get("preset", action.preset_id).action if action.kind == "preset" else action

    def preflight(self, targets, action, *, running=True):
        self.d.require_adapter()
        if not self.d.tools.enabled("device_command") or not self.d.tools.enabled("led_control"):
            raise ToolError("Управление освещением отключено в настройках доступа.")
        action = self.resolve(action)
        for identifier in targets:
            device = self.d.store.get(identifier)
            if running:
                self.d.check_location(device)
            features = self.d.features(device)
            required = []
            if (
                action.color is not None
                or action.kind == "effect"
                and action.effect in {"cycle", "transition", "rainbow", "twinkle"}
            ):
                required.append("color")
            if (
                action.brightness is not None
                or action.kind == "effect"
                and action.effect in {"pulse", "breathing"}
            ):
                required.append("brightness")
            if action.power is not None:
                required.append("power_on" if action.power else "power_off")
            if any(not features.get(key) for key in required):
                reason = next(
                    (
                        features[f"{key}_reason"]
                        for key in required
                        if not features.get(key) and features.get(f"{key}_reason")
                    ),
                    None,
                )
                if reason:
                    raise ToolError(f"{device.name}: {reason}")
                raise ToolError(
                    f"{device.name}: функция ещё не подтверждена. "
                    "Открой настройки ленты и проверь её отдельно."
                )
            if running and (
                action.kind == "fade"
                or action.kind == "effect"
                and action.effect in {"pulse", "breathing"}
            ):
                initial = {**device.sent, **device.state}
                keys = (
                    ["brightness"]
                    if action.kind == "effect"
                    else [k for k in ("color", "brightness") if getattr(action, k) is not None]
                )
                if any(k not in initial for k in keys):
                    raise ToolError(
                        f"{device.name}: сначала задай исходный цвет/яркость. "
                        "Считывание состояния этой ленты не подтверждено."
                    )
        return action

    def save(self, kind, value):
        model = Preset if kind == "preset" else LightJob
        item = model.model_validate(value)
        existing = self.get(kind, item.id) if item.id else None
        if item.revision != (existing.revision if existing else 0):
            raise ValueError("Запись изменилась в другом окне. Обнови раздел.")
        if len(self.list(kind)) >= 200 and not existing:
            raise ValueError("Сначала удали ненужные записи (лимит 200).")
        if kind == "preset":
            if any(
                p.id != item.id and p.name.casefold() == item.name.casefold()
                for p in self.list(kind)
            ):
                raise ValueError("Такое имя пресета уже используется.")
        else:
            item.targets = self.targets(item.targets)  # Freeze group membership at save time.
            self.d.valid_place(item.place_id)
            if item.enabled:
                self.preflight(item.targets, item.action, running=False)
            item.progress = {}
            item.status = "pending" if item.enabled else "paused"
            if item.enabled and item.rule.kind in {"weekly", "date", "sunrise", "sunset"}:
                upcoming = next_occurrence(item.rule, now())
                if upcoming is None:
                    raise ValueError("Нет ближайшего запуска для выбранной даты/правила.")
                item.next_at = upcoming.isoformat()
            if item.enabled and datetime.fromisoformat(item.next_at) <= now():
                raise ValueError("Время таймера должно быть в будущем.")
            for other in self.list("job"):
                if other.id == item.id or not other.enabled or not item.enabled:
                    continue
                if not set(item.targets) & set(other.targets):
                    continue
                same_deadline = (
                    abs(
                        (
                            datetime.fromisoformat(item.next_at)
                            - datetime.fromisoformat(other.next_at)
                        ).total_seconds()
                    )
                    < 2
                )
                repeating_conflict = (
                    item.rule.kind == other.rule.kind == "weekly"
                    and item.rule.at == other.rule.at
                    and item.rule.timezone == other.rule.timezone
                    and set(item.rule.weekdays) & set(other.rule.weekdays)
                )
                if same_deadline or repeating_conflict:
                    raise ValueError(
                        f"Конфликт с правилом «{other.name}». Измени время или отключи его."
                    )
        item.id = item.id or uuid4().hex
        item.revision += 1
        self.put(kind, item)
        if existing:
            self.cancel_current(existing.id)
        return item

    def cancel_current(self, identifier):
        if self.current and self.current[0] == identifier:
            self.cancelled.add(identifier)
            self.current[1].cancel()

    def delete(self, kind, identifier, revision):
        item = self.get(kind, identifier)
        if revision != item.revision:
            raise ValueError("Запись изменилась. Обнови раздел.")
        if kind == "preset" and any(j.action.preset_id == identifier for j in self.list("job")):
            raise ValueError("Пресет используется таймером. Сначала измени или удали таймер.")
        with self.memory.memory_transaction():
            self.db.execute(
                "DELETE FROM lighting_documents WHERE kind=? AND id=?", (kind, identifier)
            )
        self.cancel_current(identifier)

    def advance(self, job, moment, status):
        anchor = datetime.fromisoformat(job.progress.get("occurrence", job.next_at))
        upcoming = next_occurrence(job.rule, moment, anchor)
        job.enabled = upcoming is not None
        job.next_at = (upcoming or moment).isoformat()
        job.status, job.progress = status, {}
        self.put("job", job)

    async def stop(self, targets):
        stopped = 0
        pending = []
        for job in self.list("job"):
            if (
                job.enabled
                and job.action.kind in {"fade", "effect"}
                and set(targets) & set(job.targets)
            ):
                job.enabled, job.status = False, "cancelled"
                job.revision += 1
                self.put("job", job)
                if self.current and self.current[0] == job.id:
                    pending.append(self.current[1])
                self.cancel_current(job.id)
                stopped += 1
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return {
            "result_status": "success",
            "message": f"Остановлено переходов/эффектов: {stopped}.",
        }

    async def apply(self, targets, action, *, scheduled=False):
        if action.kind == "stop":
            return await self.stop(targets)
        action = self.preflight(targets, action)
        if not scheduled:
            await self.stop(targets)
        if action.kind in {"fade", "effect"}:
            job = self.save(
                "job",
                {
                    "name": "Плавный переход" if action.kind == "fade" else "Программный эффект",
                    "targets": targets,
                    "action": action.model_dump(),
                    "next_at": (now() + timedelta(seconds=1)).isoformat(),
                },
            )
            return {
                "result_status": "success",
                "message": "Задача сохранена; выполняется на Mac.",
                "job_id": job.id,
                "scheduled": True,
            }
        parameters = action.model_dump(include={"color", "brightness", "power"}, exclude_none=True)
        results = []
        # All selected devices were validated before the first BLE command.
        for identifier in targets:
            result = await self.d.device_command(
                identifier, "inspect" if action.kind == "status" else "led", **parameters
            )
            results.append({"id": identifier, **result})
        failed = any(r.get("result_status") in {"failed", "partial_success"} for r in results)
        return {
            "results": results,
            "result_status": "failed"
            if failed
            else "success"
            if all(r.get("result_status") == "success" for r in results)
            else "unverified",
            "written": any(r.get("written") for r in results),
            "message": "\n".join(r.get("message", "Результат не подтверждён.") for r in results),
        }

    def frame(self, job, moment):
        action = self.resolve(job.action)
        elapsed = max(0, (moment - datetime.fromisoformat(job.progress["started"])).total_seconds())
        ratio = min(1, elapsed / action.duration_s)
        frames = []
        for identifier in job.targets:
            initial = job.progress["initial"][identifier]
            params = {}
            if action.kind == "fade":
                if action.color:
                    start, end = hex_rgb(initial["color"]), hex_rgb(action.color)
                    params["color"] = rgb_hex(
                        [round(a + (b - a) * ratio) for a, b in zip(start, end, strict=True)]
                    )
                if action.brightness is not None:
                    start = initial["brightness"]
                    params["brightness"] = round(start + (action.brightness - start) * ratio)
                if ratio == 1 and action.power is not None:
                    params["power"] = action.power
            else:
                phase = (elapsed / action.period_s) % 1
                if action.effect in {"pulse", "breathing"}:
                    wave = (1 - math.cos(phase * 2 * math.pi)) / 2
                    if action.effect == "pulse":
                        wave = max(0, 1 - abs(phase * 2 - 1) * 3)
                    params["brightness"] = round(initial["brightness"] * (0.08 + 0.92 * wave))
                elif action.effect == "rainbow":
                    params["color"] = rgb_hex(
                        [round(c * 255) for c in colorsys.hsv_to_rgb(phase, 1, 1)]
                    )
                elif action.effect == "twinkle":
                    # Slow colour shimmer, never rapid strobe.
                    params["color"] = rgb_hex(
                        [
                            round(c * (0.5 + 0.5 * abs(math.sin(elapsed / 4))))
                            for c in hex_rgb(initial.get("color", "#FFFFFF"))
                        ]
                    )
                else:
                    pos = phase * len(action.colors)
                    index = int(pos) % len(action.colors)
                    start, end = (
                        hex_rgb(action.colors[index]),
                        hex_rgb(action.colors[(index + 1) % len(action.colors)]),
                    )
                    fraction = (
                        pos % 1
                        if action.effect == "transition"
                        else (1 - math.cos((pos % 1) * math.pi)) / 2
                    )
                    params["color"] = rgb_hex(
                        [round(a + (b - a) * fraction) for a, b in zip(start, end, strict=True)]
                    )
            frames.append((identifier, LightAction(**params)))
        return frames, ratio == 1

    async def run_job(self, job, moment):
        place = self.d.locations.current()
        if job.place_id != "general" and place.get("place_id") != job.place_id:
            return "skipped", "Не совпадает текущее местоположение."
        flags = self.memory.preference("lighting", "conditions") or {}
        if any(flags.get(k) != v for k, v in job.conditions.items()):
            return "skipped", "Пользовательские условия не выполнены."
        action = self.preflight(job.targets, job.action)
        if action.kind in {"fade", "effect"}:
            if not job.progress:
                job.progress = {
                    "started": moment.isoformat(),
                    "occurrence": job.next_at,
                    "initial": {
                        i: {**self.d.store.get(i).sent, **self.d.store.get(i).state}
                        for i in job.targets
                    },
                }
            frames, finished = self.frame(job, moment)
            for identifier, frame in frames:
                result = await self.apply([identifier], frame, scheduled=True)
                if result["result_status"] == "failed":
                    return "failed", result["message"]
            if not finished:
                job.next_at = (now() + timedelta(seconds=2)).isoformat()
                job.status = "active"
                self.put("job", job)
                return "active", "Программный переход; физический результат не считывается."
        else:
            result = await self.apply(job.targets, action, scheduled=True)
            return result["result_status"], result["message"]
        return "unverified", "Переход завершён; команды отправлены, состояние не считывается."

    async def tick(self, moment=None):
        moment = moment or now()
        occupied = set()
        active = [j for j in self.list("job") if j.enabled and j.progress]
        for job in sorted(self.list("job"), key=lambda j: (j.next_at, j.id)):
            if not job.enabled or datetime.fromisoformat(job.next_at) > moment:
                continue
            due = datetime.fromisoformat(job.next_at)
            conflict = set(job.targets) & occupied or any(
                other.id != job.id and set(job.targets) & set(other.targets) for other in active
            )
            if conflict:
                status, message = "conflict", "Другая задача уже управляет этой лентой."
            elif (moment - due).total_seconds() > max(3, job.catch_up_s):
                status, message = (
                    "missed",
                    "Пропущено во сне/при выключенном Mac; срок догоняющего запуска истёк.",
                )
            else:
                occupied.update(job.targets)
                with self.memory.memory_transaction():
                    latest = self.get("job", job.id)
                    if (
                        not latest.enabled
                        or latest.status == "running"
                        or latest.revision != job.revision
                        or latest.next_at != job.next_at
                    ):
                        continue
                    job.status = "running"
                    self.put("job", job)  # Claim before any side effect.
                task = asyncio.create_task(self.run_job(job, moment))
                self.current = (job.id, task)
                try:
                    status, message = await task
                except asyncio.CancelledError:
                    if job.id in self.cancelled:
                        self.cancelled.discard(job.id)
                        self.log(
                            job,
                            "cancelled",
                            "Задача изменена/отменена; начатая запись могла выполниться.",
                        )
                        continue
                    raise
                except Exception as exc:
                    status, message = (
                        "failed",
                        str(exc)
                        if isinstance(exc, (ValueError, ToolError))
                        else "Ошибка освещения; автоповтора записи нет.",
                    )
                finally:
                    self.current = None
            if status != "active":
                self.log(job, status, message)
                self.advance(job, moment, status)
            self.d.publish()

    def bind(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        if self.worker is None:
            self.worker = asyncio.create_task(self.loop())

    async def loop(self):
        while True:
            try:
                token = voice_addressed.set(True)
                try:
                    with request_scope(
                        RequestContext(self.d.conv.device, "Управляй выбранным устройством")
                    ):
                        await self.tick()
                finally:
                    voice_addressed.reset(token)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.d.publish(error="Планировщик освещения: ошибка проверки. См. журнал.")
            await asyncio.sleep(2)

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None

    async def handle(self, value):
        self.d.require_adapter()
        if not self.d.tools.enabled("device_command"):
            raise ToolError("Управление устройствами отключено.")
        op = value.get("operation")
        if op == "templates":
            return self.templates(self.targets(value.get("targets", [])))
        if op in {"preset_save", "job_save"}:
            saved = self.save("preset" if op == "preset_save" else "job", value.get("value"))
            self.d.update_catalogue()
            return {"result_status": "success", "message": "Сохранено.", "id": saved.id}
        if op in {"preset_delete", "job_delete"}:
            self.delete(
                "preset" if op == "preset_delete" else "job", value.get("id"), value.get("revision")
            )
            self.d.update_catalogue()
            return {"result_status": "success", "message": "Удалено."}
        if op == "flags":
            flags = value.get("flags")
            if (
                not isinstance(flags, dict)
                or len(flags) > 20
                or any(
                    not isinstance(k, str) or not 1 <= len(k) <= 60 or type(v) is not bool
                    for k, v in flags.items()
                )
            ):
                raise ValueError("Условия: до 20 названий и значений да/нет.")
            self.memory.set_preference("lighting", "conditions", flags)
            return {"result_status": "success", "message": "Условия сохранены."}
        if op == "export":
            return {
                "result_status": "success",
                "message": "Палитра готова к экспорту.",
                "palette": {
                    "version": 1,
                    "colors": [
                        c.model_dump(include={"name", "rgb", "favorite"})
                        for c in self.d.colors.list()
                    ],
                    "presets": [
                        p.model_dump(exclude={"id", "revision"}) for p in self.list("preset")
                    ],
                },
            }
        if op == "import":
            payload = value.get("palette")
            if (
                not isinstance(payload, dict)
                or len(json.dumps(payload)) > 65536
                or set(payload) != {"version", "colors", "presets"}
                or payload["version"] != 1
            ):
                raise ValueError("Нужна палитра версии 1 до 64 КБ.")
            if (
                not isinstance(payload["colors"], list)
                or not isinstance(payload["presets"], list)
                or len(payload["colors"]) + len(payload["presets"]) > 200
            ):
                raise ValueError("В палитре допускается до 200 цветов/пресетов.")
            colors = [ColorInput.model_validate(c) for c in payload["colors"]]
            presets = [Preset.model_validate(p) for p in payload["presets"]]
            if any(p.id or p.revision for p in presets):
                raise ValueError("Импорт не может заменять существующие записи.")
            with self.memory.memory_transaction():
                for color in colors:
                    self.d.colors.save(color.model_dump())
                for preset in presets:
                    self.save("preset", preset.model_dump())
            self.d.update_catalogue()
            return {
                "result_status": "success",
                "message": "Палитра импортирована без команд устройствам.",
            }
        if op == "apply":
            targets = self.targets(value.get("targets", []))
            action = LightAction.model_validate(value.get("action"))
            token = voice_addressed.set(True)
            try:
                with request_scope(
                    RequestContext(self.d.conv.device, "Управляй выбранным устройством")
                ):
                    result = await self.apply(targets, action)
            finally:
                voice_addressed.reset(token)
            self.log(None, result["result_status"], result["message"])
            return result
        raise ValueError("Неизвестная операция освещения.")

    def templates(self, targets):
        from backend.automation.models import Block, Scenario, normalize_phrase

        automations = self.d.tools.automations
        if not automations:
            raise ToolError("Редактор сценариев недоступен.")
        templates = [
            ("Спокойной ночи", "приглуши свет на ночь", "#220800", 10),
            ("Просмотр фильма", "включи режим кино", "#8A28CC", 15),
            ("Учёба", "включи режим учебы", "#FFE0B0", 75),
            ("Пробуждение", "включи пробуждение", "#FFD19A", 80),
        ]
        saved, unavailable = [], []
        with self.memory.memory_transaction():
            for name, phrase, color, brightness in templates:
                if any(s.name == name for s in automations.store.list()):
                    continue
                children = []
                for identifier in targets:
                    device = self.d.store.get(identifier)
                    features = self.d.features(device)
                    self.d.check_location(device)
                    blocks = []
                    if name == "Пробуждение" and features.get("power_on"):
                        blocks.append(Block(capability=f"device.{identifier}.on"))
                    if features.get("color"):
                        blocks.append(
                            Block(
                                capability=f"device.{identifier}.color", parameters={"color": color}
                            )
                        )
                    if features.get("brightness"):
                        blocks.append(
                            Block(
                                capability=f"device.{identifier}.brightness",
                                parameters={"brightness": brightness},
                            )
                        )
                    if name == "Спокойной ночи" and features.get("power_off"):
                        blocks = [Block(capability=f"device.{identifier}.off")]
                    if not blocks:
                        unavailable.append(f"{name}: {device.name}")
                        continue
                    children.append(Block(kind="sequence", children=blocks))
                if children:
                    used = {
                        normalize_phrase(p) for s in automations.store.list() for p in s.phrases
                    }
                    scenario = Scenario(
                        name=name,
                        description="Освещение · зарегистрированные возможности выбранных лент.",
                        phrases=[phrase] if normalize_phrase(phrase) not in used else [],
                        actions=[Block(kind="parallel", children=children)],
                    )
                    automations.validate_document(scenario)
                    saved.append(automations.store.save(scenario).name)
        automations.publish()
        return {
            "result_status": "success",
            "message": "Созданы сценарии: "
            + (", ".join(saved) or "уже существуют")
            + (". Недоступны функции: " + ", ".join(unavailable) if unavailable else ""),
        }
