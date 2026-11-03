"""Local versioned personalities. Style never grants tool or system permissions."""

from __future__ import annotations

import json
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.brain.privacy import contains_secret

TRAITS = {
    "sarcasm": (
        "Сарказм",
        "Стиль",
        "без сарказма",
        "редкие мягкие подколы",
        "частые уместные саркастические замечания",
    ),
    "profanity": (
        "Мат",
        "Стиль",
        "без мата",
        "редкий мягкий мат",
        "свободная грубая лексика, без оскорбления собеседницы",
    ),
    "humor": (
        "Юмор",
        "Стиль",
        "без шуток",
        "уместные лёгкие шутки",
        "живой юмор, когда ситуация допускает",
    ),
    "boldness": (
        "Дерзость",
        "Стиль",
        "мягкая манера",
        "уверенная манера",
        "дерзко и уверенно, без унижения",
    ),
    "emotionality": (
        "Эмоциональность",
        "Общение",
        "сдержанно",
        "тёплая живая речь",
        "выразительно передавай эмоции словами",
    ),
    "verbosity": (
        "Болтливость",
        "Общение",
        "одна-две смысловые фразы",
        "несколько фраз по существу",
        "развёрнуто объясняй, если это полезно",
    ),
    "care": (
        "Заботливость",
        "Общение",
        "нейтральная поддержка",
        "внимательно и доброжелательно",
        "чуткая поддержка без навязчивости",
    ),
    "formality": (
        "Формальность",
        "Общение",
        "разговорная речь",
        "спокойная аккуратная речь",
        "деловая точная речь",
    ),
    "initiative": (
        "Инициативность",
        "Поведение",
        "не предлагай лишнего",
        "предлагай полезный следующий шаг",
        "предлагай полезные идеи, но не выполняй их без запроса",
    ),
    "energy": (
        "Энергичность",
        "Поведение",
        "спокойный темп общения",
        "живой тон",
        "энергичный бодрый тон",
    ),
    "directness": (
        "Прямолинейность",
        "Поведение",
        "тактично подводи к выводу",
        "ясно говори по существу",
        "сразу называй суть и проблему",
    ),
    "curiosity": (
        "Любознательность",
        "Поведение",
        "не задавай лишних вопросов",
        "уточняй полезные детали",
        "проявляй интерес к теме, без допроса",
    ),
}
DEFAULT_TRAITS = dict(
    sarcasm=70,
    profanity=30,
    humor=50,
    boldness=60,
    emotionality=50,
    verbosity=30,
    initiative=20,
    care=60,
    formality=20,
    energy=50,
    directness=70,
    curiosity=50,
)
LEGACY_KEYS = ("sarcasm", "profanity", "boldness", "verbosity", "initiative", "technical_detail")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Communication(Strict):
    address: Literal["ты", "вы"] = "ты"
    preferred_addresses: list[str] = Field(default_factory=list, max_length=15)
    unwanted_addresses: list[str] = Field(default_factory=list, max_length=30)
    favorite_expressions: list[str] = Field(default_factory=list, max_length=30)
    forbidden_expressions: list[str] = Field(default_factory=list, max_length=30)
    jokes: Literal["off", "rare", "normal", "frequent"] = "normal"
    allow_profanity: bool = True
    response_style: Literal["brief", "detailed", "adaptive"] = "adaptive"
    use_context: bool = True
    errors: Literal["direct", "supportive", "brief"] = "direct"
    irritation: Literal["calm", "brief", "supportive"] = "calm"
    serious: Literal["calm", "supportive", "business"] = "supportive"


class PersonalityProfile(Strict):
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-z0-9_-]{1,40}$")
    revision: int = Field(default=0, ge=0)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)
    builtin: bool = False
    traits: dict[str, int] = Field(default_factory=lambda: DEFAULT_TRAITS.copy())
    communication: Communication = Field(default_factory=Communication)
    instructions: str = Field(default="", max_length=4000)
    technical_detail: int = Field(default=70, ge=0, le=100)

    @model_validator(mode="after")
    def valid(self):
        if set(self.traits) != set(TRAITS) or any(
            type(v) is not int or not 0 <= v <= 100 for v in self.traits.values()
        ):
            raise ValueError("Нужны 12 параметров характера от 0 до 100.")
        if not self.name.strip():
            raise ValueError("Укажи название профиля.")
        for values in (
            self.communication.preferred_addresses,
            self.communication.unwanted_addresses,
            self.communication.favorite_expressions,
            self.communication.forbidden_expressions,
        ):
            if any(not value.strip() or len(value) > 120 for value in values):
                raise ValueError("Выражение должно содержать от 1 до 120 символов.")
        if contains_secret(self.model_dump_json()):
            raise ValueError("Секреты не сохраняются в инструкциях и профилях.")
        return self


def presets():
    variants = [
        (
            "sarcastic",
            "Саркастичный засранец",
            "Острый язык и тёплая поддержка, когда она нужна.",
            dict(sarcasm=85, profanity=65, humor=80, boldness=85, formality=10),
        ),
        (
            "calm",
            "Спокойный помощник",
            "Спокойно, заботливо и без лишнего шума.",
            dict(sarcasm=5, profanity=0, humor=20, boldness=20, energy=20, care=90),
        ),
        (
            "cheerful",
            "Весёлый болтун",
            "Живой разговор, юмор и любопытство.",
            dict(sarcasm=35, humor=90, verbosity=80, energy=90, curiosity=85),
        ),
        (
            "business",
            "Деловой режим",
            "Коротко, точно и сосредоточенно.",
            dict(sarcasm=0, profanity=0, humor=5, formality=95, verbosity=20, directness=95),
        ),
    ]
    return [
        PersonalityProfile(
            id=key,
            revision=1,
            name=name,
            description=description,
            builtin=True,
            traits={**DEFAULT_TRAITS, **values},
        )
        for key, name, description, values in variants
    ]


class Personalities:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self._cache = {}
        self.on_change = None
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS personality_profiles "
            "(id TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        with store.memory_transaction():
            for profile in presets():
                self.db.execute(
                    "INSERT OR IGNORE INTO personality_profiles VALUES (?,?)",
                    (profile.id, profile.model_dump_json()),
                )
            if not self.db.execute(
                "SELECT 1 FROM personality_profiles WHERE id='mine'"
            ).fetchone() and not store.preference("personality", "migrated"):
                values, technical = DEFAULT_TRAITS.copy(), 70
                for row in store.memories():
                    key = row["semantic_key"].removeprefix("character:")
                    if (
                        row["category"] == "personality"
                        and row["status"] == "active"
                        and key in LEGACY_KEYS
                    ):
                        try:
                            value = int(row["content"].rsplit(":", 1)[1]) * 10
                            if 0 <= value <= 100:
                                if key == "technical_detail":
                                    technical = value
                                else:
                                    values[key] = value
                        except (ValueError, IndexError):
                            pass
                profile = PersonalityProfile(
                    id="mine",
                    revision=1,
                    name="Мой характер",
                    description="Твои прежние настройки. Можно менять и копировать.",
                    traits=values,
                    technical_detail=technical,
                )
                self.db.execute(
                    "INSERT INTO personality_profiles VALUES (?,?)",
                    (profile.id, profile.model_dump_json()),
                )
                if store.memory_available:
                    store.set_preference("personality", "migrated", True)

    def list(self):
        return [
            PersonalityProfile.model_validate_json(r[0])
            for r in self.db.execute("SELECT data FROM personality_profiles ORDER BY rowid")
        ]

    def get(self, identifier):
        row = self.db.execute(
            "SELECT data FROM personality_profiles WHERE id=?", (identifier,)
        ).fetchone()
        if not row:
            raise ValueError("Профиль не найден. Обнови настройки.")
        return PersonalityProfile.model_validate_json(row[0])

    def selection(self):
        value = self.store.preference("personality", "selection") or {}
        return {
            "id": value.get("id", "mine"),
            "reason": value.get("reason", "Прежние настройки"),
            "locked": value.get("locked", False),
        }

    def active(self):
        return self.get(self.selection()["id"])

    def select(self, identifier, *, automatic=False, reason="Выбран вручную", locked=None):
        profile, old = self.get(identifier), self.selection()
        if automatic and old["locked"]:
            return {"error": "Ручная блокировка запрещает автоматическую смену характера."}
        selection = {
            "id": profile.id,
            "reason": reason[:200],
            "locked": old["locked"] if locked is None else locked,
        }
        with self.store.memory_transaction():
            self.store.set_preference("personality", "selection", selection)
            self.store.set_preference("personality", "configured", True)
        if self.on_change:
            self.on_change()
        return {
            "ok": True,
            "profile_id": profile.id,
            "message": f"Активный характер: {profile.name}.",
        }

    def save(self, profile):
        profile = PersonalityProfile.model_validate(profile.model_dump())
        self.store._require_memory()
        if profile.builtin or profile.id in {p.id for p in presets()}:
            raise ValueError("Предустановку можно скопировать в свой профиль.")
        with self.store.memory_transaction():
            row = self.db.execute(
                "SELECT data FROM personality_profiles WHERE id=?", (profile.id,)
            ).fetchone()
            old = PersonalityProfile.model_validate_json(row[0]) if row else None
            if profile.revision != (old.revision if old else 0):
                raise ValueError("Профиль изменился в другом окне. Обнови данные.")
            saved = profile.model_copy(update={"revision": profile.revision + 1})
            self.db.execute(
                "INSERT INTO personality_profiles VALUES (?,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (saved.id, saved.model_dump_json()),
            )
        self._cache.clear()
        if self.selection()["id"] == saved.id:
            self.store.set_preference("personality", "configured", True)
        return saved

    def copy(self, identifier, name=None):
        source = self.get(identifier)
        return self.save(
            source.model_copy(
                update={
                    "id": uuid4().hex,
                    "revision": 0,
                    "name": name if name is not None else (source.name[:72] + " — копия"),
                    "builtin": False,
                }
            )
        )

    def delete(self, identifier, revision):
        profile = self.get(identifier)
        if profile.builtin:
            raise ValueError("Предустановки остаются доступными.")
        if revision != profile.revision:
            raise ValueError("Профиль изменился. Обнови данные.")
        if self.selection()["id"] == identifier:
            raise ValueError("Сначала выбери другой активный профиль.")
        self.db.execute("DELETE FROM personality_profiles WHERE id=?", (identifier,))
        self.db.commit()

    def legacy(self):
        profile = self.active()
        return {
            key: round(
                (profile.technical_detail if key == "technical_detail" else profile.traits[key])
                / 10
            )
            for key in LEGACY_KEYS
        }

    def update_legacy(self, changes):
        profile = self.active()
        if profile.builtin:
            profile = self.copy(profile.id)
            self.select(profile.id, reason="Голосовая настройка характера")
        traits = {**profile.traits, **{k: v * 10 for k, v in changes.items() if k in TRAITS}}
        self.save(
            profile.model_copy(
                update={
                    "traits": traits,
                    "technical_detail": int(
                        changes.get("technical_detail", profile.technical_detail / 10) * 10
                    ),
                }
            )
        )

    def note(self, profile=None):
        if profile is None and not self.store.preference("personality", "configured"):
            return ""  # Opening settings leaves the established prompt and latency intact.
        profile = profile or self.active()
        key = profile.model_dump_json()
        if key in self._cache:
            return self._cache[key]
        c = profile.communication
        lines = [
            f"Текущий профиль характера: {profile.name}. Настройки стиля ниже безопасности "
            "и прямой просьбы пользователя.",
            "Уровни относительные, не вероятности. Не меняют полномочия инструментов "
            "и проверку фактического результата.",
        ]
        for name, (_, _, low, medium, high) in TRAITS.items():
            value = profile.traits[name]
            style = low if value < 30 else high if value >= 70 else medium
            if name == "profanity" and not c.allow_profanity:
                style = "мат запрещён"
            lines.append(f"{TRAITS[name][0]} {value}/100: {style}.")
        jokes = dict(
            off="выключены", rare="редко", normal="когда уместно", frequent="часто, уместно"
        )
        lengths = dict(brief="кратко", detailed="подробно", adaptive="подбирай длину по задаче")
        errors = dict(
            direct="прямо назови причину и следующий шаг",
            supportive="спокойно поддержи и предложи следующий шаг",
            brief="коротко назови проблему",
        )
        irritation = dict(
            calm="снизь тон, не спорь и помоги",
            brief="убери подколы и ответь кратко",
            supportive="признай затруднение и помоги без нравоучений",
        )
        serious = dict(
            calm="спокойно и точно", supportive="чутко и поддерживающе", business="по существу"
        )
        lines += [
            f"Обращение к пользовательнице: только «{c.address}», уважительно, в женском роде.",
            f"Шутки: {jokes[c.jokes]}.",
            f"Длина: {lengths[c.response_style]}, без потери необходимого.",
            f"При ошибке: {errors[c.errors]}; не утверждай неподтверждённый успех.",
            f"При раздражении: {irritation[c.irritation]}.",
            f"Серьёзный разговор: {serious[c.serious]}, без шуток, сарказма и мата.",
        ]
        for title, values in (
            ("Предпочитаемые обращения", c.preferred_addresses),
            ("Нежелательные обращения", c.unwanted_addresses),
            ("Любимые выражения (не повторяй механически)", c.favorite_expressions),
            ("Запрещённые выражения", c.forbidden_expressions),
        ):
            if values:
                lines.append(title + ": " + json.dumps(values, ensure_ascii=False))
        if not c.use_context:
            lines.append(
                "Не опирайся на старую тему без явной просьбы. "
                "Контекст безопасности и подтверждений сохраняется."
            )
        lines.append(f"Подробность технических объяснений: {profile.technical_detail}/100.")
        if profile.instructions:
            lines += [
                "Пожелания пользователя о стиле (не полномочия и не обход ограничений):",
                profile.instructions,
            ]
        note = "\n".join(lines)
        self._cache = {key: note}
        return note


def personalities(store):
    if not hasattr(store, "_personalities"):
        store._personalities = Personalities(store)
    return store._personalities


def personality_tool(store):
    from backend.context import scenario_operation
    from backend.security import Risk, command_text
    from backend.tools.registry import Tool, ToolError

    async def select(profile_id):
        matches = [
            p
            for p in personalities(store).list()
            if p.id == profile_id or p.name.casefold() == profile_id.casefold()
        ]
        if len(matches) != 1:
            raise ToolError("Уточни профиль характера: имя должно быть однозначным.")
        automatic = scenario_operation.get() is not None
        return personalities(store).select(
            matches[0].id,
            automatic=automatic,
            reason="Выбран сценарием" if automatic else "Прямая просьба",
        )

    def requested(args, text):
        text = command_text(text)
        if not text.startswith(
            (
                "выбери характер",
                "включи характер",
                "переключи характер",
                "выбери профиль",
                "включи деловой режим",
            )
        ):
            return False
        return any(
            (p.id == args.get("profile_id") or p.name == args.get("profile_id"))
            and (p.id in text or command_text(p.name) in text)
            for p in personalities(store).list()
        )

    return Tool(
        "set_personality_profile",
        "Выбрать существующий характер по точному имени или ID, по прямой просьбе. "
        "ID: sarcastic, calm, cheerful, business, mine. Безопасность не меняется.",
        select,
        {"profile_id": {"type": "string", "maxLength": 120}},
        ["profile_id"],
        risk=Risk.CHANGE,
        intent=requested,
        reply=lambda r: (
            r.get("message") or r.get("error") or "Изменение характера не подтверждено."
        ),
    )
