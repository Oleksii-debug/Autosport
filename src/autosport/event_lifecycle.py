from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable, Iterable

from .domain import _canonical_sport_value
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .providers import _scoped_identity
from .storage import SQLiteMarketStore


class CatalogLifecycleError(ValueError):
    """Base error for durable catalog/lifecycle evidence."""


class CatalogCursorError(CatalogLifecycleError):
    """Raised when catalog cursor continuity cannot be proven."""


class CatalogConflictError(CatalogLifecycleError):
    """Raised when immutable event identity/evidence is contradicted."""


class EventPhase(StrEnum):
    PRE_MATCH = "pre_match"
    LIVE = "live"
    COMPLETED = "completed"


class EvidenceEligibility(StrEnum):
    ELIGIBLE = "eligible"
    WAIT_EVIDENCE = "wait_evidence"
    COMPLETED = "completed"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-empty trimmed string")
    return value


def _instant(value: object, name: str) -> datetime:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware ISO-8601")
    return parsed.astimezone(timezone.utc)


def _optional_instant(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    return _instant(value, name)


def _canonical_digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def canonical_event_identity(*, source_id: str, sport: str, event_id: str) -> str:
    """Reuse the persisted MarketEvent identity from the provider boundary."""
    _canonical_sport_value(sport)
    return _scoped_identity(
        _text(source_id, "source_id"),
        _text(event_id, "event_id"),
    )


@dataclass(frozen=True, slots=True)
class CatalogEvent:
    source_id: str
    sport: str
    event_id: str
    phase: EventPhase
    available_at: str
    scheduled_start_at: str | None = None
    completion_ref: str | None = None
    settlement_ref: str | None = None

    def validate(self) -> None:
        _text(self.source_id, "source_id")
        _canonical_sport_value(self.sport)
        _text(self.event_id, "event_id")
        if not isinstance(self.phase, EventPhase):
            try:
                EventPhase(self.phase)
            except ValueError as exc:
                raise ValueError("unsupported event phase") from exc
        _instant(self.available_at, "available_at")
        _optional_instant(self.scheduled_start_at, "scheduled_start_at")
        _optional_instant(self.completion_discovered_at, "completion_discovered_at")
        _optional_instant(self.settlement_discovered_at, "settlement_discovered_at")
        if self.completion_ref is not None:
            _text(self.completion_ref, "completion_ref")
        if self.settlement_ref is not None:
            _text(self.settlement_ref, "settlement_ref")
        if self.completion_ref is None and self.completion_discovered_at is not None:
            raise ValueError("completion_discovered_at requires completion_ref or completed phase")
        if self.settlement_ref is not None and self.settlement_discovered_at is None:
            raise ValueError("settlement evidence requires durable discovery time")
        if self.phase is EventPhase.COMPLETED and self.completion_discovered_at is None:
            raise ValueError("completed phase requires durable completion discovery time")
        if self.phase is not EventPhase.COMPLETED and (
            self.completion_ref is not None or self.settlement_ref is not None
        ):
            raise ValueError("completion/settlement evidence requires completed phase")

    @property
    def identity(self) -> str:
        self.validate()
        return canonical_event_identity(
            source_id=self.source_id,
            sport=self.sport,
            event_id=self.event_id,
        )

    def to_dict(self) -> dict[str, object]:
        self.validate()
        value = asdict(self)
        value["phase"] = self.phase.value
        return value

    @classmethod
    def from_dict(cls, raw: object) -> "CatalogEvent":
        if type(raw) is not dict:
            raise ValueError("catalog event must be a JSON object")
        value = dict(raw)
        value["phase"] = EventPhase(value["phase"])
        item = cls(**value)
        item.validate()
        return item


@dataclass(frozen=True, slots=True)
class CatalogPage:
    source_id: str
    stream_epoch: str
    cursor: str
    position: int
    events: tuple[CatalogEvent, ...]
    epoch_changed: bool = False

    def validate(self) -> None:
        _text(self.source_id, "source_id")
        _text(self.stream_epoch, "stream_epoch")
        _text(self.cursor, "cursor")
        if type(self.position) is not int or self.position < 0:
            raise ValueError("position must be a non-negative non-boolean int")
        if type(self.events) is not tuple:
            raise ValueError("events must be a tuple")
        identities: set[str] = set()
        for event in self.events:
            if not isinstance(event, CatalogEvent):
                raise TypeError("events must contain CatalogEvent values")
            event.validate()
            if event.source_id != self.source_id:
                raise ValueError("catalog page cannot mix source_id values")
            if event.identity in identities:
                raise ValueError("catalog page contains duplicate canonical event identity")
            identities.add(event.identity)

    @property
    def digest(self) -> str:
        self.validate()
        return _canonical_digest(
            {
                "source_id": self.source_id,
                "stream_epoch": self.stream_epoch,
                "cursor": self.cursor,
                "position": self.position,
                "events": [event.to_dict() for event in self.events],
                "epoch_changed": self.epoch_changed,
            }
        )


@dataclass(frozen=True, slots=True)
class CatalogCheckpoint:
    source_id: str
    stream_epoch: str
    cursor: str
    position: int
    page_sha256: str

    def __post_init__(self) -> None:
        _text(self.source_id, "source_id")
        _text(self.stream_epoch, "stream_epoch")
        _text(self.cursor, "cursor")
        if type(self.position) is not int or self.position < 0:
            raise ValueError("position must be a non-negative non-boolean int")
        if type(self.page_sha256) is not str or len(self.page_sha256) != 64:
            raise ValueError("page_sha256 must be a SHA-256 hex digest")
        if any(character not in "0123456789abcdef" for character in self.page_sha256):
            raise ValueError("page_sha256 must be lowercase SHA-256 hex")


@dataclass(frozen=True, slots=True)
class EventLifecycleRecord:
    identity: str
    source_id: str
    sport: str
    event_id: str
    phase: EventPhase
    first_discovered_at: str
    last_available_at: str
    scheduled_start_at: str | None
    completion_ref: str | None
    settlement_ref: str | None
    completion_discovered_at: str | None = None
    settlement_discovered_at: str | None = None

    def __post_init__(self) -> None:
        _text(self.identity, "identity")
        _text(self.source_id, "source_id")
        _canonical_sport_value(self.sport)
        _text(self.event_id, "event_id")
        if not isinstance(self.phase, EventPhase):
            raise ValueError("phase must be EventPhase")
        _instant(self.first_discovered_at, "first_discovered_at")
        _instant(self.last_available_at, "last_available_at")
        _optional_instant(self.scheduled_start_at, "scheduled_start_at")
        if self.completion_ref is not None:
            _text(self.completion_ref, "completion_ref")
        if self.settlement_ref is not None:
            _text(self.settlement_ref, "settlement_ref")
        expected = canonical_event_identity(
            source_id=self.source_id,
            sport=self.sport,
            event_id=self.event_id,
        )
        if self.identity != expected:
            raise CatalogConflictError("durable lifecycle identity does not match canonical event identity")
        if self.phase is not EventPhase.COMPLETED and (
            self.completion_ref is not None or self.settlement_ref is not None
        ):
            raise ValueError("completion/settlement evidence requires completed phase")

    @classmethod
    def from_dict(cls, raw: object) -> "EventLifecycleRecord":
        if type(raw) is not dict:
            raise ValueError("lifecycle record must be a JSON object")
        expected = {
            "identity",
            "source_id",
            "sport",
            "event_id",
            "phase",
            "first_discovered_at",
            "last_available_at",
            "scheduled_start_at",
            "completion_ref",
            "settlement_ref",
            "completion_discovered_at",
            "settlement_discovered_at",
        }
        legacy_expected = expected - {"completion_discovered_at", "settlement_discovered_at"}
        if set(raw) not in {expected, legacy_expected}:
            raise ValueError("lifecycle record fields mismatch")
        value = dict(raw)
        value.setdefault("completion_discovered_at", None)
        value.setdefault("settlement_discovered_at", None)
        value["phase"] = EventPhase(value["phase"])
        return cls(**value)

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["phase"] = self.phase.value
        return value


@dataclass(frozen=True, slots=True)
class EventEvidenceAssessment:
    identity: str
    status: EvidenceEligibility
    evidence_first_available_at: str | None
    required_history_seconds: int
    detail: str

    @property
    def eligible(self) -> bool:
        return self.status is EvidenceEligibility.ELIGIBLE


class ContinuousEventLifecycle:
    """Durable catalog cursor + lifecycle evidence over canonical market truth.

    This object is not a scheduler, market mirror, settlement engine or economic
    authority. Call refresh_once from the existing process loop. It persists only
    catalog identity/cursor/provenance, registers eligible event selectors through the
    existing live dependency seam, and reads quote history from SQLiteMarketStore.
    """

    _SCHEMA = "autosport.continuous_event_lifecycle"
    _VERSION = 1
    _PHASE_RANK = {
        EventPhase.PRE_MATCH: 0,
        EventPhase.LIVE: 1,
        EventPhase.COMPLETED: 2,
    }

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            atomic_write_json(self.path, self._empty())
        self._read()

    @classmethod
    def _empty(cls) -> dict[str, object]:
        return {
            "schema": cls._SCHEMA,
            "schema_version": cls._VERSION,
            "sources": {},
            "events": {},
        }

    def _read(self) -> dict[str, object]:
        try:
            raw = strict_json_loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise CatalogLifecycleError("cannot verify catalog lifecycle state") from exc
        if (
            type(raw) is not dict
            or set(raw) != {"schema", "schema_version", "sources", "events"}
            or raw["schema"] != self._SCHEMA
            or raw["schema_version"] != self._VERSION
            or type(raw["sources"]) is not dict
            or type(raw["events"]) is not dict
        ):
            raise CatalogLifecycleError("unsupported catalog lifecycle state")
        try:
            for source_key, checkpoint in raw["sources"].items():
                if source_key != checkpoint.get("source_id"):
                    raise ValueError("catalog checkpoint source key mismatch")
                CatalogCheckpoint(**checkpoint)
            for identity, record in raw["events"].items():
                if identity != record.get("identity"):
                    raise ValueError("catalog event identity key mismatch")
                EventLifecycleRecord.from_dict(record)
        except (AttributeError, KeyError, TypeError, ValueError, CatalogLifecycleError) as exc:
            raise CatalogLifecycleError("catalog lifecycle state contains invalid nested evidence") from exc
        return raw

    def checkpoint(self, source_id: str) -> CatalogCheckpoint | None:
        source = self._read()["sources"].get(_text(source_id, "source_id"))
        if source is None:
            return None
        return CatalogCheckpoint(**source)

    def records(self) -> tuple[EventLifecycleRecord, ...]:
        values = [
            EventLifecycleRecord.from_dict(value)
            for value in self._read()["events"].values()
        ]
        return tuple(sorted(values, key=lambda item: item.identity))

    def get(self, identity: str) -> EventLifecycleRecord | None:
        raw = self._read()["events"].get(_text(identity, "identity"))
        return None if raw is None else EventLifecycleRecord.from_dict(raw)

    @staticmethod
    def _event_record(
        event: CatalogEvent,
        *,
        discovered_at: str,
        previous: EventLifecycleRecord | None,
    ) -> EventLifecycleRecord:
        if previous is None:
            return EventLifecycleRecord(
                identity=event.identity,
                source_id=event.source_id,
                sport=event.sport,
                event_id=event.event_id,
                phase=event.phase,
                first_discovered_at=discovered_at,
                last_available_at=event.available_at,
                scheduled_start_at=event.scheduled_start_at,
                completion_ref=event.completion_ref,
                settlement_ref=event.settlement_ref,
                completion_discovered_at=(
                    discovered_at if event.phase is EventPhase.COMPLETED else None
                ),
                settlement_discovered_at=(
                    discovered_at if event.settlement_ref is not None else None
                ),
            )
        if (
            previous.source_id != event.source_id
            or previous.sport != event.sport
            or previous.event_id != event.event_id
        ):
            raise CatalogConflictError("canonical event identity changed")
        if (
            previous.scheduled_start_at is not None
            and event.scheduled_start_at is not None
            and previous.scheduled_start_at != event.scheduled_start_at
        ):
            raise CatalogConflictError(
                "scheduled_start_at changed without versioned correction evidence"
            )
        if (
            ContinuousEventLifecycle._PHASE_RANK[event.phase]
            < ContinuousEventLifecycle._PHASE_RANK[previous.phase]
        ):
            raise CatalogConflictError("event lifecycle cannot move backwards")
        if _instant(event.available_at, "available_at") < _instant(
            previous.last_available_at, "last_available_at"
        ):
            raise CatalogConflictError("event evidence availability cannot move backwards")
        completion_ref = event.completion_ref or previous.completion_ref
        settlement_ref = event.settlement_ref or previous.settlement_ref
        completion_discovered_at = previous.completion_discovered_at
        if previous.phase is not EventPhase.COMPLETED and event.phase is EventPhase.COMPLETED:
            completion_discovered_at = discovered_at
        settlement_discovered_at = previous.settlement_discovered_at
        if previous.settlement_ref is None and settlement_ref is not None:
            settlement_discovered_at = discovered_at
        if (
            previous.completion_ref is not None
            and event.completion_ref is not None
            and previous.completion_ref != event.completion_ref
        ):
            raise CatalogConflictError("completion_ref conflicts with durable evidence")
        if (
            previous.settlement_ref is not None
            and event.settlement_ref is not None
            and previous.settlement_ref != event.settlement_ref
        ):
            raise CatalogConflictError("settlement_ref conflicts with durable evidence")
        return EventLifecycleRecord(
            identity=previous.identity,
            source_id=previous.source_id,
            sport=previous.sport,
            event_id=previous.event_id,
            phase=event.phase,
            first_discovered_at=previous.first_discovered_at,
            last_available_at=event.available_at,
            scheduled_start_at=previous.scheduled_start_at or event.scheduled_start_at,
            completion_ref=completion_ref,
            settlement_ref=settlement_ref,
            completion_discovered_at=completion_discovered_at,
            settlement_discovered_at=settlement_discovered_at,
        )

    def apply_page(self, page: CatalogPage, *, discovered_at: str) -> tuple[str, ...]:
        page.validate()
        now = _instant(discovered_at, "discovered_at")
        for event in page.events:
            if _instant(event.available_at, "available_at") > now:
                raise CatalogLifecycleError(
                    "catalog evidence cannot be available after discovery cutoff"
                )

        raw = self._read()
        sources = raw["sources"]
        events = raw["events"]
        previous_raw = sources.get(page.source_id)
        previous = None if previous_raw is None else CatalogCheckpoint(**previous_raw)
        if previous is not None:
            if page.stream_epoch == previous.stream_epoch:
                if page.position == previous.position:
                    if (
                        page.cursor == previous.cursor
                        and page.digest == previous.page_sha256
                    ):
                        return ()
                    raise CatalogCursorError(
                        "equal catalog position conflicts with durable page evidence"
                    )
                if page.position != previous.position + 1:
                    raise CatalogCursorError("catalog cursor gap or regression detected")
            elif not page.epoch_changed:
                raise CatalogCursorError(
                    "catalog stream epoch changed without explicit epoch_changed evidence"
                )
        elif page.epoch_changed:
            raise CatalogCursorError("first catalog page cannot claim an epoch change")

        changed: list[str] = []
        for event in page.events:
            previous_event_raw = events.get(event.identity)
            previous_event = (
                None
                if previous_event_raw is None
                else EventLifecycleRecord.from_dict(previous_event_raw)
            )
            candidate = self._event_record(
                event,
                discovered_at=discovered_at,
                previous=previous_event,
            )
            if previous_event != candidate:
                events[event.identity] = candidate.to_dict()
                changed.append(event.identity)

        sources[page.source_id] = asdict(
            CatalogCheckpoint(
                source_id=page.source_id,
                stream_epoch=page.stream_epoch,
                cursor=page.cursor,
                position=page.position,
                page_sha256=page.digest,
            )
        )
        atomic_write_json(self.path, raw)
        return tuple(changed)

    def refresh_once(
        self,
        fetch_page: Callable[[CatalogCheckpoint | None], CatalogPage],
        *,
        source_id: str,
        discovered_at: str,
    ) -> tuple[str, ...]:
        """Poll one catalog page from the existing process/scheduler boundary."""
        if not callable(fetch_page):
            raise TypeError("fetch_page must be callable")
        current = self.checkpoint(source_id)
        page = fetch_page(current)
        if not isinstance(page, CatalogPage):
            raise TypeError("fetch_page must return CatalogPage")
        if page.source_id != source_id:
            raise CatalogConflictError("catalog reader returned the wrong source_id")
        return self.apply_page(page, discovered_at=discovered_at)

    def refresh_and_register(
        self,
        fetch_page: Callable[[CatalogCheckpoint | None], CatalogPage],
        store: SQLiteMarketStore,
        *,
        source_id: str,
        discovered_at: str,
        required_history: timedelta,
        register_input: Callable[..., None],
        retire_input: Callable[[str], object] | None = None,
    ) -> tuple[str, ...]:
        """Discover and route eligible events without a manual event seed list."""
        self.refresh_once(
            fetch_page,
            source_id=source_id,
            discovered_at=discovered_at,
        )
        return self.register_eligible(
            store,
            as_of=discovered_at,
            required_history=required_history,
            register_input=register_input,
            retire_input=retire_input,
        )

    def assess_evidence(
        self,
        identity: str,
        store: SQLiteMarketStore,
        *,
        as_of: str,
        required_history: timedelta,
    ) -> EventEvidenceAssessment:
        if not isinstance(store, SQLiteMarketStore):
            raise TypeError("store must be SQLiteMarketStore")
        if not isinstance(required_history, timedelta) or required_history < timedelta(0):
            raise ValueError("required_history must be a non-negative timedelta")
        record = self.get(identity)
        if record is None:
            raise KeyError(f"unknown catalog event {identity!r}")
        cutoff = _instant(as_of, "as_of")
        required_seconds = int(required_history.total_seconds())
        if record.phase is EventPhase.COMPLETED:
            detail = (
                "completed with settlement provenance reference; outcome authority is external"
                if record.settlement_ref is not None
                else "completed; settlement remains unresolved"
            )
            return EventEvidenceAssessment(
                identity=record.identity,
                status=EvidenceEligibility.COMPLETED,
                evidence_first_available_at=None,
                required_history_seconds=required_seconds,
                detail=detail,
            )

        availability: list[datetime] = []
        canonical_event_id = _scoped_identity(record.source_id, record.event_id)
        for event in store.events(canonical_event_id):
            if event.source_id != record.source_id or event.sport != record.sport:
                continue
            try:
                observed = _instant(event.observed_ts, "observed_ts")
                ingested = _instant(event.ingest_ts, "ingest_ts")
            except ValueError:
                continue
            if observed <= cutoff and ingested <= cutoff:
                availability.append(ingested)
        first = min(availability) if availability else None
        threshold = cutoff - required_history
        if first is None or first > threshold:
            return EventEvidenceAssessment(
                identity=record.identity,
                status=EvidenceEligibility.WAIT_EVIDENCE,
                evidence_first_available_at=(
                    None if first is None else first.isoformat()
                ),
                required_history_seconds=required_seconds,
                detail=(
                    "insufficient causally available captured history; "
                    "late discovery cannot backfill strategy evidence"
                ),
            )
        return EventEvidenceAssessment(
            identity=record.identity,
            status=EvidenceEligibility.ELIGIBLE,
            evidence_first_available_at=first.isoformat(),
            required_history_seconds=required_seconds,
            detail="required captured history is causally available",
        )

    def register_eligible(
        self,
        store: SQLiteMarketStore,
        *,
        as_of: str,
        required_history: timedelta,
        register_input: Callable[..., None],
        identities: Iterable[str] | None = None,
        retire_input: Callable[[str], object] | None = None,
    ) -> tuple[str, ...]:
        """Register evidence-sufficient events through the existing live-loop seam."""
        if not callable(register_input):
            raise TypeError("register_input must be callable")
        selected = (
            {record.identity for record in self.records()}
            if identities is None
            else set(identities)
        )
        registered: list[str] = []
        for identity in sorted(selected):
            record = self.get(identity)
            if record is None:
                raise CatalogLifecycleError(f"unknown catalog event {identity!r}")
            input_id = f"catalog:{record.identity}"
            if record.phase is EventPhase.COMPLETED:
                if retire_input is not None:
                    retire_input(input_id)
                continue
            assessment = self.assess_evidence(
                identity,
                store,
                as_of=as_of,
                required_history=required_history,
            )
            if not assessment.eligible:
                continue
            record = self.get(identity)
            assert record is not None
            input_id = f"catalog:{record.identity}"
            register_input(
                input_id,
                source_ids=record.source_id,
                sports=record.sport,
                event_ids=record.identity,
            )
            registered.append(input_id)
        return tuple(registered)