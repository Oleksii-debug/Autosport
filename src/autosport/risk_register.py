from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Iterable


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _require_exact_str(value: object, label: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be an exact string")
    if value != value.strip():
        raise ValueError(f"{label} must not contain surrounding whitespace")
    if not allow_empty and not value:
        raise ValueError(f"{label} must not be empty")
    return value


def _require_id(value: object, label: str) -> str:
    text = _require_exact_str(value, label)
    if _ID_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be a canonical identifier")
    return text


def _canonical_utc(value: object, label: str) -> str:
    text = _require_exact_str(value, label)
    if not text.endswith("Z"):
        raise ValueError(f"{label} must use canonical UTC Z notation")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"{label} must be UTC")
    canonical = parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
    if text != canonical:
        raise ValueError(f"{label} must use second-precision canonical UTC form")
    return canonical


class RiskKind(StrEnum):
    INCIDENT = "incident"
    MODEL_RISK = "model_risk"


class RiskSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RiskStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    MITIGATING = "mitigating"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class RiskEvidence:
    authority_family: str
    evidence_id: str
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "authority_family",
            _require_id(self.authority_family, "authority_family"),
        )
        object.__setattr__(self, "evidence_id", _require_id(self.evidence_id, "evidence_id"))
        if type(self.sha256) is not str or _SHA256_RE.fullmatch(self.sha256) is None:
            raise ValueError("sha256 must be exactly 64 lowercase hexadecimal characters")

    @property
    def identity(self) -> tuple[str, str]:
        return self.authority_family, self.evidence_id

    def to_dict(self) -> dict[str, str]:
        return {
            "authority_family": self.authority_family,
            "evidence_id": self.evidence_id,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class RiskRecord:
    risk_id: str
    kind: RiskKind
    severity: RiskSeverity
    status: RiskStatus
    title: str
    summary: str
    owner: str
    first_observed_at: str
    updated_at: str
    operator_action: str
    mitigation: str
    evidence: tuple[RiskEvidence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "risk_id", _require_id(self.risk_id, "risk_id"))
        if type(self.kind) is not RiskKind:
            raise ValueError("kind must be RiskKind")
        if type(self.severity) is not RiskSeverity:
            raise ValueError("severity must be RiskSeverity")
        if type(self.status) is not RiskStatus:
            raise ValueError("status must be RiskStatus")
        for name in ("title", "summary", "owner"):
            object.__setattr__(self, name, _require_exact_str(getattr(self, name), name))
        object.__setattr__(
            self,
            "operator_action",
            _require_exact_str(
                self.operator_action,
                "operator_action",
                allow_empty=self.status is RiskStatus.RESOLVED,
            ),
        )
        object.__setattr__(
            self,
            "mitigation",
            _require_exact_str(
                self.mitigation,
                "mitigation",
                allow_empty=self.status in {RiskStatus.OPEN, RiskStatus.ACKNOWLEDGED},
            ),
        )
        first = _canonical_utc(self.first_observed_at, "first_observed_at")
        updated = _canonical_utc(self.updated_at, "updated_at")
        object.__setattr__(self, "first_observed_at", first)
        object.__setattr__(self, "updated_at", updated)
        if updated < first:
            raise ValueError("updated_at must not precede first_observed_at")
        if type(self.evidence) is not tuple or any(type(item) is not RiskEvidence for item in self.evidence):
            raise ValueError("evidence must be an exact tuple of RiskEvidence")
        identities: dict[tuple[str, str], str] = {}
        for item in self.evidence:
            previous = identities.setdefault(item.identity, item.sha256)
            if previous != item.sha256:
                raise ValueError("one evidence identity cannot bind multiple digests")
        if len(identities) != len(self.evidence):
            raise ValueError("duplicate evidence identities are not allowed")
        object.__setattr__(
            self,
            "evidence",
            tuple(sorted(self.evidence, key=lambda item: (item.authority_family, item.evidence_id))),
        )
        if self.status is RiskStatus.RESOLVED:
            if not self.mitigation:
                raise ValueError("resolved risk requires mitigation")
            if not self.evidence:
                raise ValueError("resolved risk requires evidence")

    @property
    def unresolved(self) -> bool:
        return self.status is not RiskStatus.RESOLVED

    def to_dict(self) -> dict[str, object]:
        return {
            "risk_id": self.risk_id,
            "kind": self.kind.value,
            "severity": self.severity.value,
            "status": self.status.value,
            "title": self.title,
            "summary": self.summary,
            "owner": self.owner,
            "first_observed_at": self.first_observed_at,
            "updated_at": self.updated_at,
            "operator_action": self.operator_action,
            "mitigation": self.mitigation,
            "evidence": [item.to_dict() for item in self.evidence],
        }


_SEVERITY_RANK = {
    RiskSeverity.CRITICAL: 0,
    RiskSeverity.HIGH: 1,
    RiskSeverity.MEDIUM: 2,
    RiskSeverity.LOW: 3,
}
_STATUS_RANK = {
    RiskStatus.OPEN: 0,
    RiskStatus.ACKNOWLEDGED: 1,
    RiskStatus.MITIGATING: 2,
    RiskStatus.RESOLVED: 3,
}


def _record_sort_key(record: RiskRecord) -> tuple[bool, int, int, str]:
    return (
        record.status is RiskStatus.RESOLVED,
        _SEVERITY_RANK[record.severity],
        _STATUS_RANK[record.status],
        record.risk_id,
    )


@dataclass(frozen=True, slots=True)
class RiskRegisterSnapshot:
    generated_at: str
    records: tuple[RiskRecord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "generated_at", _canonical_utc(self.generated_at, "generated_at"))
        if type(self.records) is not tuple or any(type(item) is not RiskRecord for item in self.records):
            raise ValueError("records must be an exact tuple of RiskRecord")
        object.__setattr__(self, "records", tuple(sorted(self.records, key=_record_sort_key)))
        ids: set[str] = set()
        evidence_by_identity: dict[tuple[str, str], str] = {}
        for record in self.records:
            if record.risk_id in ids:
                raise ValueError(f"duplicate risk_id: {record.risk_id}")
            ids.add(record.risk_id)
            if record.updated_at > self.generated_at:
                raise ValueError("snapshot cannot contain a risk update from the future")
            for evidence in record.evidence:
                previous = evidence_by_identity.setdefault(evidence.identity, evidence.sha256)
                if previous != evidence.sha256:
                    raise ValueError(
                        "one evidence identity cannot bind different digests across the register"
                    )

    @classmethod
    def build(cls, generated_at: str, records: Iterable[RiskRecord]) -> RiskRegisterSnapshot:
        if isinstance(records, (str, bytes, dict)):
            raise ValueError("records must be an iterable of RiskRecord")
        materialized = tuple(records)
        if any(type(item) is not RiskRecord for item in materialized):
            raise ValueError("records must contain only exact RiskRecord values")
        return cls(generated_at=generated_at, records=materialized)

    @property
    def unresolved_count(self) -> int:
        return sum(record.unresolved for record in self.records)

    @property
    def critical_unresolved_count(self) -> int:
        return sum(
            record.unresolved and record.severity is RiskSeverity.CRITICAL
            for record in self.records
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "generated_at": self.generated_at,
            "truth_boundary": "operator_diagnostics_only",
            "unresolved_count": self.unresolved_count,
            "critical_unresolved_count": self.critical_unresolved_count,
            "records": [record.to_dict() for record in self.records],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    def operator_rows(self) -> tuple[str, ...]:
        """Return deterministic, screen-reader-friendly one-record-per-row text.

        This is a diagnostic projection only. It deliberately does not emit a
        release/readiness decision and does not transform missing evidence into
        positive evidence.
        """

        rows: list[str] = []
        for record in self.records:
            evidence_count = len(record.evidence)
            action = record.operator_action if record.operator_action else "none"
            rows.append(
                " | ".join(
                    (
                        f"risk={record.risk_id}",
                        f"kind={record.kind.value}",
                        f"severity={record.severity.value}",
                        f"status={record.status.value}",
                        f"owner={record.owner}",
                        f"action={action}",
                        f"evidence={evidence_count}",
                        f"title={record.title}",
                    )
                )
            )
        return tuple(rows)
