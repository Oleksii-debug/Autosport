"""Causal, restart-safe participant identity and alias lineage.

This is deliberately an identity authority only.  It does not score participants,
infer behaviour, replace provider event identity, or grant strategy/execution power.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from .integrity import atomic_write_json


_SCHEMA = "autosport.participant_identity"
_VERSION = 1


class EntityKind(StrEnum):
    PARTICIPANT = "PARTICIPANT"
    TEAM = "TEAM"
    LEAGUE = "LEAGUE"


class IdentityView(StrEnum):
    AS_KNOWN_AT_DECISION = "AS_KNOWN_AT_DECISION"
    RESTATED_RESEARCH = "RESTATED_RESEARCH"


class LineageRelation(StrEnum):
    """A correction relation between stable entity identities.

    The relation records provenance; it never silently rewrites an historical
    alias resolution.  Consumers must request and apply a restatement
    explicitly when that is scientifically appropriate.
    """

    MERGED_FROM = "MERGED_FROM"
    SPLIT_FROM = "SPLIT_FROM"
    SUPERSEDES = "SUPERSEDES"


class ParticipantIdentityError(ValueError):
    """Raised when causal identity evidence is malformed or ambiguous."""


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ParticipantIdentityError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _instant(name: str, value: object) -> datetime:
    text = _text(name, value)
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ParticipantIdentityError(f"{name} must be ISO-8601") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ParticipantIdentityError(f"{name} must include a timezone")
    return result.astimezone(timezone.utc)


def _time_text(name: str, value: object) -> str:
    return _instant(name, value).isoformat().replace("+00:00", "Z")


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EntityIdentity:
    entity_id: str
    kind: EntityKind
    source_reference: str
    evidence_sha256: str
    first_known_at: str
    available_at: str

    def __post_init__(self) -> None:
        _text("entity_id", self.entity_id)
        if not isinstance(self.kind, EntityKind):
            raise ParticipantIdentityError("kind must be EntityKind")
        _text("source_reference", self.source_reference)
        if len(_text("evidence_sha256", self.evidence_sha256)) != 64:
            raise ParticipantIdentityError("evidence_sha256 must be SHA-256 hex")
        if any(ch not in "0123456789abcdef" for ch in self.evidence_sha256):
            raise ParticipantIdentityError("evidence_sha256 must be SHA-256 hex")
        if _instant("available_at", self.available_at) < _instant("first_known_at", self.first_known_at):
            raise ParticipantIdentityError("identity cannot be available before first_known_at")

    def payload(self) -> dict[str, str]:
        return {"entity_id": self.entity_id, "kind": self.kind.value, "source_reference": self.source_reference,
                "evidence_sha256": self.evidence_sha256, "first_known_at": _time_text("first_known_at", self.first_known_at),
                "available_at": _time_text("available_at", self.available_at)}


@dataclass(frozen=True, slots=True)
class AliasRecord:
    source_id: str
    alias: str
    entity_id: str
    valid_from: str
    valid_until: str | None
    available_at: str
    evidence_sha256: str
    recorded_at: str
    relation: str = "CONFIRMED"
    supersedes_record_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("source_id", "alias", "entity_id", "evidence_sha256", "relation"):
            _text(name, getattr(self, name))
        if len(self.evidence_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in self.evidence_sha256):
            raise ParticipantIdentityError("alias evidence_sha256 must be SHA-256 hex")
        recorded = _instant("recorded_at", self.recorded_at)
        if recorded < _instant("available_at", self.available_at):
            raise ParticipantIdentityError("alias cannot be recorded before available_at")
        if self.supersedes_record_id is not None:
            supersedes = _text("supersedes_record_id", self.supersedes_record_id)
            if len(supersedes) != 64 or any(ch not in "0123456789abcdef" for ch in supersedes):
                raise ParticipantIdentityError("supersedes_record_id must be SHA-256 hex")
        start = _instant("valid_from", self.valid_from)
        if self.valid_until is not None and _instant("valid_until", self.valid_until) <= start:
            raise ParticipantIdentityError("valid_until must be after valid_from")
        if _instant("available_at", self.available_at) < start:
            raise ParticipantIdentityError("alias cannot be available before valid_from")

    @property
    def record_id(self) -> str:
        return _digest(self.payload())

    def payload(self) -> dict[str, str | None]:
        return {"source_id": self.source_id, "alias": self.alias, "entity_id": self.entity_id,
                "valid_from": _time_text("valid_from", self.valid_from),
                "valid_until": None if self.valid_until is None else _time_text("valid_until", self.valid_until),
                "available_at": _time_text("available_at", self.available_at), "evidence_sha256": self.evidence_sha256,
                "recorded_at": _time_text("recorded_at", self.recorded_at),
                "relation": self.relation, "supersedes_record_id": self.supersedes_record_id}


@dataclass(frozen=True, slots=True)
class RosterMembership:
    event_id: str
    source_id: str
    entity_id: str
    member_from: str
    member_until: str | None
    available_at: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        for name in ("event_id", "source_id", "entity_id", "evidence_sha256"):
            _text(name, getattr(self, name))
        if len(self.evidence_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in self.evidence_sha256):
            raise ParticipantIdentityError("roster evidence_sha256 must be SHA-256 hex")
        start = _instant("member_from", self.member_from)
        if self.member_until is not None and _instant("member_until", self.member_until) <= start:
            raise ParticipantIdentityError("member_until must be after member_from")
        if _instant("available_at", self.available_at) < start:
            raise ParticipantIdentityError("roster membership cannot be available before member_from")

    def payload(self) -> dict[str, str | None]:
        return {"event_id": self.event_id, "source_id": self.source_id, "entity_id": self.entity_id,
                "member_from": _time_text("member_from", self.member_from),
                "member_until": None if self.member_until is None else _time_text("member_until", self.member_until),
                "available_at": _time_text("available_at", self.available_at), "evidence_sha256": self.evidence_sha256}


@dataclass(frozen=True, slots=True)
class EntityLineage:
    """Causal merge, split, or supersession evidence between identities."""

    predecessor_entity_id: str
    successor_entity_id: str
    relation: LineageRelation
    effective_from: str
    available_at: str
    recorded_at: str
    evidence_sha256: str
    valid_until: str | None = None

    def __post_init__(self) -> None:
        for name in ("predecessor_entity_id", "successor_entity_id", "evidence_sha256"):
            _text(name, getattr(self, name))
        if self.predecessor_entity_id == self.successor_entity_id:
            raise ParticipantIdentityError("lineage must join distinct identities")
        if not isinstance(self.relation, LineageRelation):
            raise ParticipantIdentityError("relation must be LineageRelation")
        if len(self.evidence_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in self.evidence_sha256):
            raise ParticipantIdentityError("lineage evidence_sha256 must be SHA-256 hex")
        effective = _instant("effective_from", self.effective_from)
        available = _instant("available_at", self.available_at)
        recorded = _instant("recorded_at", self.recorded_at)
        if self.valid_until is not None and _instant("valid_until", self.valid_until) <= effective:
            raise ParticipantIdentityError("lineage valid_until must be after effective_from")
        if available < effective:
            raise ParticipantIdentityError("lineage cannot be available before effective_from")
        if recorded < available:
            raise ParticipantIdentityError("lineage cannot be recorded before available_at")

    @property
    def record_id(self) -> str:
        return _digest(self.payload())

    def payload(self) -> dict[str, str | None]:
        return {
            "predecessor_entity_id": self.predecessor_entity_id,
            "successor_entity_id": self.successor_entity_id,
            "relation": self.relation.value,
            "effective_from": _time_text("effective_from", self.effective_from),
            "available_at": _time_text("available_at", self.available_at),
            "recorded_at": _time_text("recorded_at", self.recorded_at),
            "evidence_sha256": self.evidence_sha256,
            "valid_until": None if self.valid_until is None else _time_text("valid_until", self.valid_until),
        }


class ParticipantIdentityRegistry:
    """Append-only identity evidence with causal and restated views."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._entities: dict[str, EntityIdentity] = {}
        self._aliases: list[AliasRecord] = []
        self._rosters: list[RosterMembership] = []
        self._lineages: list[EntityLineage] = []
        self._loading = False
        if self.path.exists():
            self._load()

    @classmethod
    def initialize_pristine(cls, path: Path) -> "ParticipantIdentityRegistry":
        registry = cls(path)
        if registry.path.exists():
            raise ParticipantIdentityError("identity registry already exists")
        registry._persist()
        return registry

    def add_entity(self, entity: EntityIdentity) -> None:
        if not isinstance(entity, EntityIdentity):
            raise TypeError("entity must be EntityIdentity")
        existing = self._entities.get(entity.entity_id)
        if existing is not None:
            if existing != entity:
                raise ParticipantIdentityError("conflicting immutable entity identity")
            return
        candidate_entities = dict(self._entities)
        candidate_entities[entity.entity_id] = entity
        self._persist_state(entities=candidate_entities)
        self._entities = candidate_entities

    def add_alias(self, alias: AliasRecord) -> None:
        if not isinstance(alias, AliasRecord):
            raise TypeError("alias must be AliasRecord")
        if alias.entity_id not in self._entities:
            raise ParticipantIdentityError("alias references unknown entity")
        if alias in self._aliases:
            return

        records_by_id = {record.record_id: record for record in self._aliases}
        correction_ancestors: set[str] = set()
        if alias.supersedes_record_id is not None:
            target = records_by_id.get(alias.supersedes_record_id)
            if target is None:
                raise ParticipantIdentityError("alias correction references unknown record")
            if target.source_id != alias.source_id or target.alias != alias.alias:
                raise ParticipantIdentityError("alias correction must preserve source and alias")
            if target.entity_id == alias.entity_id:
                raise ParticipantIdentityError("alias correction must change entity")
            if not _overlap(target.valid_from, target.valid_until, alias.valid_from, alias.valid_until):
                raise ParticipantIdentityError("alias correction must overlap superseded interval")
            if _instant("available_at", alias.available_at) <= _instant("available_at", target.available_at):
                raise ParticipantIdentityError("alias correction must become available after superseded record")
            if _instant("recorded_at", alias.recorded_at) <= _instant("recorded_at", target.recorded_at):
                raise ParticipantIdentityError("alias correction must be recorded after superseded record")
            if any(record.supersedes_record_id == target.record_id for record in self._aliases):
                raise ParticipantIdentityError("alias correction fork is not allowed")

            cursor: AliasRecord | None = target
            while cursor is not None:
                if cursor.record_id in correction_ancestors:
                    raise ParticipantIdentityError("alias correction cycle is not allowed")
                correction_ancestors.add(cursor.record_id)
                if cursor.supersedes_record_id is None:
                    cursor = None
                else:
                    cursor = records_by_id.get(cursor.supersedes_record_id)
                    if cursor is None:
                        raise ParticipantIdentityError("alias correction ancestry is incomplete")

        for existing in self._aliases:
            if existing.source_id != alias.source_id or existing.alias != alias.alias or existing.entity_id == alias.entity_id:
                continue
            if not _overlap(existing.valid_from, existing.valid_until, alias.valid_from, alias.valid_until):
                continue
            if existing.record_id in correction_ancestors:
                continue
            raise ParticipantIdentityError("conflicting alias validity intervals")

        candidate_aliases = [*self._aliases, alias]
        candidate_aliases.sort(key=lambda value: (
            value.source_id,
            value.alias,
            _time_text("recorded_at", value.recorded_at),
            _time_text("available_at", value.available_at),
            _time_text("valid_from", value.valid_from),
            value.record_id,
        ))
        self._persist_state(aliases=candidate_aliases)
        self._aliases = candidate_aliases

    def add_roster_membership(self, membership: RosterMembership) -> None:
        if not isinstance(membership, RosterMembership):
            raise TypeError("membership must be RosterMembership")
        if membership.entity_id not in self._entities:
            raise ParticipantIdentityError("roster membership references unknown entity")
        if membership in self._rosters:
            return
        candidate_rosters = [*self._rosters, membership]
        candidate_rosters.sort(key=lambda value: (value.event_id, value.source_id, value.entity_id, _time_text("member_from", value.member_from)))
        self._persist_state(rosters=candidate_rosters)
        self._rosters = candidate_rosters

    def add_lineage(self, lineage: EntityLineage) -> None:
        """Append correction provenance without changing prior resolutions."""
        if not isinstance(lineage, EntityLineage):
            raise TypeError("lineage must be EntityLineage")
        if lineage.predecessor_entity_id not in self._entities or lineage.successor_entity_id not in self._entities:
            raise ParticipantIdentityError("lineage references unknown entity")
        predecessor = self._entities[lineage.predecessor_entity_id]
        successor = self._entities[lineage.successor_entity_id]
        if predecessor.kind is not successor.kind:
            raise ParticipantIdentityError("lineage identities must have the same EntityKind")
        if _instant("available_at", lineage.available_at) < max(
            _instant("predecessor available_at", predecessor.available_at),
            _instant("successor available_at", successor.available_at),
        ):
            raise ParticipantIdentityError("lineage cannot be available before referenced entity identity")
        if lineage in self._lineages:
            return
        for existing in self._lineages:
            if existing.predecessor_entity_id != lineage.predecessor_entity_id:
                continue
            if not _overlap(existing.effective_from, existing.valid_until, lineage.effective_from, lineage.valid_until):
                continue
            if (
                existing.relation is LineageRelation.SPLIT_FROM
                and lineage.relation is LineageRelation.SPLIT_FROM
                and existing.successor_entity_id != lineage.successor_entity_id
            ):
                continue
            raise ParticipantIdentityError("conflicting lineage validity intervals")
        candidate_lineages = [*self._lineages, lineage]
        candidate_lineages.sort(key=lambda value: (
            value.predecessor_entity_id, value.successor_entity_id,
            _time_text("effective_from", value.effective_from), value.record_id,
        ))
        self._persist_state(lineages=candidate_lineages)
        self._lineages = candidate_lineages

    def lineage_at(self, entity_id: str, *, as_of: str, view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION) -> tuple[EntityLineage, ...]:
        """Return causal correction evidence; do not rewrite identity truth."""
        if not isinstance(view, IdentityView):
            raise TypeError("view must be IdentityView")
        _text("entity_id", entity_id)
        moment = _instant("as_of", as_of)
        return tuple(
            record for record in self._lineages
            if entity_id in (record.predecessor_entity_id, record.successor_entity_id)
            and _contains(record.effective_from, record.valid_until, moment)
            and (
                view is IdentityView.RESTATED_RESEARCH
                or (
                    _instant("available_at", record.available_at) <= moment
                    and _instant("recorded_at", record.recorded_at) <= moment
                    and _instant(
                        "predecessor available_at",
                        self._entities[record.predecessor_entity_id].available_at,
                    ) <= moment
                    and _instant(
                        "successor available_at",
                        self._entities[record.successor_entity_id].available_at,
                    ) <= moment
                )
            )
        )

    def resolve_alias_record(self, source_id: str, alias: str, *, as_of: str, view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION) -> AliasRecord:
        if not isinstance(view, IdentityView):
            raise TypeError("view must be IdentityView")
        moment = _instant("as_of", as_of)
        canonical_source = _text("source_id", source_id)
        canonical_alias = _text("alias", alias)
        matches = []
        for record in self._aliases:
            if record.source_id != canonical_source or record.alias != canonical_alias:
                continue
            if not _contains(record.valid_from, record.valid_until, moment):
                continue
            if view is IdentityView.AS_KNOWN_AT_DECISION and (
                _instant("available_at", record.available_at) > moment
                or _instant("recorded_at", record.recorded_at) > moment
            ):
                continue
            matches.append(record)

        superseded_ids = {
            record.supersedes_record_id
            for record in matches
            if record.supersedes_record_id is not None
        }
        tips = [record for record in matches if record.record_id not in superseded_ids]
        if not tips or len({record.entity_id for record in tips}) != 1:
            raise ParticipantIdentityError("alias cannot be resolved unambiguously at requested causal view")
        return max(
            tips,
            key=lambda record: (_instant("available_at", record.available_at), record.record_id),
        )

    def resolve_alias(self, source_id: str, alias: str, *, as_of: str, view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION) -> EntityIdentity:
        moment = _instant("as_of", as_of)
        record = self.resolve_alias_record(source_id, alias, as_of=as_of, view=view)
        entity = self._entities[record.entity_id]
        if view is IdentityView.AS_KNOWN_AT_DECISION and _instant("available_at", entity.available_at) > moment:
            raise ParticipantIdentityError("entity was not known at requested causal view")
        return entity

    def roster_at(self, event_id: str, source_id: str, *, as_of: str, view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION) -> tuple[EntityIdentity, ...]:
        moment = _instant("as_of", as_of)
        if not isinstance(view, IdentityView):
            raise TypeError("view must be IdentityView")
        result = []
        for membership in self._rosters:
            if membership.event_id == _text("event_id", event_id) and membership.source_id == _text("source_id", source_id) and _contains(membership.member_from, membership.member_until, moment):
                if view is IdentityView.RESTATED_RESEARCH or _instant("available_at", membership.available_at) <= moment:
                    entity = self._entities[membership.entity_id]
                    if view is IdentityView.AS_KNOWN_AT_DECISION and _instant("available_at", entity.available_at) > moment:
                        raise ParticipantIdentityError("roster references entity not known at requested causal view")
                    result.append(entity)
        return tuple(sorted(result, key=lambda entity: entity.entity_id))

    def _persist_state(
        self,
        *,
        entities: dict[str, EntityIdentity] | None = None,
        aliases: list[AliasRecord] | None = None,
        rosters: list[RosterMembership] | None = None,
        lineages: list[EntityLineage] | None = None,
    ) -> None:
        if self._loading:
            return
        entity_state = self._entities if entities is None else entities
        alias_state = self._aliases if aliases is None else aliases
        roster_state = self._rosters if rosters is None else rosters
        lineage_state = self._lineages if lineages is None else lineages
        atomic_write_json(self.path, {"schema": _SCHEMA, "version": _VERSION,
            "entities": [item.payload() for item in sorted(entity_state.values(), key=lambda item: item.entity_id)],
            "aliases": [item.payload() for item in alias_state],
            "rosters": [item.payload() for item in roster_state],
            "lineages": [item.payload() for item in lineage_state]})

    def _persist(self) -> None:
        self._persist_state()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ParticipantIdentityError(f"cannot load identity registry: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA or raw.get("version") != _VERSION:
            raise ParticipantIdentityError("unsupported identity registry schema")
        self._loading = True
        try:
            for item in raw.get("entities", []):
                entity = EntityIdentity(item["entity_id"], EntityKind(item["kind"]), item["source_reference"], item["evidence_sha256"], item["first_known_at"], item["available_at"])
                if entity.entity_id in self._entities:
                    raise ParticipantIdentityError("duplicate entity identity")
                self._entities[entity.entity_id] = entity
            for item in raw.get("aliases", []):
                self.add_alias(AliasRecord(**item))
            for item in raw.get("rosters", []):
                self.add_roster_membership(RosterMembership(**item))
            for item in raw.get("lineages", []):
                self.add_lineage(EntityLineage(
                    item["predecessor_entity_id"], item["successor_entity_id"],
                    LineageRelation(item["relation"]), item["effective_from"],
                    item["available_at"], item["recorded_at"], item["evidence_sha256"],
                    item.get("valid_until"),
                ))
        finally:
            self._loading = False


def _contains(start: str, end: str | None, moment: datetime) -> bool:
    return _instant("valid_from", start) <= moment and (end is None or moment < _instant("valid_until", end))


def _overlap(a_start: str, a_end: str | None, b_start: str, b_end: str | None) -> bool:
    a0, b0 = _instant("valid_from", a_start), _instant("valid_from", b_start)
    a1 = None if a_end is None else _instant("valid_until", a_end)
    b1 = None if b_end is None else _instant("valid_until", b_end)
    return (b1 is None or a0 < b1) and (a1 is None or b0 < a1)
