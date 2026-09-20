from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .workspace_lock import WorkspaceEconomicLock


SCHEMA = "autosport.evaluation_intake"
SCHEMA_VERSION = 1
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


@dataclass(frozen=True, slots=True)
class ObservationIntakeRecord:
    """One pre-result observation cycle and its complete denominator membership."""

    authority_id: str
    session_id: str
    source_id: str
    campaign_id: str
    research_protocol_id: str
    protocol_sha256: str
    universe_id: str
    cycle_index: int
    committed_at: str
    outcome_reveal_not_before: str
    row_keys: tuple[str, ...]
    source_state: str
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
        ):
            _text(getattr(self, field), field)
        object.__setattr__(
            self,
            "protocol_sha256",
            _sha(self.protocol_sha256, "protocol_sha256"),
        )
        if type(self.cycle_index) is not int or self.cycle_index <= 0:
            raise EvaluationIntakeError("cycle_index must be a positive integer")
        committed = _instant(self.committed_at, "committed_at")
        reveal = _instant(self.outcome_reveal_not_before, "outcome_reveal_not_before")
        if reveal <= committed:
            raise EvaluationIntakeError(
                "intake membership must be committed before outcome reveal"
            )
        object.__setattr__(self, "row_keys", _keys(self.row_keys))
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
            "outcome_reveal_not_before": _timestamp(
                self.outcome_reveal_not_before,
                "outcome_reveal_not_before",
            ),
            "row_keys": list(self.row_keys),
            "source_state": self.source_state,
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
            "outcome_reveal_not_before",
            "row_keys",
            "source_state",
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
    """Independently recomputable immutable membership from canonical pre-result intake."""

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
        _instant(self.committed_at, "committed_at")

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
    """Durable pre-result membership authority; freeze recomputes membership from history."""

    FILE_NAME = "evaluation-intake.json"

    def __init__(self, workspace: str | Path, *, authority_id: str) -> None:
        self.workspace = Path(workspace)
        self.path = self.workspace / self.FILE_NAME
        self.authority_id = _text(authority_id, "authority_id")

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
            return self._read_unlocked()

    def _validate_chain(self, records: tuple[ObservationIntakeRecord, ...]) -> None:
        previous: str | None = None
        identity: tuple[str, str, str, str, str, str] | None = None
        expected_cycle = 1
        seen_rows: set[str] = set()
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
            seen_rows.update(record.row_keys)
            previous = record.record_sha256
            expected_cycle += 1

    def append_cycle(
        self,
        *,
        session_id: str,
        source_id: str,
        campaign_id: str,
        research_protocol_id: str,
        protocol_sha256: str,
        universe_id: str,
        cycle_index: int,
        committed_at: str,
        outcome_reveal_not_before: str,
        row_keys: tuple[str, ...],
        source_state: str,
    ) -> ObservationIntakeRecord:
        with WorkspaceEconomicLock(self.workspace):
            records = self._read_unlocked()
            if type(cycle_index) is not int or cycle_index <= 0:
                raise EvaluationIntakeError("cycle_index must be a positive integer")
            if cycle_index <= len(records):
                existing = records[cycle_index - 1]
                proposed_without_previous = ObservationIntakeRecord(
                    authority_id=self.authority_id,
                    session_id=session_id,
                    source_id=source_id,
                    campaign_id=campaign_id,
                    research_protocol_id=research_protocol_id,
                    protocol_sha256=protocol_sha256,
                    universe_id=universe_id,
                    cycle_index=cycle_index,
                    committed_at=committed_at,
                    outcome_reveal_not_before=outcome_reveal_not_before,
                    row_keys=row_keys,
                    source_state=source_state,
                    previous_record_sha256=existing.previous_record_sha256,
                )
                if proposed_without_previous.record_sha256 == existing.record_sha256:
                    return existing
                raise EvaluationIntakeIntegrityError(
                    "intake cycle retry conflicts with durable history"
                )
            expected_cycle = len(records) + 1
            if cycle_index != expected_cycle:
                raise EvaluationIntakeIntegrityError(
                    "intake cycle cannot skip an observation cycle"
                )
            previous = None if not records else records[-1].record_sha256
            record = ObservationIntakeRecord(
                authority_id=self.authority_id,
                session_id=session_id,
                source_id=source_id,
                campaign_id=campaign_id,
                research_protocol_id=research_protocol_id,
                protocol_sha256=protocol_sha256,
                universe_id=universe_id,
                cycle_index=cycle_index,
                committed_at=committed_at,
                outcome_reveal_not_before=outcome_reveal_not_before,
                row_keys=row_keys,
                source_state=source_state,
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
            return record

    def snapshot(self, *, first_cycle: int, last_cycle: int) -> ObservationIntakeSnapshot:
        with WorkspaceEconomicLock(self.workspace):
            records = self._read_unlocked()
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
                "snapshot does not match canonical durable pre-result intake"
            )

    def record_for_row(self, row_key: str) -> ObservationIntakeRecord:
        row_key = _text(row_key, "row_key")
        matches = [record for record in self.records() if row_key in record.row_keys]
        if len(matches) != 1:
            raise EvaluationIntakeIntegrityError(
                "row_key must resolve to exactly one canonical intake record"
            )
        return matches[0]
