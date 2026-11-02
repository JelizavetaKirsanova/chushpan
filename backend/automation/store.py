"""Local scenario documents, phrase uniqueness, training audit and crash-safe run journal."""

import json
from uuid import uuid4

from backend.automation.models import Scenario, normalize_phrase
from backend.memory.store import _now

SCHEMA = """
CREATE TABLE IF NOT EXISTS scenarios (
 id TEXT PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scenario_phrases (
 normalized TEXT PRIMARY KEY, scenario_id TEXT NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS scenario_training (
 id INTEGER PRIMARY KEY, scenario_id TEXT NOT NULL, phrase TEXT NOT NULL,
 operation TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scenario_runs (
 id TEXT PRIMARY KEY, scenario_id TEXT NOT NULL, revision INTEGER NOT NULL,
 event_id TEXT UNIQUE NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scenario_runs ON scenario_runs(scenario_id, started_at);
CREATE TABLE IF NOT EXISTS scenario_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class ScenarioStore:
    def __init__(self, memory):
        self.memory, self.db = memory, memory.db
        self.db.executescript(SCHEMA)
        self.generation = 0
        self.on_run_change = None

    def list(self):
        return [
            Scenario.model_validate_json(r[0])
            for r in self.db.execute("SELECT data FROM scenarios ORDER BY updated_at, id")
        ]

    def get(self, identifier):
        row = self.db.execute("SELECT data FROM scenarios WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ValueError("Сценарий удалён или не найден.")
        return Scenario.model_validate_json(row[0])

    def save(self, scenario):
        self.memory._require_memory()
        scenario = Scenario.model_validate(scenario.model_dump())
        identifier = scenario.id or uuid4().hex
        with self.memory.memory_transaction():
            row = self.db.execute(
                "SELECT revision FROM scenarios WHERE id=?", (identifier,)
            ).fetchone()
            if (row and row[0] != scenario.revision) or (not row and scenario.revision):
                raise ValueError("Сценарий изменён в другом окне. Обнови его перед сохранением.")
            for phrase in scenario.phrases:
                found = self.db.execute(
                    "SELECT scenario_id FROM scenario_phrases WHERE normalized=?",
                    (normalize_phrase(phrase),),
                ).fetchone()
                if found and found[0] != identifier:
                    raise ValueError(
                        f"Фраза «{phrase}» уже принадлежит сценарию «{self.get(found[0]).name}»."
                    )
            saved = scenario.model_copy(
                update={"id": identifier, "revision": scenario.revision + 1}
            )
            self.db.execute(
                "INSERT INTO scenarios VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "revision=excluded.revision,data=excluded.data,updated_at=excluded.updated_at",
                (identifier, saved.revision, saved.model_dump_json(), _now()),
            )
            self.db.execute("DELETE FROM scenario_phrases WHERE scenario_id=?", (identifier,))
            self.db.executemany(
                "INSERT INTO scenario_phrases VALUES (?,?)",
                [(normalize_phrase(p), identifier) for p in saved.phrases],
            )
        self.generation += 1
        return saved

    def delete(self, identifier, revision):
        self.memory._require_memory()
        with self.memory.memory_transaction():
            if self.get(identifier).revision != revision:
                raise ValueError("Сценарий изменился. Обнови список.")
            self.db.execute("DELETE FROM scenarios WHERE id=?", (identifier,))
        self.generation += 1

    def teach(self, identifier, phrase, *, old=None):
        scenario = self.get(identifier)
        phrases = list(scenario.phrases)
        if old is not None:
            if old not in phrases:
                raise ValueError("Исходная фраза уже удалена.")
            phrases.remove(old)
        if phrase is not None:
            phrases.append(phrase)
        with self.memory.memory_transaction():
            saved = self.save(scenario.model_copy(update={"phrases": phrases}))
            self.db.execute(
                "INSERT INTO scenario_training VALUES (NULL,?,?,?,?)",
                (
                    identifier,
                    phrase or old,
                    "delete" if phrase is None else "edit" if old else "add",
                    _now(),
                ),
            )
        return saved

    def training(self):
        return [
            dict(r)
            for r in self.db.execute("SELECT * FROM scenario_training ORDER BY id DESC LIMIT 200")
        ]

    def begin_run(self, scenario, event_id):
        self.memory._require_memory()
        existing = self.db.execute(
            "SELECT data FROM scenario_runs WHERE event_id=?", (event_id,)
        ).fetchone()
        if existing:
            return json.loads(existing[0]), False
        run = {
            "id": uuid4().hex,
            "scenario_id": scenario.id,
            "revision": scenario.revision,
            "name": scenario.name,
            "status": "running",
            "steps": [],
            "started_at": _now(),
        }
        with self.memory.memory_transaction():
            self.db.execute(
                "INSERT INTO scenario_runs VALUES (?,?,?,?,?,?,NULL,?)",
                (
                    run["id"],
                    scenario.id,
                    scenario.revision,
                    event_id,
                    "running",
                    _now(),
                    json.dumps(run),
                ),
            )
        return run, True

    def update_run(self, run):
        with self.memory.memory_transaction():
            self.db.execute(
                "UPDATE scenario_runs SET status=?,finished_at=?,data=? WHERE id=?",
                (
                    run["status"],
                    None if run["status"] == "running" else _now(),
                    json.dumps(run, ensure_ascii=False),
                    run["id"],
                ),
            )
        if self.on_run_change:
            self.on_run_change()

    def history(self, identifier=None):
        rows = self.db.execute(
            "SELECT data FROM scenario_runs "
            + ("WHERE scenario_id=? " if identifier else "")
            + "ORDER BY started_at DESC, rowid DESC LIMIT 100",
            (identifier,) if identifier else (),
        )
        return [json.loads(r[0]) for r in rows]
