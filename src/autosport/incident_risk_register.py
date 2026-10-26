"""Typed incident/model-risk register contract and operator projection.

The register contract is deliberately presentation-oriented. It does not grant or
revoke execution authority, does not persist credentials, and does not promote any
release/human-verification truth. Durable storage and Windows widget wiring remain
separate product boundaries.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, IntEnum
from typing import Final, Iterable


_SCHEMA: Final = "autosport.incident_model_risk_entry"
_SCHEMA_VERSION: Final = 2
_ENTRY_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "entry_id",
        "revision",
        "kind",
        "severity",
        "status",
        "evidence_state",
        "opened_at",
        "updated_at",
        "title",
        "summary",
        "mitigation",
        "residual_risk",
        "affected_components",
        "occurrence_evidence_refs",
        "evidence_refs",
        "model_version_ids",
        "requires_operator_action",
    }
)
_ENUM_JSON_FIELDS: Final = ("kind", "severity", "status", "evidence_state")


class IncidentRiskRegisterError(ValueError):
    """Raised when a register entry or revision is non-canonical."""


class RegisterEntryKind(str, Enum):
    INCIDENT = "incident"
    MODEL_RISK = "model_risk"


class RiskSeverity(IntEnum):
    """Operator triage severity; numeric ordering is presentation-only."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @property
    def token(self) -> str:
        return self.name.lower()


class RiskStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    MITIGATING = "mitigating"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"


_TERMINAL_RISK_STATUSES: Final = frozenset(
    {RiskStatus.RESOLVED, RiskStatus.SUPERSEDED}
)
_ALLOWED_STATUS_SUCCESSORS: Final = {
    RiskStatus.OPEN: frozenset(
        {
            RiskStatus.OPEN,
            RiskStatus.ACKNOWLEDGED,
            RiskStatus.MITIGATING,
            RiskStatus.RESOLVED,
            RiskStatus.SUPERSEDED,
        }
    ),
    RiskStatus.ACKNOWLEDGED: frozenset(
        {
            RiskStatus.ACKNOWLEDGED,
            RiskStatus.MITIGATING,
            RiskStatus.RESOLVED,
            RiskStatus.SUPERSEDED,
        }
    ),
    RiskStatus.MITIGATING: frozenset(
        {
            RiskStatus.MITIGATING,
            RiskStatus.RESOLVED,
            RiskStatus.SUPERSEDED,
        }
    ),
    RiskStatus.RESOLVED: frozenset(
        {RiskStatus.RESOLVED, RiskStatus.OPEN}
    ),
    RiskStatus.SUPERSEDED: frozenset(
        {RiskStatus.SUPERSEDED, RiskStatus.OPEN}
    ),
}


class RiskEvidenceState(str, Enum):
    UNVERIFIED = "unverified"
    PARTIAL = "partial"
    VERIFIED = "verified"


def _canonical_text(
    name: str,
    value: object,
    *,
    allow_empty: bool = False,
    maximum_length: int = 4096,
) -> str:
    if type(value) is not str:
        raise IncidentRiskRegisterError(f"{name} must be a string")
    if value == "" and allow_empty:
        return value
    if not value or value.strip() != value:
        raise IncidentRiskRegisterError(f"{name} must be canonical trimmed text")
    if "\x00" in value:
        raise IncidentRiskRegisterError(f"{name} must not contain NUL")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise IncidentRiskRegisterError(f"{name} must be valid UTF-8 text") from exc
    if len(encoded) > maximum_length:
        raise IncidentRiskRegisterError(
            f"{name} must be at most {maximum_length} UTF-8 bytes"
        )
    return value


def _positive_int(name: str, value: object) -> int:
    if type(value) is not int or value <= 0:
        raise IncidentRiskRegisterError(
            f"{name} must be a positive non-boolean integer"
        )
    return value


def _canonical_timestamp(name: str, value: object) -> tuple[str, datetime]:
    raw = _canonical_text(name, value, maximum_length=64)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise IncidentRiskRegisterError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IncidentRiskRegisterError(f"{name} must be timezone-aware")
    if parsed.utcoffset() != timedelta(0):
        raise IncidentRiskRegisterError(f"{name} must use canonical UTC +00:00")
    if parsed.isoformat() != raw:
        raise IncidentRiskRegisterError(
            f"{name} must use datetime.isoformat() canonical UTC form"
        )
    return raw, parsed


def _canonical_string_tuple(name: str, value: object) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise IncidentRiskRegisterError(f"{name} must be a tuple")
    normalized = tuple(
        _canonical_text(f"{name} member", item, maximum_length=512) for item in value
    )
    if normalized != tuple(sorted(set(normalized))):
        raise IncidentRiskRegisterError(f"{name} must be sorted and unique")
    return normalized


def _enum_value(enum_type, name: str, value: object):
    if not isinstance(value, enum_type):
        raise IncidentRiskRegisterError(f"{name} must be {enum_type.__name__}")
    return value


def derive_occurrence_entry_id(
    *,
    kind: RegisterEntryKind,
    affected_components: tuple[str, ...],
    occurrence_evidence_refs: tuple[str, ...],
    model_version_ids: tuple[str, ...] = (),
) -> str:
    """Derive stable occurrence identity from immutable canonical source identities."""

    _enum_value(RegisterEntryKind, "kind", kind)
    components = _canonical_string_tuple(
        "affected_components", affected_components
    )
    occurrence_refs = _canonical_string_tuple(
        "occurrence_evidence_refs", occurrence_evidence_refs
    )
    models = _canonical_string_tuple("model_version_ids", model_version_ids)
    if not occurrence_refs:
        raise IncidentRiskRegisterError(
            "occurrence identity requires at least one occurrence_evidence_ref"
        )
    if kind is RegisterEntryKind.MODEL_RISK and not models:
        raise IncidentRiskRegisterError(
            "model-risk occurrence identity requires a model_version_id"
        )
    payload = json.dumps(
        {
            "schema": "autosport.incident_model_risk_occurrence_identity",
            "schema_version": 1,
            "kind": kind.value,
            "affected_components": list(components),
            "occurrence_evidence_refs": list(occurrence_refs),
            "model_version_ids": list(models),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"{kind.value}:{hashlib.sha256(payload).hexdigest()}"


def _timestamp_sort_key(value: datetime) -> int:
    """Return exact integer microseconds for deterministic UTC ordering."""

    return (
        (
            value.toordinal() * 86_400
            + value.hour * 3_600
            + value.minute * 60
            + value.second
        )
        * 1_000_000
        + value.microsecond
    )


@dataclass(frozen=True, slots=True)
class IncidentRiskEntry:
    """One immutable revision of an operator-visible incident/model-risk entry."""

    entry_id: str
    revision: int
    kind: RegisterEntryKind
    severity: RiskSeverity
    status: RiskStatus
    evidence_state: RiskEvidenceState
    opened_at: str
    updated_at: str
    title: str
    summary: str
    mitigation: str = ""
    residual_risk: str = ""
    affected_components: tuple[str, ...] = ()
    occurrence_evidence_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    model_version_ids: tuple[str, ...] = ()
    requires_operator_action: bool = False

    def __post_init__(self) -> None:
        _canonical_text("entry_id", self.entry_id, maximum_length=160)
        _positive_int("revision", self.revision)
        _enum_value(RegisterEntryKind, "kind", self.kind)
        _enum_value(RiskSeverity, "severity", self.severity)
        _enum_value(RiskStatus, "status", self.status)
        _enum_value(RiskEvidenceState, "evidence_state", self.evidence_state)
        _, opened = _canonical_timestamp("opened_at", self.opened_at)
        _, updated = _canonical_timestamp("updated_at", self.updated_at)
        if updated < opened:
            raise IncidentRiskRegisterError("updated_at must not precede opened_at")
        _canonical_text("title", self.title, maximum_length=256)
        _canonical_text("summary", self.summary, maximum_length=4096)
        _canonical_text(
            "mitigation",
            self.mitigation,
            allow_empty=True,
            maximum_length=4096,
        )
        _canonical_text(
            "residual_risk",
            self.residual_risk,
            allow_empty=True,
            maximum_length=4096,
        )
        _canonical_string_tuple("affected_components", self.affected_components)
        _canonical_string_tuple(
            "occurrence_evidence_refs", self.occurrence_evidence_refs
        )
        _canonical_string_tuple("evidence_refs", self.evidence_refs)
        _canonical_string_tuple("model_version_ids", self.model_version_ids)
        if type(self.requires_operator_action) is not bool:
            raise IncidentRiskRegisterError("requires_operator_action must be a bool")

        if self.kind is RegisterEntryKind.MODEL_RISK and not self.model_version_ids:
            raise IncidentRiskRegisterError(
                "model-risk entries require at least one exact model_version_id"
            )
        if not self.occurrence_evidence_refs:
            raise IncidentRiskRegisterError(
                "entries require at least one immutable occurrence_evidence_ref"
            )
        if not set(self.occurrence_evidence_refs).issubset(self.evidence_refs):
            raise IncidentRiskRegisterError(
                "occurrence_evidence_refs must be included in evidence_refs"
            )
        expected_entry_id = derive_occurrence_entry_id(
            kind=self.kind,
            affected_components=self.affected_components,
            occurrence_evidence_refs=self.occurrence_evidence_refs,
            model_version_ids=self.model_version_ids,
        )
        if self.entry_id != expected_entry_id:
            raise IncidentRiskRegisterError(
                "entry_id must equal product-derived occurrence identity"
            )
        if (
            self.evidence_state
            in {RiskEvidenceState.PARTIAL, RiskEvidenceState.VERIFIED}
            and not self.evidence_refs
        ):
            raise IncidentRiskRegisterError(
                "partial/verified evidence state requires at least one evidence_ref"
            )
        if (
            self.status
            in {
                RiskStatus.MITIGATING,
                RiskStatus.RESOLVED,
                RiskStatus.SUPERSEDED,
            }
            and not self.mitigation
        ):
            raise IncidentRiskRegisterError(
                "mitigating/resolved/superseded entries require a non-empty mitigation"
            )
        if self.status in _TERMINAL_RISK_STATUSES:
            if self.evidence_state is not RiskEvidenceState.VERIFIED:
                raise IncidentRiskRegisterError(
                    "terminal entries require verified evidence"
                )
            if self.requires_operator_action:
                raise IncidentRiskRegisterError(
                    "terminal entries cannot require operator action"
                )
        elif (
            self.severity is RiskSeverity.CRITICAL
            and not self.requires_operator_action
        ):
            raise IncidentRiskRegisterError(
                "unresolved critical entries must require operator action"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "entry_id": self.entry_id,
            "revision": self.revision,
            "kind": self.kind.value,
            "severity": self.severity.token,
            "status": self.status.value,
            "evidence_state": self.evidence_state.value,
            "opened_at": self.opened_at,
            "updated_at": self.updated_at,
            "title": self.title,
            "summary": self.summary,
            "mitigation": self.mitigation,
            "residual_risk": self.residual_risk,
            "affected_components": list(self.affected_components),
            "occurrence_evidence_refs": list(self.occurrence_evidence_refs),
            "evidence_refs": list(self.evidence_refs),
            "model_version_ids": list(self.model_version_ids),
            "requires_operator_action": self.requires_operator_action,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "IncidentRiskEntry":
        if type(raw) is not dict or set(raw) != _ENTRY_KEYS:
            raise IncidentRiskRegisterError(
                "incident/model-risk entry must contain exactly canonical fields"
            )
        if (
            type(raw["schema"]) is not str
            or raw["schema"] != _SCHEMA
            or type(raw["schema_version"]) is not int
            or raw["schema_version"] != _SCHEMA_VERSION
        ):
            raise IncidentRiskRegisterError("unsupported incident/model-risk schema")
        for name in _ENUM_JSON_FIELDS:
            if type(raw[name]) is not str:
                raise IncidentRiskRegisterError(f"{name} must be a JSON string")

        def tuple_field(name: str) -> tuple[str, ...]:
            value = raw[name]
            if type(value) is not list or any(type(item) is not str for item in value):
                raise IncidentRiskRegisterError(f"{name} must be a JSON string array")
            return tuple(value)

        try:
            severity = {
                member.token: member
                for member in RiskSeverity
            }[raw["severity"]]
            return cls(
                entry_id=raw["entry_id"],
                revision=raw["revision"],
                kind=RegisterEntryKind(raw["kind"]),
                severity=severity,
                status=RiskStatus(raw["status"]),
                evidence_state=RiskEvidenceState(raw["evidence_state"]),
                opened_at=raw["opened_at"],
                updated_at=raw["updated_at"],
                title=raw["title"],
                summary=raw["summary"],
                mitigation=raw["mitigation"],
                residual_risk=raw["residual_risk"],
                affected_components=tuple_field("affected_components"),
                occurrence_evidence_refs=tuple_field(
                    "occurrence_evidence_refs"
                ),
                evidence_refs=tuple_field("evidence_refs"),
                model_version_ids=tuple_field("model_version_ids"),
                requires_operator_action=raw["requires_operator_action"],
            )
        except IncidentRiskRegisterError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise IncidentRiskRegisterError(
                "incident/model-risk entry contains invalid enum/value data"
            ) from exc

    @property
    def fingerprint_sha256(self) -> str:
        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def validate_successor(
    previous: IncidentRiskEntry,
    candidate: IncidentRiskEntry,
) -> None:
    """Require contiguous immutable-identity history for one register entry."""

    if not isinstance(previous, IncidentRiskEntry) or not isinstance(
        candidate, IncidentRiskEntry
    ):
        raise TypeError("register successor validation requires IncidentRiskEntry values")
    if candidate.entry_id != previous.entry_id:
        raise IncidentRiskRegisterError("successor must preserve entry_id")
    if candidate.kind is not previous.kind:
        raise IncidentRiskRegisterError("successor must preserve entry kind")
    if candidate.affected_components != previous.affected_components:
        raise IncidentRiskRegisterError(
            "successor must preserve affected_components occurrence identity"
        )
    if candidate.occurrence_evidence_refs != previous.occurrence_evidence_refs:
        raise IncidentRiskRegisterError(
            "successor must preserve occurrence_evidence_refs"
        )
    if candidate.model_version_ids != previous.model_version_ids:
        raise IncidentRiskRegisterError(
            "successor must preserve model_version_ids occurrence identity"
        )
    if candidate.opened_at != previous.opened_at:
        raise IncidentRiskRegisterError("successor must preserve opened_at")
    if candidate.revision != previous.revision + 1:
        raise IncidentRiskRegisterError("successor revision must be contiguous")
    _, previous_updated = _canonical_timestamp(
        "previous.updated_at", previous.updated_at
    )
    _, candidate_updated = _canonical_timestamp(
        "candidate.updated_at", candidate.updated_at
    )
    if candidate_updated <= previous_updated:
        raise IncidentRiskRegisterError(
            "successor updated_at must move strictly forward"
        )

    if candidate.status not in _ALLOWED_STATUS_SUCCESSORS[previous.status]:
        raise IncidentRiskRegisterError(
            "unsupported incident/model-risk lifecycle transition"
        )

    previous_evidence = set(previous.evidence_refs)
    candidate_evidence = set(candidate.evidence_refs)
    if not previous_evidence.issubset(candidate_evidence):
        raise IncidentRiskRegisterError(
            "successor must preserve all prior evidence_refs"
        )

    enters_terminal = (
        candidate.status in _TERMINAL_RISK_STATUSES
        and candidate.status is not previous.status
    )
    follows_terminal = previous.status in _TERMINAL_RISK_STATUSES
    if enters_terminal or follows_terminal:
        if (
            candidate.evidence_state is not RiskEvidenceState.VERIFIED
            or not previous_evidence < candidate_evidence
        ):
            raise IncidentRiskRegisterError(
                "terminal transition/reopen requires new verified evidence"
            )


@dataclass(frozen=True, slots=True)
class OperatorRiskProjection:
    """Localization-ready presentation descriptor with no execution authority."""

    entry_id: str
    revision: int
    kind_key: str
    severity_key: str
    status_key: str
    evidence_key: str
    title: str
    summary: str
    mitigation: str
    residual_risk: str
    affected_components: tuple[str, ...]
    occurrence_evidence_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    model_version_ids: tuple[str, ...]
    requires_operator_action: bool
    fingerprint_sha256: str


def operator_projection(entry: IncidentRiskEntry) -> OperatorRiskProjection:
    if not isinstance(entry, IncidentRiskEntry):
        raise TypeError("operator projection requires an IncidentRiskEntry")
    return OperatorRiskProjection(
        entry_id=entry.entry_id,
        revision=entry.revision,
        kind_key=f"ui.risk_register.kind.{entry.kind.value}",
        severity_key=f"ui.risk_register.severity.{entry.severity.token}",
        status_key=f"ui.risk_register.status.{entry.status.value}",
        evidence_key=f"ui.risk_register.evidence.{entry.evidence_state.value}",
        title=entry.title,
        summary=entry.summary,
        mitigation=entry.mitigation,
        residual_risk=entry.residual_risk,
        affected_components=entry.affected_components,
        occurrence_evidence_refs=entry.occurrence_evidence_refs,
        evidence_refs=entry.evidence_refs,
        model_version_ids=entry.model_version_ids,
        requires_operator_action=entry.requires_operator_action,
        fingerprint_sha256=entry.fingerprint_sha256,
    )


def operator_sort(entries: Iterable[IncidentRiskEntry]) -> tuple[IncidentRiskEntry, ...]:
    """Put actionable/high-severity/newest entries first without mutating truth."""

    materialized = tuple(entries)
    if any(not isinstance(entry, IncidentRiskEntry) for entry in materialized):
        raise TypeError("operator_sort accepts only IncidentRiskEntry values")

    def key(entry: IncidentRiskEntry) -> tuple[int, int, int, str]:
        _, updated = _canonical_timestamp("updated_at", entry.updated_at)
        return (
            0 if entry.requires_operator_action else 1,
            -int(entry.severity),
            -_timestamp_sort_key(updated),
            entry.entry_id,
        )

    return tuple(sorted(materialized, key=key))
