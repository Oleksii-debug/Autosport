"""Fail-closed incident/model-risk register schema and operator projection.

This module is deliberately descriptive.  It provides deterministic, immutable risk facts
and a read-only operator-facing projection.  It does not authorize model promotion,
provider writes, execution, readiness, or real-money use, and it does not create a second
incident persistence authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Iterable

from .json_integrity import strict_json_loads


_SCHEMA_KIND = "autosport_incident_model_risk_entry"
_SCHEMA_VERSION = 1


class ModelRiskRegisterError(ValueError):
    """Raised when an incident/model-risk fact violates the register contract."""


class RegisterEntryType(str, Enum):
    INCIDENT = "incident"
    MODEL_RISK = "model_risk"


class RiskSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RiskStatus(str, Enum):
    OPEN = "open"
    MITIGATING = "mitigating"
    BLOCKED = "blocked"
    RESOLVED = "resolved"


class OperatorPriority(str, Enum):
    IMMEDIATE = "immediate"
    URGENT = "urgent"
    REVIEW = "review"
    CLOSED = "closed"


_SEVERITY_RANK = {
    RiskSeverity.LOW: 1,
    RiskSeverity.MEDIUM: 2,
    RiskSeverity.HIGH: 3,
    RiskSeverity.CRITICAL: 4,
}
_PRIORITY_RANK = {
    OperatorPriority.CLOSED: 0,
    OperatorPriority.REVIEW: 1,
    OperatorPriority.URGENT: 2,
    OperatorPriority.IMMEDIATE: 3,
}


def _text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ModelRiskRegisterError(f"{field} must be a string")
    if not value or value != value.strip():
        raise ModelRiskRegisterError(f"{field} must be non-empty and trimmed")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ModelRiskRegisterError(f"{field} must be valid UTF-8 text") from exc
    return value


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _canonical_timestamp(value: str, field: str) -> str:
    _text(value, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelRiskRegisterError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ModelRiskRegisterError(f"{field} must include a timezone offset")
    normalized = parsed.astimezone(timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _bool(value: bool, field: str) -> bool:
    if type(value) is not bool:
        raise ModelRiskRegisterError(f"{field} must be a bool")
    return value


def _enum(value: object, enum_type: type[Enum], field: str) -> Enum:
    if not isinstance(value, enum_type):
        raise ModelRiskRegisterError(f"{field} must be a {enum_type.__name__} value")
    return value


def _canonical_refs(value: tuple[str, ...], field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ModelRiskRegisterError(f"{field} must be a tuple")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        item = _text(item, f"{field} item")
        if item in seen:
            raise ModelRiskRegisterError(f"{field} must not contain duplicates")
        seen.add(item)
        normalized.append(item)
    return tuple(sorted(normalized))


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class RiskRegisterEntry:
    """One current incident/model-risk fact with deterministic evidence identity."""

    entry_id: str
    entry_type: RegisterEntryType
    severity: RiskSeverity
    status: RiskStatus
    title: str
    summary: str
    detected_at: str
    updated_at: str
    evidence_refs: tuple[str, ...]
    affected_authorities: tuple[str, ...]
    blocks_product_readiness: bool
    blocks_execution: bool
    owner_ref: str | None = None
    model_or_strategy_ref: str | None = None
    resolution_summary: str | None = None

    def __post_init__(self) -> None:
        _text(self.entry_id, "entry_id")
        _enum(self.entry_type, RegisterEntryType, "entry_type")
        _enum(self.severity, RiskSeverity, "severity")
        _enum(self.status, RiskStatus, "status")
        _text(self.title, "title")
        _text(self.summary, "summary")
        detected_at = _canonical_timestamp(self.detected_at, "detected_at")
        updated_at = _canonical_timestamp(self.updated_at, "updated_at")
        if _timestamp_value(updated_at) < _timestamp_value(detected_at):
            raise ModelRiskRegisterError("updated_at cannot precede detected_at")
        object.__setattr__(self, "detected_at", detected_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(
            self,
            "evidence_refs",
            _canonical_refs(self.evidence_refs, "evidence_refs"),
        )
        object.__setattr__(
            self,
            "affected_authorities",
            _canonical_refs(self.affected_authorities, "affected_authorities"),
        )
        _bool(self.blocks_product_readiness, "blocks_product_readiness")
        _bool(self.blocks_execution, "blocks_execution")
        if self.blocks_execution and not self.blocks_product_readiness:
            raise ModelRiskRegisterError(
                "an execution blocker must also block product readiness"
            )
        _optional_text(self.owner_ref, "owner_ref")
        _optional_text(self.model_or_strategy_ref, "model_or_strategy_ref")
        _optional_text(self.resolution_summary, "resolution_summary")

        if (
            self.entry_type is RegisterEntryType.MODEL_RISK
            and self.model_or_strategy_ref is None
        ):
            raise ModelRiskRegisterError(
                "model_risk entries require model_or_strategy_ref"
            )
        if self.status is RiskStatus.RESOLVED:
            if self.resolution_summary is None:
                raise ModelRiskRegisterError(
                    "resolved entries require resolution_summary"
                )
            if self.blocks_product_readiness or self.blocks_execution:
                raise ModelRiskRegisterError(
                    "resolved entries cannot retain readiness/execution blockers"
                )
        elif self.resolution_summary is not None:
            raise ModelRiskRegisterError(
                "resolution_summary is allowed only for resolved entries"
            )

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "affected_authorities": list(self.affected_authorities),
            "blocks_execution": self.blocks_execution,
            "blocks_product_readiness": self.blocks_product_readiness,
            "detected_at": self.detected_at,
            "entry_id": self.entry_id,
            "entry_type": self.entry_type.value,
            "evidence_refs": list(self.evidence_refs),
            "model_or_strategy_ref": self.model_or_strategy_ref,
            "owner_ref": self.owner_ref,
            "resolution_summary": self.resolution_summary,
            "severity": self.severity.value,
            "status": self.status.value,
            "summary": self.summary,
            "title": self.title,
            "updated_at": self.updated_at,
        }

    @property
    def entry_sha256(self) -> str:
        return sha256(_canonical_json_bytes(self.to_canonical_dict())).hexdigest()

    @property
    def operator_priority(self) -> OperatorPriority:
        if self.status is RiskStatus.RESOLVED:
            return OperatorPriority.CLOSED
        if self.blocks_execution or self.severity is RiskSeverity.CRITICAL:
            return OperatorPriority.IMMEDIATE
        if self.blocks_product_readiness or self.severity is RiskSeverity.HIGH:
            return OperatorPriority.URGENT
        return OperatorPriority.REVIEW

    def to_json(self) -> str:
        envelope = {
            "entry": self.to_canonical_dict(),
            "entry_sha256": self.entry_sha256,
            "schema_kind": _SCHEMA_KIND,
            "schema_version": _SCHEMA_VERSION,
        }
        return _canonical_json_bytes(envelope).decode("utf-8")

    @classmethod
    def from_json(cls, text: str) -> "RiskRegisterEntry":
        if not isinstance(text, str):
            raise ModelRiskRegisterError("risk register JSON must be text")
        try:
            payload = strict_json_loads(text)
        except (TypeError, ValueError) as exc:
            raise ModelRiskRegisterError("risk register JSON is invalid") from exc
        if not isinstance(payload, dict):
            raise ModelRiskRegisterError("risk register envelope must be an object")
        expected_envelope = {
            "entry",
            "entry_sha256",
            "schema_kind",
            "schema_version",
        }
        if set(payload) != expected_envelope:
            raise ModelRiskRegisterError(
                "risk register envelope fields do not match schema v1"
            )
        if payload["schema_kind"] != _SCHEMA_KIND:
            raise ModelRiskRegisterError("unsupported risk register schema kind")
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise ModelRiskRegisterError("unsupported risk register schema version")
        entry_payload = payload["entry"]
        if not isinstance(entry_payload, dict):
            raise ModelRiskRegisterError("entry must be an object")
        expected_entry = {
            "affected_authorities",
            "blocks_execution",
            "blocks_product_readiness",
            "detected_at",
            "entry_id",
            "entry_type",
            "evidence_refs",
            "model_or_strategy_ref",
            "owner_ref",
            "resolution_summary",
            "severity",
            "status",
            "summary",
            "title",
            "updated_at",
        }
        if set(entry_payload) != expected_entry:
            raise ModelRiskRegisterError("entry fields do not match schema v1")

        evidence_refs = entry_payload["evidence_refs"]
        affected_authorities = entry_payload["affected_authorities"]
        if not isinstance(evidence_refs, list) or not isinstance(
            affected_authorities, list
        ):
            raise ModelRiskRegisterError(
                "evidence_refs and affected_authorities must be arrays"
            )
        try:
            entry_type = RegisterEntryType(entry_payload["entry_type"])
            severity = RiskSeverity(entry_payload["severity"])
            status = RiskStatus(entry_payload["status"])
        except (TypeError, ValueError) as exc:
            raise ModelRiskRegisterError("entry contains an unsupported enum value") from exc

        entry = cls(
            entry_id=entry_payload["entry_id"],
            entry_type=entry_type,
            severity=severity,
            status=status,
            title=entry_payload["title"],
            summary=entry_payload["summary"],
            detected_at=entry_payload["detected_at"],
            updated_at=entry_payload["updated_at"],
            evidence_refs=tuple(evidence_refs),
            affected_authorities=tuple(affected_authorities),
            blocks_product_readiness=entry_payload["blocks_product_readiness"],
            blocks_execution=entry_payload["blocks_execution"],
            owner_ref=entry_payload["owner_ref"],
            model_or_strategy_ref=entry_payload["model_or_strategy_ref"],
            resolution_summary=entry_payload["resolution_summary"],
        )
        digest = payload["entry_sha256"]
        if not isinstance(digest, str) or digest != entry.entry_sha256:
            raise ModelRiskRegisterError("entry_sha256 does not match canonical entry")
        if text != entry.to_json():
            raise ModelRiskRegisterError("risk register JSON must use canonical encoding")
        return entry


@dataclass(frozen=True, slots=True)
class OperatorRiskRow:
    """Presentation-ready row that keeps localization authority outside this module."""

    entry_id: str
    entry_sha256: str
    entry_type: RegisterEntryType
    severity: RiskSeverity
    status: RiskStatus
    priority: OperatorPriority
    title: str
    summary: str
    updated_at: str
    blocks_product_readiness: bool
    blocks_execution: bool
    evidence_count: int

    @property
    def entry_type_key(self) -> str:
        return f"model_risk.entry_type.{self.entry_type.value}"

    @property
    def severity_key(self) -> str:
        return f"model_risk.severity.{self.severity.value}"

    @property
    def status_key(self) -> str:
        return f"model_risk.status.{self.status.value}"

    @property
    def priority_key(self) -> str:
        return f"model_risk.priority.{self.priority.value}"

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "blocks_execution": self.blocks_execution,
            "blocks_product_readiness": self.blocks_product_readiness,
            "entry_id": self.entry_id,
            "entry_sha256": self.entry_sha256,
            "entry_type": self.entry_type.value,
            "evidence_count": self.evidence_count,
            "priority": self.priority.value,
            "severity": self.severity.value,
            "status": self.status.value,
            "summary": self.summary,
            "title": self.title,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class OperatorRiskRegisterView:
    """Deterministic operator projection over one explicit current-entry universe."""

    as_of: str
    rows: tuple[OperatorRiskRow, ...]
    readiness_blocking_ids: tuple[str, ...]
    execution_blocking_ids: tuple[str, ...]
    unresolved_count: int
    critical_unresolved_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _canonical_timestamp(self.as_of, "as_of"))
        if not isinstance(self.rows, tuple) or not all(
            isinstance(row, OperatorRiskRow) for row in self.rows
        ):
            raise ModelRiskRegisterError("rows must be a tuple of OperatorRiskRow")
        if type(self.unresolved_count) is not int or self.unresolved_count < 0:
            raise ModelRiskRegisterError("unresolved_count must be a non-negative int")
        if (
            type(self.critical_unresolved_count) is not int
            or self.critical_unresolved_count < 0
            or self.critical_unresolved_count > self.unresolved_count
        ):
            raise ModelRiskRegisterError(
                "critical_unresolved_count must be within unresolved_count"
            )

    @property
    def operator_attention_required(self) -> bool:
        return self.unresolved_count > 0

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def readiness_claim_authorized(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False

    @property
    def view_sha256(self) -> str:
        payload = {
            "as_of": self.as_of,
            "critical_unresolved_count": self.critical_unresolved_count,
            "execution_blocking_ids": list(self.execution_blocking_ids),
            "readiness_blocking_ids": list(self.readiness_blocking_ids),
            "rows": [row.to_canonical_dict() for row in self.rows],
            "unresolved_count": self.unresolved_count,
        }
        return sha256(_canonical_json_bytes(payload)).hexdigest()


def build_operator_risk_register_view(
    entries: Iterable[RiskRegisterEntry], *, as_of: str
) -> OperatorRiskRegisterView:
    """Build a deterministic read-only operator view without widening any authority."""

    canonical_as_of = _canonical_timestamp(as_of, "as_of")
    cutoff = _timestamp_value(canonical_as_of)
    materialized: list[RiskRegisterEntry] = []
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, RiskRegisterEntry):
            raise ModelRiskRegisterError(
                "entries must contain only RiskRegisterEntry values"
            )
        if entry.entry_id in seen_ids:
            raise ModelRiskRegisterError(
                f"duplicate current register entry_id: {entry.entry_id}"
            )
        seen_ids.add(entry.entry_id)
        if _timestamp_value(entry.updated_at) > cutoff:
            raise ModelRiskRegisterError(
                f"entry {entry.entry_id} is not available by as_of"
            )
        materialized.append(entry)

    def _sort_key(entry: RiskRegisterEntry) -> tuple[int, int, int, str]:
        updated = _timestamp_value(entry.updated_at)
        utc_epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        since_epoch = updated - utc_epoch
        updated_microseconds = (
            since_epoch.days * 86_400_000_000
            + since_epoch.seconds * 1_000_000
            + since_epoch.microseconds
        )
        return (
            -_PRIORITY_RANK[entry.operator_priority],
            -_SEVERITY_RANK[entry.severity],
            -updated_microseconds,
            entry.entry_id,
        )

    ordered = sorted(materialized, key=_sort_key)
    rows = tuple(
        OperatorRiskRow(
            entry_id=entry.entry_id,
            entry_sha256=entry.entry_sha256,
            entry_type=entry.entry_type,
            severity=entry.severity,
            status=entry.status,
            priority=entry.operator_priority,
            title=entry.title,
            summary=entry.summary,
            updated_at=entry.updated_at,
            blocks_product_readiness=entry.blocks_product_readiness,
            blocks_execution=entry.blocks_execution,
            evidence_count=len(entry.evidence_refs),
        )
        for entry in ordered
    )
    unresolved = tuple(
        entry for entry in ordered if entry.status is not RiskStatus.RESOLVED
    )
    return OperatorRiskRegisterView(
        as_of=canonical_as_of,
        rows=rows,
        readiness_blocking_ids=tuple(
            sorted(entry.entry_id for entry in unresolved if entry.blocks_product_readiness)
        ),
        execution_blocking_ids=tuple(
            sorted(entry.entry_id for entry in unresolved if entry.blocks_execution)
        ),
        unresolved_count=len(unresolved),
        critical_unresolved_count=sum(
            entry.severity is RiskSeverity.CRITICAL for entry in unresolved
        ),
    )
