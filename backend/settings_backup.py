"""Versioned, secret-free JSON backup. Restore merges; never deletes history."""

import json
import time
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from backend.audio.activity import VoiceControls
from backend.automation.models import Place, Scenario
from backend.brain.privacy import contains_secret
from backend.personality import PersonalityProfile, Strict
from backend.settings_models import CATEGORIES, DialogueExtras


class MemoryEntry(Strict):
    content: str = Field(min_length=1, max_length=300)
    category: str
    sensitive: bool = False


class PlaceSelection(Strict):
    mode: Literal["auto", "manual"] = "auto"
    place_id: str = Field(default="", max_length=40)


class Backup(Strict):
    version: int = 1
    created_at: str = ""
    settings: dict = Field(default_factory=dict)
    profiles: list[PersonalityProfile] = Field(default_factory=list, max_length=100)
    active_profile: str = "mine"
    profile_locked: bool = False
    scenarios: list[Scenario] = Field(default_factory=list, max_length=1000)
    places: list[Place] = Field(default_factory=list, max_length=100)
    memories: list[MemoryEntry] = Field(default_factory=list, max_length=10000)
    voice_controls: VoiceControls | None = None
    dialogue: DialogueExtras | None = None
    ambient: dict | None = None
    music_provider: str | None = None
    place_selection: PlaceSelection | None = None

    @model_validator(mode="after")
    def valid(self):
        from backend.hud.dashboard import dashboard_settings

        if self.ambient is not None:
            self.ambient = dashboard_settings(self.ambient)
        if self.music_provider not in {None, "spotify", "youtube_music", "unselected"}:
            raise ValueError("Неизвестный музыкальный сервис.")
        if self.version != 1 or set(self.settings) - set(CATEGORIES):
            raise ValueError("Версия или категории резервной копии не поддерживаются.")
        for key, value in self.settings.items():
            self.settings[key] = CATEGORIES[key].model_validate(value).model_dump()
        if any(
            m.category not in {"profile", "preference", "habit", "project", "episode", "correction"}
            for m in self.memories
        ):
            raise ValueError("Неизвестная категория памяти.")
        inspected = self.model_dump(mode="json")
        for place in inspected["places"]:
            for signal in place["signals"]:
                signal["key"] = "observed-sha256"
        if contains_secret(json.dumps(inspected, ensure_ascii=False)):
            raise ValueError(
                "Копия содержит секреты. API-ключи не экспортируются и не импортируются."
            )
        if len({p.id for p in self.profiles}) != len(self.profiles) or len(
            {p.id for p in self.places}
        ) != len(self.places):
            raise ValueError("В резервной копии повторяются идентификаторы.")
        return self


async def backup_request(center, value):
    service = center.conv.tools.automations
    if value["op"] == "export":
        scope = value.get("scope", "all")
        if scope not in {"all", "memory"}:
            raise ValueError("Неизвестный состав экспорта.")
        document = Backup(
            created_at=datetime.now(UTC).isoformat(),
            settings={k: center.value(k) for k in CATEGORIES} if scope == "all" else {},
            profiles=center.profiles.list() if scope == "all" else [],
            active_profile=center.profiles.active().id,
            profile_locked=center.profiles.selection()["locked"],
            scenarios=service.store.list() if service and scope == "all" else [],
            places=service.locations.list() if service and scope == "all" else [],
            memories=[
                MemoryEntry(
                    content=m["content"], category=m["category"], sensitive=bool(m["sensitive"])
                )
                for m in center.store.memories()
                if m["status"] == "active" and m["category"] != "personality"
            ],
            voice_controls=(center.store.preference("voice", "controls"))
            if scope == "all"
            else None,
            dialogue=center.store.preference("voice", "dialogue") if scope == "all" else None,
            place_selection=center.store.preference("automation", "location")
            if scope == "all"
            else None,
            ambient=center.store.preference("dashboard", "settings") if scope == "all" else None,
            music_provider=(center.store.preference("media", "provider") or {}).get(
                "provider", "unselected"
            )
            if scope == "all"
            else None,
        )
        center.bus.publish(
            {
                "type": "settings_export",
                "document": document.model_dump(mode="json"),
                "filename": "chushpan-backup-"
                + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
                + ".json",
            }
        )
        return
    if value["op"] == "import_preview":
        raw = value.get("document")
        if not isinstance(raw, dict) or len(json.dumps(raw)) > 2_000_000:
            raise ValueError("Импортируется JSON-копия до 2 МБ.")
        document = Backup.model_validate(raw)
        token = uuid4().hex
        center.pending_import = (token, time.monotonic() + 300, document)
        center.bus.publish(
            {
                "type": "settings_import",
                "token": token,
                "counts": {
                    "profiles": len(document.profiles),
                    "scenarios": len(document.scenarios),
                    "memories": len(document.memories),
                    "categories": len(document.settings),
                },
                "message": (
                    "История остаётся на месте. Профили и сценарии добавляются копиями. "
                    "Сценарии будут отключены до проверки."
                ),
            }
        )
        return
    staged = center.pending_import
    if (
        not staged
        or staged[0] != value.get("token")
        or staged[1] < time.monotonic()
        or value.get("confirm") is not True
    ):
        raise ValueError("Подтверждение импорта истекло. Выбери файл ещё раз.")
    document = staged[2]
    if "studio" in document.settings:
        center.validate_studio(document.settings["studio"])
    # Prepare every object first. A corrupt copy cannot leave half an import.
    profiles = [
        p.model_copy(
            update={
                "id": uuid4().hex,
                "revision": 0,
                "builtin": False,
                "name": p.name[:71] + " (импорт)",
            }
        )
        for p in document.profiles
        if not p.builtin
    ]
    remap = {
        old.id: new.id
        for old, new in zip([p for p in document.profiles if not p.builtin], profiles, strict=True)
    }
    places = []
    if service:
        for place in document.places:
            current = next((p for p in service.locations.list() if p.id == place.id), None)
            if current:
                continue  # Existing explicit local place bindings always win.
            places.append(place.model_copy(update={"revision": 0}))
    scenarios = []
    for scenario in document.scenarios:
        existing = (
            next((s for s in service.store.list() if s.id == scenario.id), None)
            if service
            else None
        )
        if existing and existing.model_dump() == scenario.model_dump():
            continue
        data = scenario.model_dump(mode="json")

        def change(blocks):
            for b in blocks:
                if b["capability"] == "assistant.personality":
                    profile_id = b["parameters"].get("profile_id")
                    b["parameters"]["profile_id"] = remap.get(profile_id, profile_id)
                change(b["children"])
                change(b["otherwise"])

        change(data["actions"])
        for variant in data["variants"]:
            change(variant["actions"])
        data.update(id="", revision=0, enabled=False)
        if service:
            used = {phrase.casefold() for s in service.store.list() for phrase in s.phrases}
            data["phrases"] = [
                phrase for phrase in data["phrases"] if phrase.casefold() not in used
            ]
        scenarios.append(Scenario.model_validate(data))
    with center.store.memory_transaction():
        for profile in profiles:
            center.profiles.save(profile)
        if service:
            for place in places:
                service.locations.save(place)
            for scenario in scenarios:
                service.validate_document(scenario)
                service.store.save(scenario)
        for memory in document.memories:
            center.store.save_memory(
                memory.content,
                category=memory.category,
                source="local_backup_import",
                sensitive=memory.sensitive,
            )
        for category, setting in document.settings.items():
            if category != "access":
                center.store.set_preference("settings", category, setting)
        if document.voice_controls is not None:
            center.store.set_preference("voice", "controls", document.voice_controls.model_dump())
        if document.dialogue is not None:
            center.store.set_preference("voice", "dialogue", document.dialogue.model_dump())
        if service and document.place_selection is not None:
            selected = document.place_selection
            identifiers = {p.id for p in service.locations.list()}
            place = selected.place_id if selected.place_id in identifiers else "general"
            service.locations.select(selected.mode, place if selected.mode == "manual" else "")
        if document.ambient is not None:
            center.store.set_preference("dashboard", "settings", document.ambient)
        if document.music_provider is not None:
            center.store.set_preference(
                "media",
                "provider",
                {
                    "provider": None
                    if document.music_provider == "unselected"
                    else document.music_provider
                },
            )
        active = remap.get(document.active_profile)
        if (
            document.profiles
            and not active
            and document.active_profile in {p.id for p in center.profiles.list() if p.builtin}
        ):
            active = document.active_profile
        if active:
            center.profiles.select(
                active, reason="Восстановлен из локальной копии", locked=document.profile_locked
            )
    center.pending_import = None
    from backend.settings_models import apply_persisted

    apply_persisted(center.s, center.store)
    center.apply_studio(center.value("studio"))
    if document.voice_controls is not None:
        from backend.audio.activity import apply_controls

        if center.loop:
            center.loop.configure_voice(document.voice_controls.model_dump())
        else:
            apply_controls(center.s, document.voice_controls.model_dump())
    if document.ambient is not None:
        center.bus.request("dashboard", {"op": "settings", "value": document.ambient})
    if "models" in document.settings:
        await center.save("models", document.settings["models"])
    center.publish(
        message=(
            "Копия восстановлена. Сценарии добавлены отключёнными, история не удалялась. "
            "Ограничения доступа текущего Mac сохранены."
        )
    )
