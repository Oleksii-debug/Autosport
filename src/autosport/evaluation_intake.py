from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .workspace_lock import WorkspaceEconomicLock


SCHEMA = "autosport.evaluation_intake"
SCHEMA_VERSION = 2
_HEX = frozenset("0123456789abcdef")


class EvaluationIntakeError(RuntimeError):
    """Canonical pre-result intake authority rejected incomplete/conflicting evidence."""


class EvaluationIntakeIntegrityError(EvaluationIntakeError):
    """Durable intake history is malformed, tampered, or non-contiguous."""


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise EvaluationIntakeError(f"{field} must be non-empty canonical text")
    value.encode("utf-8")
    return value


def _instant(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationIntakeError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvaluationIntakeError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: object, field: str) -> str:
    return _instant(value, field).isoformat().replace("+00:00", "Z")


def _sha(value: object, field: str) -> str:
    raw = _text(value, field).lower()
    if len(raw) != 64 or any(ch not in _HEX for ch in raw):
        raise EvaluationIntakeError(f"{field} must be canonical SHA-256 hex")
    return raw


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _keys(values: object) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not values:
        raise EvaluationIntakeError("row_keys must be a non-empty tuple")
    items = tuple(_text(item, "row_key") for item in values)
    if items != tuple(sorted(items)) or len(items) != len(set(items)):
        raise EvaluationIntakeError("row_keys must be sorted and unique")
    return items


def _row_evidence(
    values: object,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(values, tuple) or not values:
        raise EvaluationIntakeError(
            "row_evidence_sha256 must be a non-empty tuple"
        )
    items: list[tuple[str, str]] = []
    for item in values:
        if not isinstance(item, tuple) or len(item) != 2:
            raise EvaluationIntakeError(
                "row_evidence_sha256 entries must be (row_key, sha256) tuples"
            )
        row_key = _text(item[0], "row_evidence row_key")
        row_sha256 = _sha(item[1], "row_evidence sha256")
        items.append((row_key, row_sha256))
    result = tuple(items)
    if result != tuple(sorted(result)) or len({key for key, _ in result}) != len(result):
        raise EvaluationIntakeError(
            "row_evidence_sha256 must use sorted unique row keys"
        )
    return result


@dataclass(frozen=True, slots=True)
class ObservationEnumerationWitness:
    """Upstream acquisition proof for one exhaustive, gap-free observation range."""

    enumeration_id: str
    session_id: str
    source_id: str
    campaign_id: str
    research_protocol_id: str
    protocol_sha256: str
    universe_id: str
    cycle_index: int
    source_range_id: str
    stream_epoch: str
    start_cursor: str
    end_cursor: str
    acquisition_sha256: str
    row_keys: tuple[str, ...]
    row_evidence_sha256: tuple[tuple[str, str], ...]
    exhaustive: bool
    gap_free: bool
    committed_at: str
    evaluation_not_before: str
    outcome_reveal_not_before: str

    def __post_init__(self) -> None:
        for field in (
            "enumeration_id",
            "session_id",
            "source_id",
            "campaign_id",
            "research_protocol_id",
            "universe_id",
            "source_range_id",
            "stream_epoch",
            "start_cursor",
            "end_cursor",
        ):
            _text(getattr(self, field), field)
        if self.start_cursor == self.end_cursor:
            raise EvaluationIntakeError("enumeration cursor range must advance")
        object.__setattr__(
            self,
            "protocol_sha256",
            _sha(self.protocol_sha256, "protocol_sha256"),
        )
        object.__setattr__(
            self,
            "acquisition_sha256",
            _sha(self.acquisition_sha256, "acquisition_sha256"),
        )
        object.__setattr__(self, "row_keys", _keys(self.row_keys))
        object.__setattr__(
            self,
            "row_evidence_sha256",
            _row_evidence(self.row_evidence_sha256),
        )
        if tuple(key for key, _ in self.row_evidence_sha256) != self.row_keys:
            raise EvaluationIntakeError(
                "row_evidence_sha256 must bind every authoritative row_key exactly once"
            )
        if type(self.cycle_index) is not int or self.cycle_index <= 0:
            raise EvaluationIntakeError("cycle_index must be a positive integer")
        if type(self.exhaustive) is not bool or type(self.gap_free) is not bool:
            raise EvaluationIntakeError("exhaustive/gap_free must be booleans")
        committed = _instant(self.committed_at, "committed_at")
        evaluation = _instant(self.evaluation_not_before, "evaluation_not_before")
        reveal = _instant(self.outcome_reveal_not_before, "outcome_reveal_not_before")
        if evaluation < committed:
            raise EvaluationIntakeError(
                "evaluation_not_before cannot precede enumeration commit"
            )
        if reveal <= evaluation:
            raise EvaluationIntakeError(
                "outcome reveal must be strictly after evaluation boundary"
            )

    @property
    def witness_sha256(self) -> str:
        return _digest(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "enumeration_id": self.enumeration_id,
            "session_id": self.session_id,
            "source_id": self.source_id,
            "campaign_id": self.campaign_id,
            "research_protocol_id": self.research_protocol_id,
            "protocol_sha256": self.protocol_sha256,
            "universe_id": self.universe_id,
            "cycle_index": self.cycle_index,
            "source_range_id": self.source_range_id,
            "stream_epoch": self.stream_epoch,
            "start_cursor": self.start_cursor,
            "end_cursor": self.end_cursor,
            "acquisition_sha256": self.acquisition_sha256,
            "row_keys": list(self.row_keys),
            "row_evidence_sha256": [
                {"row_key": key, "sha256": sha256}
                for key, sha256 in self.row_evidence_sha256
            ],
            "exhaustive": self.exhaustive,
            "gap_free": self.gap_free,
            "committed_at": _timestamp(self.committed_at, "committed_at"),
            "evaluation_not_before": _timestamp(
                self.evaluation_not_before, "evaluation_not_before"
            ),
            "outcome_reveal_not_before": _timestamp(
                self.outcome_reveal_not_before, "outcome_reveal_not_before"
            ),
        }


@runtime_checkable
class ObservationEnumerationResolver(Protocol):
    """Upstream authority for immutable ranges and the terminal complete range."""

    def resolve_enumeration(self, enumeration_id: str) -> ObservationEnumerationWitness:
        ...

    def terminal_enumeration_id(
        self,
        *,
        session_id: str,
        source_id: str,
        campaign_id: str,
        research_protocol_id: str,
        protocol_sha256: str,
        universe_id: str,
    ) -> str:
        ...


@dataclass(frozen=True, slots=True)
class ObservationIntakeRecord:
    """One pre-result observation cycle derived from upstream enumeration evidence."""

    authority_id: str
    session_id: str
    source_id: str
    campaign_id: str
    research_protocol_id: str
    protocol_sha256: str
    universe_id: str
    cycle_index: int
    committed_at: str
    evaluation_not_before: str
    outcome_reveal_not_before: str
    row_keys: tuple[str, ...]
    source_state: str
    enumeration_id: str
    enumeration_witness_sha256: str
    previous_record_sha256: str | None

    def __post_init__(self) -> None:
        for field in (
            "authority_id",
            "session_id",
            "source_id",
            "campaign_id",
            "research_protocol_id",
            "universe_id",
            "source_state",
            "enumeration_id",
        ):
            _text(getattr(self, field), field)
        object.__setattr__(
            self,
            "protocol_sha256",
            _sha(self.protocol_sha256, "protocol_sha256"),
        )
        object.__setattr__(
            self,
            "enumeration_witness_sha256",
            _sha(self.enumeration_witness_sha256, "enumeration_witness_sha256"),
        )
        if type(self.cycle_index) is not int or self.cycle_index <= 0:
            raise EvaluationIntakeError("cycle_index must be a positive integer")
        committed = _instant(self.committed_at, "committed_at")
        evaluation = _instant(self.evaluation_not_before, "evaluation_not_before")
        reveal = _instant(self.outcome_reveal_not_before, "outcome_reveal_not_before")
        if evaluation < committed:
            raise EvaluationIntakeError("evaluation boundary precedes intake commit")
        if reveal <= evaluation:
            raise EvaluationIntakeError("outcome reveal must follow evaluation boundary")
        object.__setattr__(self, "row_keys", _keys(self.row_keys))
        if self.source_state != "EXHAUSTIVE_GAP_FREE":
            raise EvaluationIntakeError("intake record must be exhaustive and gap-free")
        if self.previous_record_sha256 is not None:
            object.__setattr__(
                self,
                "previous_record_sha256",
                _sha(self.previous_record_sha256, "previous_record_sha256"),
            )

    @property
    def identity(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.session_id,
            self.source_id,
            self.campaign_id,
            self.research_protocol_id,
            self.protocol_sha256,
            self.universe_id,
        )

    @property
    def record_sha256(self) -> str:
        return _digest(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "authority_id": self.authority_id,
            "session_id": self.session_id,
            "source_id": self.source_id,
            "campaign_id": self.campaign_id,
            "research_protocol_id": self.research_protocol_id,
            "protocol_sha256": self.protocol_sha256,
            "universe_id": self.universe_id,
            "cycle_index": self.cycle_index,
            "committed_at": _timestamp(self.committed_at, "committed_at"),
            "evaluation_not_before": _timestamp(
                self.evaluation_not_before, "evaluation_not_before"
            ),
            "outcome_reveal_not_before": _timestamp(
                self.outcome_reveal_not_before, "outcome_reveal_not_before"
            ),
            "row_keys": list(self.row_keys),
            "source_state": self.source_state,
            "enumeration_id": self.enumeration_id,
            "enumeration_witness_sha256": self.enumeration_witness_sha256,
            "previous_record_sha256": self.previous_record_sha256,
        }

    @classmethod
    def from_payload(cls, raw: object) -> "ObservationIntakeRecord":
        if type(raw) is not dict:
            raise EvaluationIntakeIntegrityError("intake record must be an object")
        expected = {
            "authority_id",
            "session_id",
            "source_id",
            "campaign_id",
            "research_protocol_id",
            "protocol_sha256",
            "universe_id",
            "cycle_index",
            "committed_at",
            "evaluation_not_before",
            "outcome_reveal_not_before",
            "row_keys",
            "source_state",
            "enumeration_id",
            "enumeration_witness_sha256",
            "previous_record_sha256",
            "record_sha256",
        }
        if set(raw) != expected:
            raise EvaluationIntakeIntegrityError("intake record schema mismatch")
        values = dict(raw)
        claimed = values.pop("record_sha256")
        try:
            values["row_keys"] = tuple(values["row_keys"])
            record = cls(**values)
        except (TypeError, ValueError) as exc:
            if isinstance(exc, EvaluationIntakeError):
                raise
            raise EvaluationIntakeIntegrityError("invalid intake record") from exc
        if claimed != record.record_sha256:
            raise EvaluationIntakeIntegrityError("intake record digest mismatch")
        return record


@dataclass(frozen=True, slots=True)
class ObservationIntakeSnapshot:
    """Immutable membership recomputed from canonical upstream-authorized intake."""

    authority_id: str
    session_id: str
    source_id: str
    campaign_id: str
    research_protocol_id: str
    protocol_sha256: str
    universe_id: str
    first_cycle: int
    last_cycle: int
    record_count: int
    root_sha256: str
    expected_row_keys: tuple[str, ...]
    committed_at: str
    evaluation_not_before: str

    def __post_init__(self) -> None:
        for field in (
            "authority_id",
            "session_id",
            "source_id",
            "campaign_id",
            "research_protocol_id",
            "universe_id",
        ):
            _text(getattr(self, field), field)
        object.__setattr__(
            self,
            "protocol_sha256",
            _sha(self.protocol_sha256, "protocol_sha256"),
        )
        if type(self.first_cycle) is not int or type(self.last_cycle) is not int:
            raise EvaluationIntakeError("snapshot cycle bounds must be integers")
        if self.first_cycle <= 0 or self.last_cycle < self.first_cycle:
            raise EvaluationIntakeError("snapshot cycle bounds are invalid")
        if self.record_count != self.last_cycle - self.first_cycle + 1:
            raise EvaluationIntakeError("snapshot record_count must cover every cycle")
        object.__setattr__(self, "root_sha256", _sha(self.root_sha256, "root_sha256"))
        object.__setattr__(self, "expected_row_keys", _keys(self.expected_row_keys))
        committed = _instant(self.committed_at, "committed_at")
        evaluation = _instant(self.evaluation_not_before, "evaluation_not_before")
        if evaluation < committed:
            raise EvaluationIntakeError("snapshot evaluation boundary precedes commit")

    @property
    def identity(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.session_id,
            self.source_id,
            self.campaign_id,
            self.research_protocol_id,
            self.protocol_sha256,
            self.universe_id,
        )

    @property
    def snapshot_sha256(self) -> str:
        return _digest(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "authority_id": self.authority_id,
            "session_id": self.session_id,
            "source_id": self.source_id,
            "campaign_id": self.campaign_id,
            "research_protocol_id": self.research_protocol_id,
            "protocol_sha256": self.protocol_sha256,
            "universe_id": self.universe_id,
            "first_cycle": self.first_cycle,
            "last_cycle": self.last_cycle,
            "record_count": self.record_count,
            "root_sha256": self.root_sha256,
            "expected_row_keys": list(self.expected_row_keys),
            "committed_at": _timestamp(self.committed_at, "committed_at"),
            "evaluation_not_before": _timestamp(
                self.evaluation_not_before, "evaluation_not_before"
            ),
        }

    @classmethod
    def from_payload(cls, raw: object) -> "ObservationIntakeSnapshot":
        if type(raw) is not dict:
            raise EvaluationIntakeIntegrityError("intake snapshot must be an object")
        expected = {
            "authority_id",
            "session_id",
            "source_id",
            "campaign_id",
            "research_protocol_id",
            "protocol_sha256",
            "universe_id",
            "first_cycle",
            "last_cycle",
            "record_count",
            "root_sha256",
            "expected_row_keys",
            "committed_at",
            "evaluation_not_before",
        }
        if set(raw) != expected:
            raise EvaluationIntakeIntegrityError("intake snapshot schema mismatch")
        values = dict(raw)
        try:
            values["expected_row_keys"] = tuple(values["expected_row_keys"])
            return cls(**values)
        except (TypeError, ValueError) as exc:
            if isinstance(exc, EvaluationIntakeError):
                raise
            raise EvaluationIntakeIntegrityError("invalid intake snapshot") from exc


class ObservationIntakeLedger:
    """Durable denominator membership derived only from upstream enumeration authority."""

    FILE_NAME = "evaluation-intake.json"

    def __init__(
        self,
        workspace: str | Path,
        *,
        authority_id: str,
        enumeration_resolver: ObservationEnumerationResolver,
    ) -> None:
        if not isinstance(enumeration_resolver, ObservationEnumerationResolver):
            raise TypeError(
                "enumeration_resolver must implement ObservationEnumerationResolver"
            )
        self.workspace = Path(workspace)
        self.path = self.workspace / self.FILE_NAME
        self.authority_id = _text(authority_id, "authority_id")
        self.enumeration_resolver = enumeration_resolver

    def _read_unlocked(self) -> tuple[ObservationIntakeRecord, ...]:
        if not self.path.exists():
            return ()
        try:
            raw = strict_json_loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise EvaluationIntakeIntegrityError("cannot read canonical intake ledger") from exc
        if (
            type(raw) is not dict
            or set(raw)
            != {"schema", "schema_version", "authority_id", "records", "root_sha256"}
        ):
            raise EvaluationIntakeIntegrityError("canonical intake ledger schema mismatch")
        if (
            raw["schema"] != SCHEMA
            or raw["schema_version"] != SCHEMA_VERSION
            or raw["authority_id"] != self.authority_id
        ):
            raise EvaluationIntakeIntegrityError("canonical intake authority identity mismatch")
        if type(raw["records"]) is not list:
            raise EvaluationIntakeIntegrityError("canonical intake records must be a list")
        records = tuple(ObservationIntakeRecord.from_payload(item) for item in raw["records"])
        self._validate_chain(records)
        expected_root = None if not records else records[-1].record_sha256
        if raw["root_sha256"] != expected_root:
            raise EvaluationIntakeIntegrityError("canonical intake root mismatch")
        return records

    def records(self) -> tuple[ObservationIntakeRecord, ...]:
        with WorkspaceEconomicLock(self.workspace):
            records = self._read_unlocked()
        witnesses = tuple(self._verify_enumeration(record) for record in records)
        self._validate_enumeration_sequence(witnesses)
        return records

    def _validate_chain(self, records: tuple[ObservationIntakeRecord, ...]) -> None:
        previous: str | None = None
        identity: tuple[str, str, str, str, str, str] | None = None
        expected_cycle = 1
        seen_rows: set[str] = set()
        seen_enumerations: set[str] = set()
        for record in records:
            if record.authority_id != self.authority_id:
                raise EvaluationIntakeIntegrityError("record belongs to another intake authority")
            if identity is None:
                identity = record.identity
            elif identity != record.identity:
                raise EvaluationIntakeIntegrityError(
                    "intake authority cannot mix session/source/campaign/protocol/universe identity"
                )
            if record.cycle_index != expected_cycle:
                raise EvaluationIntakeIntegrityError("intake cycle history is not contiguous")
            if record.previous_record_sha256 != previous:
                raise EvaluationIntakeIntegrityError("intake record chain predecessor mismatch")
            if seen_rows.intersection(record.row_keys):
                raise EvaluationIntakeIntegrityError("row_key cannot move between intake cycles")
            if record.enumeration_id in seen_enumerations:
                raise EvaluationIntakeIntegrityError("enumeration_id cannot authorize two cycles")
            seen_rows.update(record.row_keys)
            seen_enumerations.add(record.enumeration_id)
            previous = record.record_sha256
            expected_cycle += 1

    def _resolve_enumeration(self, enumeration_id: str) -> ObservationEnumerationWitness:
        enumeration_id = _text(enumeration_id, "enumeration_id")
        try:
            witness = self.enumeration_resolver.resolve_enumeration(enumeration_id)
        except Exception as exc:
            raise EvaluationIntakeIntegrityError(
                "enumeration resolver could not resolve immutable acquisition evidence"
            ) from exc
        if not isinstance(witness, ObservationEnumerationWitness):
            raise EvaluationIntakeIntegrityError(
                "enumeration resolver returned invalid witness type"
            )
        if witness.enumeration_id != enumeration_id:
            raise EvaluationIntakeIntegrityError(
                "enumeration resolver returned a different identity"
            )
        if not witness.exhaustive or not witness.gap_free:
            raise EvaluationIntakeError(
                "evaluation denominator requires exhaustive gap-free acquisition evidence"
            )
        return witness

    def _verify_enumeration(
        self,
        record: ObservationIntakeRecord,
    ) -> ObservationEnumerationWitness:
        witness = self._resolve_enumeration(record.enumeration_id)
        expected = ObservationIntakeRecord(
            authority_id=self.authority_id,
            session_id=witness.session_id,
            source_id=witness.source_id,
            campaign_id=witness.campaign_id,
            research_protocol_id=witness.research_protocol_id,
            protocol_sha256=witness.protocol_sha256,
            universe_id=witness.universe_id,
            cycle_index=witness.cycle_index,
            committed_at=witness.committed_at,
            evaluation_not_before=witness.evaluation_not_before,
            outcome_reveal_not_before=witness.outcome_reveal_not_before,
            row_keys=witness.row_keys,
            source_state="EXHAUSTIVE_GAP_FREE",
            enumeration_id=witness.enumeration_id,
            enumeration_witness_sha256=witness.witness_sha256,
            previous_record_sha256=record.previous_record_sha256,
        )
        if expected.record_sha256 != record.record_sha256:
            raise EvaluationIntakeIntegrityError(
                "durable intake record no longer matches upstream enumeration authority"
            )
        return witness

    def _validate_enumeration_sequence(
        self,
        witnesses: tuple[ObservationEnumerationWitness, ...],
    ) -> None:
        if not witnesses:
            return
        epoch = witnesses[0].stream_epoch
        seen_ranges: set[str] = set()
        previous: ObservationEnumerationWitness | None = None
        for witness in witnesses:
            if witness.stream_epoch != epoch:
                raise EvaluationIntakeIntegrityError(
                    "enumeration sequence cannot cross acquisition stream epochs"
                )
            if witness.source_range_id in seen_ranges:
                raise EvaluationIntakeIntegrityError(
                    "source_range_id cannot authorize multiple intake cycles"
                )
            if previous is not None and witness.start_cursor != previous.end_cursor:
                raise EvaluationIntakeIntegrityError(
                    "enumeration cursor continuity contains a gap or overlap"
                )
            seen_ranges.add(witness.source_range_id)
            previous = witness

    def _terminal_enumeration_id(
        self,
        identity: tuple[str, str, str, str, str, str],
    ) -> str:
        try:
            terminal = self.enumeration_resolver.terminal_enumeration_id(
                session_id=identity[0],
                source_id=identity[1],
                campaign_id=identity[2],
                research_protocol_id=identity[3],
                protocol_sha256=identity[4],
                universe_id=identity[5],
            )
        except Exception as exc:
            raise EvaluationIntakeIntegrityError(
                "enumeration resolver cannot prove the terminal complete acquisition range"
            ) from exc
        return _text(terminal, "terminal_enumeration_id")

    def append_cycle(self, *, enumeration_id: str) -> ObservationIntakeRecord:
        witness = self._resolve_enumeration(enumeration_id)
        with WorkspaceEconomicLock(self.workspace):
            records = self._read_unlocked()
            if witness.cycle_index <= len(records):
                existing = records[witness.cycle_index - 1]
                self._verify_enumeration(existing)
                if existing.enumeration_id == witness.enumeration_id:
                    return existing
                raise EvaluationIntakeIntegrityError(
                    "intake cycle retry conflicts with durable enumeration history"
                )
            expected_cycle = len(records) + 1
            if witness.cycle_index != expected_cycle:
                raise EvaluationIntakeIntegrityError(
                    "intake cycle cannot skip an observation cycle"
                )
            previous = None if not records else records[-1].record_sha256
            if records:
                previous_witness = self._verify_enumeration(records[-1])
                self._validate_enumeration_sequence((previous_witness, witness))
            record = ObservationIntakeRecord(
                authority_id=self.authority_id,
                session_id=witness.session_id,
                source_id=witness.source_id,
                campaign_id=witness.campaign_id,
                research_protocol_id=witness.research_protocol_id,
                protocol_sha256=witness.protocol_sha256,
                universe_id=witness.universe_id,
                cycle_index=witness.cycle_index,
                committed_at=witness.committed_at,
                evaluation_not_before=witness.evaluation_not_before,
                outcome_reveal_not_before=witness.outcome_reveal_not_before,
                row_keys=witness.row_keys,
                source_state="EXHAUSTIVE_GAP_FREE",
                enumeration_id=witness.enumeration_id,
                enumeration_witness_sha256=witness.witness_sha256,
                previous_record_sha256=previous,
            )
            if records and records[0].identity != record.identity:
                raise EvaluationIntakeIntegrityError(
                    "intake authority cannot change session/source/campaign/protocol/universe identity"
                )
            if any(set(record.row_keys).intersection(item.row_keys) for item in records):
                raise EvaluationIntakeIntegrityError(
                    "row_key already belongs to an earlier intake cycle"
                )
            updated = (*records, record)
            payload = {
                "schema": SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "authority_id": self.authority_id,
                "records": [
                    {**item.to_payload(), "record_sha256": item.record_sha256}
                    for item in updated
                ],
                "root_sha256": record.record_sha256,
            }
            atomic_write_json(self.path, payload)
            self._validate_chain(updated)
            self._verify_enumeration(record)
            return record

    def snapshot(self, *, first_cycle: int, last_cycle: int) -> ObservationIntakeSnapshot:
        records = self.records()
        if type(first_cycle) is not int or type(last_cycle) is not int:
            raise EvaluationIntakeError("snapshot cycle bounds must be integers")
        if first_cycle != 1:
            raise EvaluationIntakeError(
                "evaluation intake freeze must begin at cycle 1 to prevent prefix cherry-picking"
            )
        if last_cycle != len(records) or not records:
            raise EvaluationIntakeError(
                "evaluation intake freeze must include the complete durable intake tip"
            )
        selected = records[first_cycle - 1 : last_cycle]
        terminal_id = self._terminal_enumeration_id(selected[0].identity)
        if selected[-1].enumeration_id != terminal_id:
            raise EvaluationIntakeError(
                "evaluation intake tip is not the authoritative terminal acquisition enumeration"
            )
        expected_keys = tuple(sorted(key for record in selected for key in record.row_keys))
        first = selected[0]
        return ObservationIntakeSnapshot(
            authority_id=self.authority_id,
            session_id=first.session_id,
            source_id=first.source_id,
            campaign_id=first.campaign_id,
            research_protocol_id=first.research_protocol_id,
            protocol_sha256=first.protocol_sha256,
            universe_id=first.universe_id,
            first_cycle=first_cycle,
            last_cycle=last_cycle,
            record_count=len(selected),
            root_sha256=selected[-1].record_sha256,
            expected_row_keys=expected_keys,
            committed_at=max(record.committed_at for record in selected),
            evaluation_not_before=max(record.evaluation_not_before for record in selected),
        )

    def verify_snapshot(self, snapshot: ObservationIntakeSnapshot) -> None:
        if not isinstance(snapshot, ObservationIntakeSnapshot):
            raise EvaluationIntakeError("snapshot must be ObservationIntakeSnapshot")
        canonical = self.snapshot(
            first_cycle=snapshot.first_cycle,
            last_cycle=snapshot.last_cycle,
        )
        if canonical != snapshot:
            raise EvaluationIntakeIntegrityError(
                "snapshot does not match canonical upstream-authorized intake"
            )

    def record_for_row(self, row_key: str) -> ObservationIntakeRecord:
        row_key = _text(row_key, "row_key")
        matches = [record for record in self.records() if row_key in record.row_keys]
        if len(matches) != 1:
            raise EvaluationIntakeIntegrityError(
                "row_key must resolve to exactly one canonical intake record"
            )
        return matches[0]

    def row_evidence_sha256(self, row_key: str) -> str:
        row_key = _text(row_key, "row_key")
        record = self.record_for_row(row_key)
        witness = self._verify_enumeration(record)
        matches = [
            sha256
            for key, sha256 in witness.row_evidence_sha256
            if key == row_key
        ]
        if len(matches) != 1:
            raise EvaluationIntakeIntegrityError(
                "row_key must resolve to exactly one immutable upstream row evidence digest"
            )
        return matches[0]
