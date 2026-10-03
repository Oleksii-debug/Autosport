"""Fail-closed model drift, expiry, and retirement lifecycle contract.

This module records lifecycle evidence for one exact model artifact.  It deliberately
has no deployment, provider, economic-execution, release, or readiness authority.
Consumers may use :func:`evaluate_model_eligibility` as a prerequisite, never as a
substitute for their own scientific/deployment authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Final

from .drift_control import DriftControlError, DriftMonitor, DriftState
from .scientific_registry import ScientificRegistry


_SCHEMA: Final = "autosport.model_lifecycle_revision"
_SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")
_ELIGIBILITY_CONSTRUCTION_TOKEN: Final = object()
_RECORD_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "model_version_id",
        "model_artifact_sha256",
        "revision",
        "lifecycle_state",
        "drift_state",
        "created_at",
        "updated_at",
        "knowledge_valid_until",
        "drift_observed_at",
        "drift_valid_until",
        "evidence_refs",
        "reason_code",
    }
)


class ModelLifecycleError(ValueError):
    """Raised when lifecycle evidence is malformed or history is unsafe."""


class ModelLifecycleState(str, Enum):
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    EXPIRED = "expired"
    RETIRED = "retired"


class ModelDriftState(str, Enum):
    UNKNOWN = "unknown"
    STABLE = "stable"
    WARNING = "warning"
    BREACH = "breach"


def _canonical_text(name: str, value: object, *, maximum_bytes: int = 512) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ModelLifecycleError(f"{name} must be non-empty canonical text")
    if "\x00" in value:
        raise ModelLifecycleError(f"{name} must not contain NUL")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ModelLifecycleError(f"{name} must be valid UTF-8 text") from exc
    if len(encoded) > maximum_bytes:
        raise ModelLifecycleError(
            f"{name} must be at most {maximum_bytes} UTF-8 bytes"
        )
    return value


def _canonical_sha256(name: str, value: object) -> str:
    text = _canonical_text(name, value, maximum_bytes=64)
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ModelLifecycleError(f"{name} must be lowercase SHA-256 hex")
    return text


def _positive_int(name: str, value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ModelLifecycleError(f"{name} must be a positive non-boolean integer")
    return value


def _canonical_utc(name: str, value: object) -> tuple[str, datetime]:
    text = _canonical_text(name, value, maximum_bytes=64)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ModelLifecycleError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ModelLifecycleError(f"{name} must be timezone-aware")
    if parsed.utcoffset() != timedelta(0):
        raise ModelLifecycleError(f"{name} must use UTC +00:00")
    if parsed.isoformat() != text:
        raise ModelLifecycleError(
            f"{name} must use datetime.isoformat() canonical UTC form"
        )
    return text, parsed


def _canonical_refs(value: object) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise ModelLifecycleError("evidence_refs must be a tuple")
    refs = tuple(
        _canonical_text("evidence_ref", item, maximum_bytes=1024) for item in value
    )
    if refs != tuple(sorted(set(refs))):
        raise ModelLifecycleError("evidence_refs must be sorted and unique")
    return refs


def _parse_enum(enum_type, name: str, value: object):
    if type(value) is not str:
        raise ModelLifecycleError(f"{name} must be a JSON string")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ModelLifecycleError(f"{name} is unsupported") from exc


@dataclass(frozen=True, slots=True)
class ModelLifecycleRevision:
    """One immutable evidence revision for one exact model artifact."""

    model_version_id: str
    model_artifact_sha256: str
    revision: int
    lifecycle_state: ModelLifecycleState
    drift_state: ModelDriftState
    created_at: str
    updated_at: str
    knowledge_valid_until: str
    drift_observed_at: str
    drift_valid_until: str
    evidence_refs: tuple[str, ...]
    reason_code: str

    def __post_init__(self) -> None:
        _canonical_text("model_version_id", self.model_version_id, maximum_bytes=256)
        _canonical_sha256("model_artifact_sha256", self.model_artifact_sha256)
        _positive_int("revision", self.revision)
        if not isinstance(self.lifecycle_state, ModelLifecycleState):
            raise ModelLifecycleError(
                "lifecycle_state must be a ModelLifecycleState"
            )
        if not isinstance(self.drift_state, ModelDriftState):
            raise ModelLifecycleError("drift_state must be a ModelDriftState")
        _, created = _canonical_utc("created_at", self.created_at)
        _, updated = _canonical_utc("updated_at", self.updated_at)
        _, knowledge_until = _canonical_utc(
            "knowledge_valid_until", self.knowledge_valid_until
        )
        _, drift_observed = _canonical_utc(
            "drift_observed_at", self.drift_observed_at
        )
        _, drift_until = _canonical_utc("drift_valid_until", self.drift_valid_until)
        _canonical_refs(self.evidence_refs)
        _canonical_text("reason_code", self.reason_code, maximum_bytes=160)

        if updated < created:
            raise ModelLifecycleError("updated_at must not precede created_at")
        if knowledge_until <= created:
            raise ModelLifecycleError(
                "knowledge_valid_until must be strictly after created_at"
            )
        if drift_observed < created:
            raise ModelLifecycleError("drift_observed_at must not precede created_at")
        if drift_observed > updated:
            raise ModelLifecycleError("drift_observed_at must not exceed updated_at")
        if drift_until <= drift_observed:
            raise ModelLifecycleError(
                "drift_valid_until must be strictly after drift_observed_at"
            )
        if drift_until > knowledge_until:
            raise ModelLifecycleError(
                "drift_valid_until must not exceed knowledge_valid_until"
            )
        if self.drift_state is not ModelDriftState.UNKNOWN and not self.evidence_refs:
            raise ModelLifecycleError(
                "known drift state requires at least one evidence_ref"
            )
        if self.lifecycle_state is ModelLifecycleState.EXPIRED and updated < knowledge_until:
            raise ModelLifecycleError(
                "explicit expired state requires updated_at at or after knowledge expiry"
            )
        if self.lifecycle_state in {
            ModelLifecycleState.QUARANTINED,
            ModelLifecycleState.EXPIRED,
            ModelLifecycleState.RETIRED,
        } and not self.evidence_refs:
            raise ModelLifecycleError(
                "non-active lifecycle state requires at least one evidence_ref"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "model_version_id": self.model_version_id,
            "model_artifact_sha256": self.model_artifact_sha256,
            "revision": self.revision,
            "lifecycle_state": self.lifecycle_state.value,
            "drift_state": self.drift_state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "knowledge_valid_until": self.knowledge_valid_until,
            "drift_observed_at": self.drift_observed_at,
            "drift_valid_until": self.drift_valid_until,
            "evidence_refs": list(self.evidence_refs),
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "ModelLifecycleRevision":
        if type(raw) is not dict or set(raw) != _RECORD_KEYS:
            raise ModelLifecycleError(
                "model lifecycle revision must contain exactly canonical fields"
            )
        if (
            type(raw["schema"]) is not str
            or raw["schema"] != _SCHEMA
            or type(raw["schema_version"]) is not int
            or raw["schema_version"] != _SCHEMA_VERSION
        ):
            raise ModelLifecycleError("unsupported model lifecycle schema")
        refs = raw["evidence_refs"]
        if type(refs) is not list or any(type(item) is not str for item in refs):
            raise ModelLifecycleError("evidence_refs must be a JSON string array")
        return cls(
            model_version_id=raw["model_version_id"],
            model_artifact_sha256=raw["model_artifact_sha256"],
            revision=raw["revision"],
            lifecycle_state=_parse_enum(
                ModelLifecycleState, "lifecycle_state", raw["lifecycle_state"]
            ),
            drift_state=_parse_enum(ModelDriftState, "drift_state", raw["drift_state"]),
            created_at=raw["created_at"],
            updated_at=raw["updated_at"],
            knowledge_valid_until=raw["knowledge_valid_until"],
            drift_observed_at=raw["drift_observed_at"],
            drift_valid_until=raw["drift_valid_until"],
            evidence_refs=tuple(refs),
            reason_code=raw["reason_code"],
        )

    @property
    def fingerprint_sha256(self) -> str:
        encoded = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def validate_lifecycle_successor(
    previous: ModelLifecycleRevision,
    candidate: ModelLifecycleRevision,
) -> None:
    """Validate one contiguous fail-closed lifecycle history transition."""

    if type(previous) is not ModelLifecycleRevision or type(candidate) is not ModelLifecycleRevision:
        raise TypeError("lifecycle successor validation requires exact lifecycle revisions")
    if candidate.model_version_id != previous.model_version_id:
        raise ModelLifecycleError("successor must preserve model_version_id")
    if candidate.model_artifact_sha256 != previous.model_artifact_sha256:
        raise ModelLifecycleError("successor must preserve model_artifact_sha256")
    if candidate.created_at != previous.created_at:
        raise ModelLifecycleError("successor must preserve created_at")
    if candidate.revision != previous.revision + 1:
        raise ModelLifecycleError("successor revision must be contiguous")

    _, previous_updated = _canonical_utc("previous.updated_at", previous.updated_at)
    _, candidate_updated = _canonical_utc("candidate.updated_at", candidate.updated_at)
    if candidate_updated <= previous_updated:
        raise ModelLifecycleError("successor updated_at must move strictly forward")

    _, previous_knowledge = _canonical_utc(
        "previous.knowledge_valid_until", previous.knowledge_valid_until
    )
    _, candidate_knowledge = _canonical_utc(
        "candidate.knowledge_valid_until", candidate.knowledge_valid_until
    )
    if candidate_knowledge > previous_knowledge:
        raise ModelLifecycleError(
            "successor cannot extend knowledge validity for the same model artifact"
        )

    _, previous_drift_observed = _canonical_utc(
        "previous.drift_observed_at", previous.drift_observed_at
    )
    _, candidate_drift_observed = _canonical_utc(
        "candidate.drift_observed_at", candidate.drift_observed_at
    )
    if candidate_drift_observed < previous_drift_observed:
        raise ModelLifecycleError("successor drift observation cannot move backward")

    _, previous_drift_until = _canonical_utc(
        "previous.drift_valid_until", previous.drift_valid_until
    )
    _, candidate_drift_until = _canonical_utc(
        "candidate.drift_valid_until", candidate.drift_valid_until
    )
    if candidate_drift_observed == previous_drift_observed:
        if candidate_drift_until > previous_drift_until:
            raise ModelLifecycleError(
                "unchanged drift observation cannot extend drift validity"
            )
        if candidate.drift_state is not previous.drift_state:
            raise ModelLifecycleError(
                "unchanged drift observation cannot relabel drift state"
            )
        if candidate.evidence_refs != previous.evidence_refs:
            raise ModelLifecycleError(
                "unchanged drift observation cannot replace drift evidence"
            )
    elif candidate.evidence_refs == previous.evidence_refs:
        raise ModelLifecycleError(
            "new drift observation requires new drift evidence"
        )

    if previous.lifecycle_state is ModelLifecycleState.RETIRED:
        if candidate.lifecycle_state is not ModelLifecycleState.RETIRED:
            raise ModelLifecycleError("retired model lifecycle is terminal")
    elif previous.lifecycle_state is ModelLifecycleState.EXPIRED:
        if candidate.lifecycle_state not in {
            ModelLifecycleState.EXPIRED,
            ModelLifecycleState.RETIRED,
        }:
            raise ModelLifecycleError("expired model cannot become active again")
    elif previous.lifecycle_state is ModelLifecycleState.QUARANTINED:
        if candidate.lifecycle_state is ModelLifecycleState.ACTIVE:
            if candidate.drift_state is not ModelDriftState.STABLE:
                raise ModelLifecycleError(
                    "quarantine release requires stable drift evidence"
                )
            if candidate_drift_observed <= previous_drift_observed:
                raise ModelLifecycleError(
                    "quarantine release requires a newer drift observation"
                )


def _reason_tuple(reasons: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(reasons))


@dataclass(frozen=True, slots=True, init=False)
class ModelEligibility:
    """Derived lifecycle prerequisite report; never independent authority."""

    model_version_id: str
    model_artifact_sha256: str
    lifecycle_revision: int
    lifecycle_fingerprint_sha256: str
    evaluated_at: str
    eligible: bool
    reasons: tuple[str, ...]

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError(
            "ModelEligibility is derived; use evaluate_model_eligibility()"
        )

    @classmethod
    def _from_evaluation(
        cls,
        *,
        model_version_id: str,
        model_artifact_sha256: str,
        lifecycle_revision: int,
        lifecycle_fingerprint_sha256: str,
        evaluated_at: str,
        reasons: tuple[str, ...],
        _construction_token: object,
    ) -> "ModelEligibility":
        if _construction_token is not _ELIGIBILITY_CONSTRUCTION_TOKEN:
            raise ModelLifecycleError(
                "ModelEligibility may only be issued by the canonical evaluator"
            )
        _canonical_text("model_version_id", model_version_id, maximum_bytes=256)
        _canonical_sha256("model_artifact_sha256", model_artifact_sha256)
        _positive_int("lifecycle_revision", lifecycle_revision)
        _canonical_sha256(
            "lifecycle_fingerprint_sha256", lifecycle_fingerprint_sha256
        )
        evaluated_text, _ = _canonical_utc("evaluated_at", evaluated_at)
        if type(reasons) is not tuple:
            raise ModelLifecycleError("eligibility reasons must be a tuple")
        validated_reasons = tuple(
            _canonical_text("eligibility_reason", reason, maximum_bytes=160)
            for reason in reasons
        )
        if len(set(validated_reasons)) != len(validated_reasons):
            raise ModelLifecycleError("eligibility reasons must be unique")

        instance = object.__new__(cls)
        object.__setattr__(instance, "model_version_id", model_version_id)
        object.__setattr__(
            instance, "model_artifact_sha256", model_artifact_sha256
        )
        object.__setattr__(instance, "lifecycle_revision", lifecycle_revision)
        object.__setattr__(
            instance,
            "lifecycle_fingerprint_sha256",
            lifecycle_fingerprint_sha256,
        )
        object.__setattr__(instance, "evaluated_at", evaluated_text)
        object.__setattr__(instance, "eligible", not validated_reasons)
        object.__setattr__(instance, "reasons", validated_reasons)
        return instance


def _registry_instant(value: object, name: str) -> datetime:
    if type(value) is not str or not value:
        raise ModelLifecycleError(f"{name} must be a registry timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelLifecycleError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ModelLifecycleError(f"{name} must be UTC")
    return parsed


def _canonical_stable_drift_reasons(
    revision: ModelLifecycleRevision,
    *,
    evaluated_at: str,
    scientific_registry: ScientificRegistry | None,
) -> tuple[str, ...]:
    """Re-resolve the canonical #532 finding before positive STABLE eligibility."""

    if scientific_registry is None:
        return ("canonical_drift_finding_required",)

    monitor = DriftMonitor(scientific_registry)
    canonical_findings = []
    for evidence_ref in revision.evidence_refs:
        try:
            finding_entry, _, _ = monitor.require_canonical_finding(
                evidence_ref,
                as_of=evaluated_at,
            )
        except DriftControlError:
            continue
        canonical_findings.append(finding_entry)

    if not canonical_findings:
        return ("canonical_drift_finding_required",)
    if len(canonical_findings) != 1:
        return ("canonical_drift_finding_ambiguous",)

    finding_entry = canonical_findings[0]
    finding = finding_entry.payload
    if finding.get("model_version_id") != revision.model_version_id:
        return ("canonical_drift_model_mismatch",)

    model_entry = scientific_registry.get("ModelVersion", revision.model_version_id)
    if model_entry is None:
        return ("canonical_drift_model_missing",)
    if model_entry.payload.get("artifact_sha256") != revision.model_artifact_sha256:
        return ("canonical_drift_artifact_mismatch",)
    if _registry_instant(model_entry.available_at, "ModelVersion.available_at") > _registry_instant(
        evaluated_at, "evaluated_at"
    ):
        return ("canonical_drift_model_not_yet_available",)

    finding_evaluated_at = finding.get("evaluated_at")
    if _registry_instant(
        finding_evaluated_at, "DriftFinding.evaluated_at"
    ) != _canonical_utc("drift_observed_at", revision.drift_observed_at)[1]:
        return ("canonical_drift_observation_time_mismatch",)

    reference_id = finding.get("reference_id")
    if type(reference_id) is not str or not reference_id:
        return ("canonical_drift_reference_missing",)
    same_lineage = tuple(
        entry
        for entry in monitor.list_findings(as_of=evaluated_at)
        if entry.payload.get("model_version_id") == revision.model_version_id
        and entry.payload.get("reference_id") == reference_id
    )
    if not same_lineage:
        return ("canonical_drift_finding_required",)
    latest_available_at = max(
        _registry_instant(entry.available_at, "DriftFinding.available_at")
        for entry in same_lineage
    )
    latest = tuple(
        entry
        for entry in same_lineage
        if _registry_instant(entry.available_at, "DriftFinding.available_at")
        == latest_available_at
    )
    if len(latest) != 1:
        return ("canonical_drift_latest_finding_ambiguous",)
    latest_entry = latest[0]
    if latest_entry.record_id != finding_entry.record_id:
        try:
            monitor.require_canonical_finding(
                latest_entry.record_id,
                as_of=evaluated_at,
            )
        except DriftControlError:
            return ("canonical_latest_drift_finding_invalid",)
        return ("canonical_drift_finding_superseded",)

    state = finding.get("state")
    if state == DriftState.DRIFT_DETECTED.value:
        return ("canonical_drift_detected",)
    if state == DriftState.INSUFFICIENT_EVIDENCE.value:
        return ("canonical_drift_insufficient_evidence",)
    if state != DriftState.NO_DRIFT.value:
        return ("canonical_drift_state_invalid",)
    return ()


def evaluate_model_eligibility(
    revision: ModelLifecycleRevision,
    *,
    evaluated_at: str,
    scientific_registry: ScientificRegistry | None = None,
) -> ModelEligibility:
    """Fail closed at expiry boundaries and require canonical stable-drift truth."""

    if type(revision) is not ModelLifecycleRevision:
        raise TypeError("revision must be an exact ModelLifecycleRevision")
    if scientific_registry is not None and type(scientific_registry) is not ScientificRegistry:
        raise TypeError("scientific_registry must be an exact ScientificRegistry")
    evaluated_text, evaluated = _canonical_utc("evaluated_at", evaluated_at)
    _, created = _canonical_utc("created_at", revision.created_at)
    _, revision_updated = _canonical_utc("updated_at", revision.updated_at)
    _, knowledge_until = _canonical_utc(
        "knowledge_valid_until", revision.knowledge_valid_until
    )
    _, drift_observed = _canonical_utc(
        "drift_observed_at", revision.drift_observed_at
    )
    _, drift_until = _canonical_utc("drift_valid_until", revision.drift_valid_until)

    reasons: list[str] = []
    if evaluated < created:
        reasons.append("before_model_creation")
    if evaluated < revision_updated:
        reasons.append("lifecycle_revision_not_yet_available")
    if revision.lifecycle_state is not ModelLifecycleState.ACTIVE:
        reasons.append(f"lifecycle_{revision.lifecycle_state.value}")
    if evaluated >= knowledge_until:
        reasons.append("knowledge_expired")
    if evaluated < drift_observed:
        reasons.append("drift_not_yet_observed")
    if revision.drift_state is not ModelDriftState.STABLE:
        reasons.append(f"drift_{revision.drift_state.value}")
    if evaluated >= drift_until:
        reasons.append("drift_evidence_expired")
    if not revision.evidence_refs:
        reasons.append("missing_lifecycle_evidence")

    if (
        not reasons
        and revision.lifecycle_state is ModelLifecycleState.ACTIVE
        and revision.drift_state is ModelDriftState.STABLE
    ):
        reasons.extend(
            _canonical_stable_drift_reasons(
                revision,
                evaluated_at=evaluated_text,
                scientific_registry=scientific_registry,
            )
        )

    normalized = _reason_tuple(reasons)
    return ModelEligibility._from_evaluation(
        model_version_id=revision.model_version_id,
        model_artifact_sha256=revision.model_artifact_sha256,
        lifecycle_revision=revision.revision,
        lifecycle_fingerprint_sha256=revision.fingerprint_sha256,
        evaluated_at=evaluated_text,
        reasons=normalized,
        _construction_token=_ELIGIBILITY_CONSTRUCTION_TOKEN,
    )
