"""Playback lifecycle shared by Safari, WebKit and the owned AVKit player.

Catalogue availability and configured capabilities are deliberately separate from
the evidence returned by a concrete player. No state transition opens a URL.
"""

import json
import math
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from enum import StrEnum

from backend.cinema.classification import PROMO_TYPES, classify
from backend.tools.registry import ToolError


class PlaybackState(StrEnum):
    IDLE = "IDLE"
    SEARCHING = "SEARCHING"
    VALIDATING_SOURCE = "VALIDATING_SOURCE"
    CHECKING_REGION = "CHECKING_REGION"
    CHECKING_PLAYER = "CHECKING_PLAYER"
    READY = "READY"
    OPENING = "OPENING"
    WAITING_FOR_PLAYER = "WAITING_FOR_PLAYER"
    STARTING = "STARTING"
    RESTORING_POSITION = "RESTORING_POSITION"
    ENTERING_FULLSCREEN = "ENTERING_FULLSCREEN"
    PLAYING = "PLAYING"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


def playback_error(code, message):
    error = ToolError(message)
    error.code = code
    return error


def valid_sample(value):
    return (
        all(
            type(value.get(k)) in {int, float} and math.isfinite(value[k])
            for k in ("position", "duration")
        )
        and value["duration"] > 0
        and 0 <= value["position"] <= value["duration"] + 2
        and not value.get("seeking")
        and not value.get("error")
    )


def duration_matches(expected, observed):
    """A known asset duration cannot be replaced by a preview of the same work."""
    return (
        type(observed) in {int, float}
        and math.isfinite(observed)
        and observed > 0
        and (
            not expected
            or (
                type(expected) in {int, float}
                and math.isfinite(expected)
                and expected > 0
                and abs(expected - observed) <= max(2, expected * 0.02)
            )
        )
    )


@dataclass
class PlaybackSource:
    url: str
    title_id: str
    episode_id: str
    source_id: str
    link_id: str
    adapter_id: str
    content_type: str
    voice: str = ""
    capabilities: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)


class SourceValidator:
    @staticmethod
    def player(query, evidence, direct_url="", verification=None):
        result = classify(query, evidence)
        if not direct_url or result["video_type"] in PROMO_TYPES:
            return result
        # A direct asset has no HTML movie metadata. Its trusted, link-bound
        # provider verification can only apply to that exact active media URL.
        proof = verification or {}
        runtime = (query.get("metadata") or {}).get("runtime", 0) * 60
        duration = evidence.get("duration", 0)
        from backend.cinema.archive_open import verified_alias

        if proof.get("duration") and not duration_matches(proof["duration"], duration):
            return {
                **result,
                "content_verified": False,
                "classification_reason": "Длительность активного видео не совпала с полным "
                "произведением; источник мог вернуть превью.",
            }
        if (
            proof.get("content_verified") is True
            and proof.get("video_type")
            == ("episode" if query.get("kind") == "series" else "full_movie")
            and (
                evidence.get("media_url", "").split("#", 1)[0] == direct_url.split("#", 1)[0]
                or verified_alias(proof, direct_url, evidence.get("media_url", ""), query)
            )
            and duration > 0
            and duration_matches(proof.get("duration", 0), duration)
            and (not runtime or abs(runtime - duration) <= max(90, runtime * 0.25))
        ):
            result.update(
                content_verified=True,
                video_type=proof["video_type"],
                classification_reason="Активный прямой файл совпал с проверенным источником "
                "этого названия и серии; длительность не противоречит данным.",
            )
        return result

    def validate(self, target, verification):
        query = {
            **target["item"],
            "season": target["episode"]["season"],
            "episode": target["episode"]["episode"],
        }
        marker = classify(query, {"page_url": target["link"]["url"]})
        if marker["video_type"] in PROMO_TYPES or verification.get("video_type") in PROMO_TYPES:
            raise playback_error(
                "promotional_content",
                "Источник для просмотра не найден: сохранённая ссылка ведёт на промо-видео.",
            )
        return PlaybackSource(
            url=target["link"]["url"],
            title_id=target["item"]["id"],
            episode_id=target["episode"]["id"],
            source_id=target["source"]["id"],
            link_id=target["link"]["id"],
            adapter_id=target["source"]["adapter"],
            content_type=verification.get("video_type", "unknown"),
            voice=target["link"].get("voice", ""),
            validation={**verification, "live_player_verified": False},
        )


class SourceResolver:
    """Reuse the existing public adapters and durable per-episode source selection."""

    def __init__(self, sources):
        self.sources = sources
        self.checks = OrderedDict()
        self.reliability = OrderedDict()

    async def resolve(self, target, region):
        """Cache exact work/episode/configuration checks, never a PLAYING result."""
        source, item, ep = target["source"], target["item"], target["episode"]
        key = (
            json.dumps(source, sort_keys=True),
            target["link"]["url"],
            item["id"],
            ep["id"],
            region,
        )
        cached = self.checks.get(key)
        if cached and cached[0] > time.monotonic():
            return dict(cached[1])
        result = await self.sources.adapter(source).resolve(
            {
                "url": target["link"]["url"],
                "_expected": {
                    **item,
                    "season": ep["season"],
                    "episode": ep["episode"],
                    "region": region,
                },
            }
        )
        self.checks[key] = (
            time.monotonic() + (300 if result.get("availability") == "available" else 30),
            dict(result),
        )
        while len(self.checks) > 128:
            self.checks.popitem(last=False)
        return result

    def record(self, source_id, success):
        old = self.reliability.get(source_id, (0, 0))
        self.reliability[source_id] = (
            min(20, old[0] + int(success)),
            min(20, old[1] + int(not success)),
        )
        self.reliability.move_to_end(source_id)
        while len(self.reliability) > 64:
            self.reliability.popitem(last=False)

    def fallback_order(self, rows):
        def reliability(row):
            good, bad = self.reliability.get(row["source_id"], (0, 0))
            return row.get("score", 0) + 20 * (good + 1) / (good + bad + 2)

        return sorted(
            (
                r
                for r in rows
                if (
                    r.get("auto_eligible")
                    and r.get("allowed")
                    and not r.get("requires_confirmation")
                    and not r.get("new_source")
                    and r.get("content_verified")
                    and r.get("video_type") in {"full_movie", "episode"}
                    and r.get("availability") == "available"
                    and r.get("region_access") != "blocked"
                    and r.get("scope") in {"movie", "episode"}
                )
            ),
            key=reliability,
            reverse=True,
        )

    async def search(self, *args, **kwargs):
        return await self.sources.search(*args, **kwargs)

    @staticmethod
    def preferred(available, item, episode, prefs):
        memory = item.get("source_memory", {})
        return sorted(
            available,
            key=lambda choice: (
                choice["source"]["id"] == memory.get("pinned_source_id"),
                prefs.use_last_source and choice["link"]["id"] == episode["last_link_id"],
                prefs.use_last_source and choice["source"]["id"] == memory.get("last_source_id"),
                choice["source"]["preferred"],
                -choice["source"]["priority"],
            ),
            reverse=True,
        )


class PlayerController:
    """One controller owns an adapter; it never upgrades page-open to playback."""

    def __init__(self, adapter):
        self.adapter = adapter

    async def open(self, target):
        result = await self.adapter.open(target)
        if result.get("playback_confirmed") and (
            not valid_sample(result) or result.get("paused") is not False
        ):
            raise playback_error(
                "invalid_confirmation", "Плеер не подтвердил корректное время видео."
            )
        return result

    async def sample(self):
        return await self.adapter.sample()

    async def control(self, action, position=0):
        result = await self.adapter.control(action, position)
        if action == "play" and (
            result.get("playback_confirmed") is not True
            or result.get("paused") is not False
            or not valid_sample(result)
        ):
            raise playback_error("not_playing", "Плеер не подтвердил движение времени видео.")
        return result


class PlaybackSession(dict):
    """Legacy session fields remain accessible while ownership lives in one manager."""

    def __init__(self, source, **values):
        super().__init__(values)
        self.source = source
        self.controller = PlayerController(values["adapter"])


class PlaybackManager:
    # New user requests may search/validate from any state; teardown may always idle.
    transitions = {
        PlaybackState.CHECKING_REGION: {"CHECKING_PLAYER", "READY", "BLOCKED", "FAILED"},
        PlaybackState.CHECKING_PLAYER: {"READY", "BLOCKED", "FAILED"},
        PlaybackState.READY: {"OPENING", "WAITING_FOR_PLAYER", "STARTING", "BLOCKED", "FAILED"},
        PlaybackState.IDLE: {"BLOCKED", "FAILED"},
        PlaybackState.SEARCHING: {"BLOCKED", "FAILED"},
        PlaybackState.VALIDATING_SOURCE: {
            "OPENING",
            "WAITING_FOR_PLAYER",
            "STARTING",
            "PAUSED",
            "BLOCKED",
            "FAILED",
        },
        PlaybackState.OPENING: {"WAITING_FOR_PLAYER", "STARTING", "PAUSED", "BLOCKED", "FAILED"},
        PlaybackState.WAITING_FOR_PLAYER: {"STARTING", "PAUSED", "BLOCKED", "FAILED"},
        PlaybackState.STARTING: {
            "WAITING_FOR_PLAYER",
            "RESTORING_POSITION",
            "ENTERING_FULLSCREEN",
            "PAUSED",
            "BLOCKED",
            "FAILED",
        },
        PlaybackState.RESTORING_POSITION: {
            "STARTING",
            "WAITING_FOR_PLAYER",
            "ENTERING_FULLSCREEN",
            "PAUSED",
            "BLOCKED",
            "FAILED",
        },
        PlaybackState.ENTERING_FULLSCREEN: {
            "STARTING",
            "WAITING_FOR_PLAYER",
            "PAUSED",
            "BLOCKED",
            "FAILED",
        },
        PlaybackState.PLAYING: {"WAITING_FOR_PLAYER", "STARTING", "PAUSED", "BLOCKED", "FAILED"},
        PlaybackState.PAUSED: {"WAITING_FOR_PLAYER", "STARTING", "BLOCKED", "FAILED"},
        PlaybackState.BLOCKED: {"OPENING", "WAITING_FOR_PLAYER", "STARTING", "PAUSED", "FAILED"},
        PlaybackState.FAILED: {"OPENING", "WAITING_FOR_PLAYER", "STARTING", "PAUSED", "BLOCKED"},
    }

    def __init__(self):
        self.session = None
        self.state = PlaybackState.IDLE
        self.code = ""
        self.message = ""
        self.revision = 0
        self.last_successful_at = None

    def transition(self, state, message="", code=""):
        state = PlaybackState(state)
        # PLAYING is only entered by confirmed()/observe(), never diagnostic stages.
        if state == PlaybackState.PLAYING:
            raise ValueError("PLAYING requires a confirmed player sample")
        if state == PlaybackState.PAUSED and not self.session:
            raise ValueError("PAUSED requires a player session")
        if (
            state != self.state
            and state
            not in {
                PlaybackState.IDLE,
                PlaybackState.SEARCHING,
                PlaybackState.VALIDATING_SOURCE,
                PlaybackState.CHECKING_REGION,
                PlaybackState.CHECKING_PLAYER,
                PlaybackState.READY,
            }
            and state not in self.transitions[self.state]
        ):
            raise ValueError(f"Invalid playback transition: {self.state} → {state}")
        self.state, self.message, self.code = state, message, code
        self.revision += 1

    def bind(self, source, **values):
        self.session = PlaybackSession(source, **values)
        self.last_successful_at = None
        self.transition(PlaybackState.VALIDATING_SOURCE)
        return self.session

    def stage(self, name, status, message):
        states = {
            "OPEN_SAFARI": PlaybackState.OPENING,
            "OPEN_NATIVE": PlaybackState.OPENING,
            "DETECT_PLAYER": PlaybackState.WAITING_FOR_PLAYER,
            "VERIFY": PlaybackState.VALIDATING_SOURCE,
            "RESTORE_POSITION": PlaybackState.RESTORING_POSITION,
            "FULLSCREEN": PlaybackState.ENTERING_FULLSCREEN,
            "PLAY": PlaybackState.STARTING,
        }
        if status in {"limited", "rejected", "failed"}:
            self.transition(PlaybackState.BLOCKED, message, name.lower())
        elif name in states and self.state != PlaybackState.PLAYING:
            self.transition(states[name], message)

    def confirmed(self, sample):
        if (
            not self.session
            or sample.get("playback_confirmed") is not True
            or sample.get("paused") is not False
            or not valid_sample(sample)
        ):
            return False
        self.state, self.code = PlaybackState.PLAYING, ""
        self.message = sample.get("message", "Воспроизведение подтверждено движением времени.")
        self.last_successful_at = time.time()
        self.session["last_advance"] = time.monotonic()
        self.session["manager_position"] = sample["position"]
        source = self.session.source
        source.validation["live_player_verified"] = True
        for key in ("content_verified", "video_type", "classification_reason"):
            if key in sample:
                source.validation[key] = sample[key]
        if sample.get("video_type"):
            source.content_type = sample["video_type"]
        source.capabilities.update(sample.get("capabilities") or {})
        source.capabilities["fullscreen"] = (
            sample.get("fullscreen_confirmed") is True or sample.get("fullscreen") is True
        )
        self.revision += 1
        return True

    def outcome(self, result):
        if self.confirmed(result):
            return
        paused = result.get("paused") is True and valid_sample(result)
        self.transition(
            PlaybackState.PAUSED if paused else PlaybackState.BLOCKED,
            result.get("message", "Запуск видео не подтверждён."),
            result.get("error_code", "not_confirmed"),
        )

    def observe(self, sample, event="periodic"):
        if not self.session or not valid_sample(sample):
            return
        previous = self.session.get("manager_position", self.session.get("sample_position", 0))
        self.session["manager_position"] = sample["position"]
        if (
            event != "seek"
            and sample.get("paused") is False
            and sample["position"] > previous + 0.05
        ):
            self.confirmed({**sample, "playback_confirmed": True})
        elif sample.get("paused") is True and self.session.get("counted"):
            self.transition(PlaybackState.PAUSED, "Фильм на паузе.")
        elif sample.get("paused") is False and self.state == PlaybackState.PAUSED:
            self.transition(PlaybackState.STARTING, "Ожидаю движения времени видео.")
        elif sample.get("paused") is False and self.session.get("last_advance"):
            stalled = time.monotonic() - self.session["last_advance"]
            if stalled > 30:
                self.transition(
                    PlaybackState.BLOCKED,
                    "Видео не продвигается. Проверь загрузку в плеере.",
                    "playback_stalled",
                )
            elif stalled > 10:
                self.transition(PlaybackState.STARTING, "Ожидаю загрузки и движения времени видео.")
        if sample.get("capabilities"):
            self.session.source.capabilities.update(sample["capabilities"])

    def failure(self, error):
        code = getattr(error, "code", "player_unavailable")
        self.transition(
            PlaybackState.BLOCKED
            if code
            in {
                "automation_unknown",
                "automation_denied",
                "permission_denied",
                "javascript_denied",
                "javascript_disabled",
                "cross_origin_player",
                "protected_player",
                "gesture_required",
                "promotional_content",
                "page_changed",
                "tab_changed",
                "player_changed",
                "region_blocked",
                "authorization_required",
                "access_restricted",
            }
            else PlaybackState.FAILED,
            str(error),
            code,
        )

    def clear(self, preserve_error=False):
        self.session = None
        if not preserve_error:
            self.transition(PlaybackState.IDLE)

    def snapshot(self):
        return {
            "state": self.state.value,
            "code": self.code,
            "message": self.message,
            "revision": self.revision,
            "last_successful_at": self.last_successful_at,
            "token": self.session["token"] if self.session else "",
            "pending_command": self.session.get("control", {}).get("action", "")
            if self.session
            else "",
            "source": asdict(self.session.source) if self.session else None,
        }
