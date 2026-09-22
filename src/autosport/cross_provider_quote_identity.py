"""Causal provider-neutral identity for source-scoped market quotes.

Provider IDs remain immutable provenance. Cross-provider equivalence is accepted only
from explicit evidence and the canonical participant roster authority. This module is
read-only intelligence infrastructure and grants no execution/settlement/risk authority.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .domain import MarketEvent
from .integrity import atomic_write_json
from .participant_identity import (
    EntityKind,
    IdentityView,
    ParticipantIdentityError,
    ParticipantIdentityRegistry,
)

_SCHEMA = "autosport.cross_provider_quote_identity"
_VERSION = 1


class CrossProviderQuoteIdentityError(ValueError):
    pass


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise CrossProviderQuoteIdentityError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _time(name: str, value: object) -> datetime:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CrossProviderQuoteIdentityError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CrossProviderQuoteIdentityError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc(name: str, value: object) -> str:
    return _time(name, value).isoformat().replace("+00:00", "Z")


def _sha(name: str, value: object) -> str:
    text = _text(name, value)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise CrossProviderQuoteIdentityError(f"{name} must be lowercase SHA-256")
    return text


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _scoped(source_id: str, name: str, value: object) -> str:
    text = _text(name, value)
    prefix = f"{source_id}:"
    if not text.startswith(prefix) or len(text) == len(prefix):
        raise CrossProviderQuoteIdentityError(f"{name} must preserve exact source-scoped identity")
    return text


def _contains(start: str, end: str | None, moment: datetime) -> bool:
    return _time("valid_from", start) <= moment and (end is None or moment < _time("valid_until", end))


def _overlaps(a: "QuoteIdentityMappingRecord", b: "QuoteIdentityMappingRecord") -> bool:
    a0, b0 = _time("valid_from", a.valid_from), _time("valid_from", b.valid_from)
    a1 = None if a.valid_until is None else _time("valid_until", a.valid_until)
    b1 = None if b.valid_until is None else _time("valid_until", b.valid_until)
    return (a1 is None or b0 < a1) and (b1 is None or a0 < b1)


def _strict_json(text: str) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise CrossProviderQuoteIdentityError("registry contains duplicate JSON keys")
            result[key] = value
        return result
    try:
        return json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                CrossProviderQuoteIdentityError(f"registry contains non-finite JSON value {value}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise CrossProviderQuoteIdentityError("registry must be valid JSON") from exc


@dataclass(frozen=True, slots=True)
class QuoteIdentityMappingRecord:
    source_id: str
    source_event_id: str
    source_market_id: str
    source_selection_id: str
    sport: str
    source_competition_id: str
    source_market_semantics_id: str
    canonical_event_id: str
    canonical_market_id: str
    canonical_selection_id: str
    canonical_competition_id: str
    canonical_market_semantics_id: str
    participant_entity_ids: tuple[str, ...]
    selection_entity_id: str | None
    valid_from: str
    valid_until: str | None
    available_at: str
    recorded_at: str
    evidence_ref: str
    evidence_sha256: str
    supersedes_record_id: str | None = None

    def __post_init__(self) -> None:
        source = _text("source_id", self.source_id)
        _scoped(source, "source_event_id", self.source_event_id)
        _scoped(source, "source_market_id", self.source_market_id)
        _scoped(source, "source_selection_id", self.source_selection_id)
        for name in (
            "sport", "source_competition_id", "source_market_semantics_id",
            "canonical_event_id", "canonical_market_id", "canonical_selection_id",
            "canonical_competition_id", "canonical_market_semantics_id", "evidence_ref",
        ):
            _text(name, getattr(self, name))
        if type(self.participant_entity_ids) is not tuple or not self.participant_entity_ids:
            raise CrossProviderQuoteIdentityError("participant_entity_ids must be a non-empty sorted tuple")
        for item in self.participant_entity_ids:
            _text("participant_entity_id", item)
        if self.participant_entity_ids != tuple(sorted(set(self.participant_entity_ids))):
            raise CrossProviderQuoteIdentityError("participant_entity_ids must be sorted and unique")
        if self.selection_entity_id is not None:
            _text("selection_entity_id", self.selection_entity_id)
            if self.selection_entity_id not in self.participant_entity_ids:
                raise CrossProviderQuoteIdentityError("selection_entity_id must belong to participant_entity_ids")
        start = _time("valid_from", self.valid_from)
        if self.valid_until is not None and _time("valid_until", self.valid_until) <= start:
            raise CrossProviderQuoteIdentityError("valid_until must be after valid_from")
        if _time("recorded_at", self.recorded_at) < _time("available_at", self.available_at):
            raise CrossProviderQuoteIdentityError("recorded_at cannot precede available_at")
        _sha("evidence_sha256", self.evidence_sha256)
        if self.supersedes_record_id is not None:
            _sha("supersedes_record_id", self.supersedes_record_id)

    @property
    def source_key(self) -> tuple[str, str, str, str]:
        return self.source_id, self.source_event_id, self.source_market_id, self.source_selection_id

    @property
    def semantic_key(self) -> tuple[object, ...]:
        return (
            self.sport, self.canonical_event_id, self.canonical_market_id,
            self.canonical_selection_id, self.canonical_competition_id,
            self.canonical_market_semantics_id, self.participant_entity_ids,
            self.selection_entity_id,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "source_id": self.source_id, "source_event_id": self.source_event_id,
            "source_market_id": self.source_market_id, "source_selection_id": self.source_selection_id,
            "sport": self.sport, "source_competition_id": self.source_competition_id,
            "source_market_semantics_id": self.source_market_semantics_id,
            "canonical_event_id": self.canonical_event_id, "canonical_market_id": self.canonical_market_id,
            "canonical_selection_id": self.canonical_selection_id,
            "canonical_competition_id": self.canonical_competition_id,
            "canonical_market_semantics_id": self.canonical_market_semantics_id,
            "participant_entity_ids": list(self.participant_entity_ids),
            "selection_entity_id": self.selection_entity_id,
            "valid_from": _utc("valid_from", self.valid_from),
            "valid_until": None if self.valid_until is None else _utc("valid_until", self.valid_until),
            "available_at": _utc("available_at", self.available_at),
            "recorded_at": _utc("recorded_at", self.recorded_at),
            "evidence_ref": self.evidence_ref, "evidence_sha256": self.evidence_sha256,
            "supersedes_record_id": self.supersedes_record_id,
        }

    @property
    def record_id(self) -> str:
        return _digest(self.to_payload())

    @classmethod
    def from_payload(cls, value: object) -> "QuoteIdentityMappingRecord":
        if type(value) is not dict:
            raise CrossProviderQuoteIdentityError("mapping must be a JSON object")
        expected = set(cls.__dataclass_fields__)
        if set(value) != expected:
            raise CrossProviderQuoteIdentityError("mapping fields do not match canonical schema")
        data = dict(value)
        participants = data["participant_entity_ids"]
        if type(participants) is not list:
            raise CrossProviderQuoteIdentityError("participant_entity_ids must be a JSON array")
        data["participant_entity_ids"] = tuple(participants)
        return cls(**data)


@dataclass(frozen=True, slots=True)
class CrossProviderQuoteIdentity:
    source_id: str
    source_event_id: str
    source_market_id: str
    source_selection_id: str
    sport: str
    canonical_event_id: str
    canonical_market_id: str
    canonical_selection_id: str
    canonical_competition_id: str
    canonical_market_semantics_id: str
    participant_entity_ids: tuple[str, ...]
    selection_entity_id: str | None
    mapping_record_id: str
    mapping_evidence_sha256: str
    market_event_sha256: str

    @property
    def participant_roster_sha256(self) -> str:
        return _digest({"participants": list(self.participant_entity_ids)})

    @property
    def semantic_join_key(self) -> tuple[str, ...]:
        return (
            self.sport, self.canonical_event_id, self.canonical_market_id,
            self.canonical_selection_id, self.canonical_competition_id,
            self.canonical_market_semantics_id, self.participant_roster_sha256,
            self.selection_entity_id or "<non-participant-outcome>",
        )

    @property
    def semantic_identity_sha256(self) -> str:
        return _digest({"schema": "autosport.cross_provider_quote_semantic_identity", "v": 1, "key": self.semantic_join_key})

    @property
    def resolution_sha256(self) -> str:
        return _digest(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.cross_provider_quote_identity_resolution", "schema_version": 1,
            "source_id": self.source_id, "source_event_id": self.source_event_id,
            "source_market_id": self.source_market_id, "source_selection_id": self.source_selection_id,
            "sport": self.sport, "canonical_event_id": self.canonical_event_id,
            "canonical_market_id": self.canonical_market_id, "canonical_selection_id": self.canonical_selection_id,
            "canonical_competition_id": self.canonical_competition_id,
            "canonical_market_semantics_id": self.canonical_market_semantics_id,
            "participant_entity_ids": list(self.participant_entity_ids),
            "participant_roster_sha256": self.participant_roster_sha256,
            "selection_entity_id": self.selection_entity_id, "mapping_record_id": self.mapping_record_id,
            "mapping_evidence_sha256": self.mapping_evidence_sha256,
            "market_event_sha256": self.market_event_sha256,
            "semantic_identity_sha256": self.semantic_identity_sha256,
            "claims": {"provider_native_ids_preserved": True, "identity_inferred_from_names": False,
                       "provider_universe_complete": False, "execution_authorized": False,
                       "accepted_odds_proven": False, "real_money_edge_proven": False},
        }


class CrossProviderQuoteIdentityRegistry:
    """Append-only mapping store with causal and restated resolution views."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._records: list[QuoteIdentityMappingRecord] = []
        if self.path.exists():
            self._load()

    @classmethod
    def initialize_pristine(cls, path: str | Path) -> "CrossProviderQuoteIdentityRegistry":
        registry = cls(path)
        if registry.path.exists():
            raise CrossProviderQuoteIdentityError("identity registry already exists")
        registry._persist([])
        return registry

    @property
    def state_sha256(self) -> str:
        return _digest(self._state(self._records))

    @staticmethod
    def _state(records: list[QuoteIdentityMappingRecord]) -> dict[str, object]:
        return {"schema": _SCHEMA, "schema_version": _VERSION, "mappings": [r.to_payload() for r in records]}

    def _persist(self, records: list[QuoteIdentityMappingRecord]) -> None:
        atomic_write_json(self.path, self._state(records))

    @staticmethod
    def _append(current: list[QuoteIdentityMappingRecord], record: QuoteIdentityMappingRecord) -> list[QuoteIdentityMappingRecord]:
        by_id = {item.record_id: item for item in current}
        ancestors: set[str] = set()
        if record.supersedes_record_id is not None:
            target = by_id.get(record.supersedes_record_id)
            if target is None:
                raise CrossProviderQuoteIdentityError("correction references unknown mapping")
            if target.source_key != record.source_key:
                raise CrossProviderQuoteIdentityError("correction must preserve exact source quote identity")
            if target.semantic_key == record.semantic_key:
                raise CrossProviderQuoteIdentityError("correction must change semantic identity")
            if not _overlaps(target, record):
                raise CrossProviderQuoteIdentityError("correction must overlap superseded validity")
            if _time("available_at", record.available_at) <= _time("available_at", target.available_at):
                raise CrossProviderQuoteIdentityError("correction must become available later")
            if _time("recorded_at", record.recorded_at) <= _time("recorded_at", target.recorded_at):
                raise CrossProviderQuoteIdentityError("correction must be recorded later")
            if any(item.supersedes_record_id == target.record_id for item in current):
                raise CrossProviderQuoteIdentityError("correction fork is not allowed")
            cursor: QuoteIdentityMappingRecord | None = target
            while cursor is not None:
                if cursor.record_id in ancestors:
                    raise CrossProviderQuoteIdentityError("correction cycle is not allowed")
                ancestors.add(cursor.record_id)
                cursor = None if cursor.supersedes_record_id is None else by_id.get(cursor.supersedes_record_id)
                if cursor is None and target.supersedes_record_id is not None and target.supersedes_record_id not in ancestors:
                    raise CrossProviderQuoteIdentityError("correction ancestry is incomplete")
        for existing in current:
            if existing.source_key != record.source_key or not _overlaps(existing, record):
                continue
            if existing.record_id in ancestors:
                continue
            if existing.semantic_key == record.semantic_key:
                raise CrossProviderQuoteIdentityError("overlapping duplicate mapping requires exact record reuse")
            raise CrossProviderQuoteIdentityError("overlapping semantic mapping requires correction lineage")
        result = [*current, record]
        result.sort(key=lambda r: (r.source_key, _utc("valid_from", r.valid_from), _utc("recorded_at", r.recorded_at), r.record_id))
        return result

    def add_mapping(self, record: QuoteIdentityMappingRecord) -> None:
        if type(record) is not QuoteIdentityMappingRecord:
            raise TypeError("record must be QuoteIdentityMappingRecord")
        if any(item.record_id == record.record_id for item in self._records):
            return
        candidate = self._append(self._records, record)
        self._persist(candidate)
        self._records = candidate

    def resolve(
        self,
        event: MarketEvent,
        participant_registry: ParticipantIdentityRegistry,
        *,
        as_of: str,
        view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION,
    ) -> CrossProviderQuoteIdentity:
        if not isinstance(event, MarketEvent):
            raise TypeError("event must be MarketEvent")
        if not isinstance(participant_registry, ParticipantIdentityRegistry):
            raise TypeError("participant_registry must be ParticipantIdentityRegistry")
        if not isinstance(view, IdentityView):
            raise TypeError("view must be IdentityView")
        try:
            event = MarketEvent.from_dict(event.to_dict())
        except (TypeError, ValueError) as exc:
            raise CrossProviderQuoteIdentityError("event must be canonical MarketEvent") from exc
        cutoff = _time("as_of", as_of)
        event_time = _time("event identity time", event.source_ts or event.observed_ts)
        source_key = event.source_id, event.event_id, event.market_id, event.selection_id
        matches = [
            r for r in self._records
            if r.source_key == source_key and _contains(r.valid_from, r.valid_until, event_time)
            and r.sport == event.sport and r.source_competition_id == event.competition_id
            and r.source_market_semantics_id == event.market_semantics_id
            and (view is IdentityView.RESTATED_RESEARCH or (
                _time("available_at", r.available_at) <= cutoff and _time("recorded_at", r.recorded_at) <= cutoff
            ))
        ]
        superseded = {r.supersedes_record_id for r in matches if r.supersedes_record_id is not None}
        tips = [r for r in matches if r.record_id not in superseded]
        if not tips:
            raise CrossProviderQuoteIdentityError("no causally available semantic mapping")
        if len({r.semantic_key for r in tips}) != 1:
            raise CrossProviderQuoteIdentityError("semantic mapping is ambiguous")
        record = max(tips, key=lambda r: (_time("available_at", r.available_at), _time("recorded_at", r.recorded_at), r.record_id))
        try:
            roster = participant_registry.roster_at(event.event_id, event.source_id, as_of=_utc("as_of", as_of), view=view)
        except ParticipantIdentityError as exc:
            raise CrossProviderQuoteIdentityError("canonical participant roster cannot be resolved") from exc
        if not roster or any(entity.kind not in (EntityKind.PARTICIPANT, EntityKind.TEAM) for entity in roster):
            raise CrossProviderQuoteIdentityError("canonical participant roster is absent or invalid")
        participant_ids = tuple(sorted(entity.entity_id for entity in roster))
        if participant_ids != record.participant_entity_ids:
            raise CrossProviderQuoteIdentityError("mapping roster does not match canonical participant authority")
        return CrossProviderQuoteIdentity(
            source_id=event.source_id, source_event_id=event.event_id,
            source_market_id=event.market_id, source_selection_id=event.selection_id,
            sport=record.sport, canonical_event_id=record.canonical_event_id,
            canonical_market_id=record.canonical_market_id, canonical_selection_id=record.canonical_selection_id,
            canonical_competition_id=record.canonical_competition_id,
            canonical_market_semantics_id=record.canonical_market_semantics_id,
            participant_entity_ids=record.participant_entity_ids,
            selection_entity_id=record.selection_entity_id, mapping_record_id=record.record_id,
            mapping_evidence_sha256=record.evidence_sha256, market_event_sha256=_digest(event.to_dict()),
        )

    def _load(self) -> None:
        try:
            payload = _strict_json(self.path.read_text(encoding="utf-8"))
        except UnicodeDecodeError as exc:
            raise CrossProviderQuoteIdentityError("registry must be UTF-8 JSON") from exc
        if type(payload) is not dict or set(payload) != {"schema", "schema_version", "mappings"}:
            raise CrossProviderQuoteIdentityError("registry schema is invalid")
        if payload["schema"] != _SCHEMA or type(payload["schema_version"]) is not int or payload["schema_version"] != _VERSION:
            raise CrossProviderQuoteIdentityError("registry version is unsupported")
        if type(payload["mappings"]) is not list:
            raise CrossProviderQuoteIdentityError("mappings must be a JSON array")
        parsed = [QuoteIdentityMappingRecord.from_payload(item) for item in payload["mappings"]]
        parsed.sort(key=lambda r: (_time("recorded_at", r.recorded_at), r.record_id))
        loaded: list[QuoteIdentityMappingRecord] = []
        seen: set[str] = set()
        for record in parsed:
            if record.record_id in seen:
                raise CrossProviderQuoteIdentityError("registry contains duplicate mapping record")
            seen.add(record.record_id)
            loaded = self._append(loaded, record)
        self._records = loaded
