"""Canonical drift-finding projection into the incident/model-risk register.

DriftMonitor and ScientificRegistry remain the scientific evidence authority.
incident_risk_register remains the model-risk schema/lifecycle authority. This
adapter only composes the two after DriftMonitor re-proves a persisted finding.
It never grants model promotion, execution, financial, release, or recovery
authority.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

from .drift_control import (
    DriftControlError,
    DriftKind,
    DriftMonitor,
    DriftState,
)
from .incident_risk_register import (
    IncidentRiskEntry,
    RegisterEntryKind,
    RiskEvidenceState,
    RiskSeverity,
    RiskStatus,
    derive_occurrence_entry_id,
)


_EVIDENCE_PREFIX: Final = "drift-finding-evidence:"
_HEX: Final = frozenset("0123456789abcdef")


class DriftIncidentProjectionError(RuntimeError):
    """Canonical drift evidence cannot safely support the requested register claim."""


def _canonical_utc(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise DriftIncidentProjectionError(f"{name} must be non-empty canonical text")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DriftIncidentProjectionError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DriftIncidentProjectionError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()


def _sha256(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise DriftIncidentProjectionError(f"{name} must be SHA-256 text")
    canonical = value.lower()
    if (
        value != canonical
        or len(canonical) != 64
        or any(character not in _HEX for character in canonical)
    ):
        raise DriftIncidentProjectionError(f"{name} must be canonical SHA-256 hex")
    return canonical


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise DriftIncidentProjectionError(f"{name} must be non-empty canonical text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise DriftIncidentProjectionError(f"{name} must be valid UTF-8") from exc
    return value


def _evidence_ref(*, finding_id: str, evidence_sha256: str) -> str:
    finding = _sha256(finding_id, name="finding_id")
    evidence = _sha256(evidence_sha256, name="evidence_sha256")
    return f"{_EVIDENCE_PREFIX}{finding}:{evidence}"


def _parse_evidence_ref(value: object) -> tuple[str, str]:
    if not isinstance(value, str) or not value.startswith(_EVIDENCE_PREFIX):
        raise DriftIncidentProjectionError("invalid drift finding evidence reference")
    tail = value[len(_EVIDENCE_PREFIX) :]
    parts = tail.split(":")
    if len(parts) != 2:
        raise DriftIncidentProjectionError("invalid drift finding evidence reference")
    return (
        _sha256(parts[0], name="evidence finding_id"),
        _sha256(parts[1], name="evidence_sha256"),
    )


def _severity(drift_kind: DriftKind) -> RiskSeverity:
    if drift_kind is DriftKind.COVARIATE:
        return RiskSeverity.MEDIUM
    if drift_kind in {
        DriftKind.FORECAST_PERFORMANCE,
        DriftKind.EXECUTION_MARKET_STATE,
    }:
        return RiskSeverity.HIGH
    raise DriftIncidentProjectionError("unsupported drift kind")


def _affected_components(drift_kind: DriftKind) -> tuple[str, ...]:
    return tuple(
        sorted(
            (
                "drift_control",
                f"drift_kind:{drift_kind.value.lower()}",
            )
        )
    )


def _canonical_finding(
    monitor: DriftMonitor,
    *,
    finding_id: str,
    as_of: str,
) -> dict[str, object]:
    if not isinstance(monitor, DriftMonitor):
        raise TypeError("monitor must be a DriftMonitor")
    finding_id = _sha256(finding_id, name="finding_id")
    try:
        finding_entry, _reference_entry, _observation_entry = (
            monitor.require_canonical_finding(finding_id, as_of=as_of)
        )
    except DriftControlError as exc:
        raise DriftIncidentProjectionError(
            "canonical drift finding re-resolution failed"
        ) from exc
    payload = finding_entry.payload
    if not isinstance(payload, dict):
        raise DriftIncidentProjectionError("canonical drift finding payload is invalid")
    return payload


def project_canonical_drift_model_risk(
    monitor: DriftMonitor,
    *,
    finding_id: str,
    as_of: str,
) -> IncidentRiskEntry | None:
    """Project one canonical detected-drift finding into an OPEN model-risk entry.

    NO_DRIFT and INSUFFICIENT_EVIDENCE are not silently reinterpreted as drift.
    This adapter deliberately has no recovery authority: a later non-alarm does not
    resolve an earlier model-risk occurrence.
    """

    finding = _canonical_finding(
        monitor,
        finding_id=finding_id,
        as_of=as_of,
    )
    try:
        state = DriftState(_text(finding.get("state"), name="finding.state"))
        drift_kind = DriftKind(
            _text(finding.get("drift_kind"), name="finding.drift_kind")
        )
    except ValueError as exc:
        raise DriftIncidentProjectionError(
            "canonical drift finding contains unsupported enum data"
        ) from exc

    if state is not DriftState.DRIFT_DETECTED:
        return None

    canonical_finding_id = _sha256(
        finding.get("finding_id"),
        name="finding.finding_id",
    )
    evidence_sha256 = _sha256(
        finding.get("evidence_sha256"),
        name="finding.evidence_sha256",
    )
    model_version_id = _text(
        finding.get("model_version_id"),
        name="finding.model_version_id",
    )
    evaluated_at = _canonical_utc(
        finding.get("evaluated_at"),
        name="finding.evaluated_at",
    )
    occurrence_ref = _evidence_ref(
        finding_id=canonical_finding_id,
        evidence_sha256=evidence_sha256,
    )
    components = _affected_components(drift_kind)
    entry_id = derive_occurrence_entry_id(
        kind=RegisterEntryKind.MODEL_RISK,
        affected_components=components,
        occurrence_evidence_refs=(occurrence_ref,),
        model_version_ids=(model_version_id,),
    )

    return IncidentRiskEntry(
        entry_id=entry_id,
        revision=1,
        kind=RegisterEntryKind.MODEL_RISK,
        severity=_severity(drift_kind),
        status=RiskStatus.OPEN,
        evidence_state=RiskEvidenceState.VERIFIED,
        opened_at=evaluated_at,
        updated_at=evaluated_at,
        title="Виявлено статистичний дрейф моделі",
        summary=(
            "Канонічний scientific drift finding підтверджує статистичний дрейф. "
            "Це є сигналом model-risk, а не автоматичним дозволом на зміну моделі."
        ),
        mitigation="",
        residual_risk=(
            "Потрібна окрема перевірка, challenger або postmortem згідно з "
            "канонічною scientific policy. Цей запис не надає execution, "
            "financial, promotion, release або readiness authority."
        ),
        affected_components=components,
        occurrence_evidence_refs=(occurrence_ref,),
        evidence_refs=(occurrence_ref,),
        model_version_ids=(model_version_id,),
        requires_operator_action=True,
    )


def validate_canonical_drift_model_risk(
    monitor: DriftMonitor,
    *,
    entry: IncidentRiskEntry,
    as_of: str,
) -> None:
    """Re-prove a persisted model-risk occurrence against ScientificRegistry.

    This validator proves the drift occurrence and severity only. ACKNOWLEDGED or
    MITIGATING remain nonterminal operator/product lifecycle states. RESOLVED and
    SUPERSEDED are rejected because DriftFinding alone has no recovery authority.
    """

    if not isinstance(entry, IncidentRiskEntry):
        raise TypeError("entry must be an IncidentRiskEntry")
    if entry.kind is not RegisterEntryKind.MODEL_RISK:
        raise DriftIncidentProjectionError(
            "drift evidence can validate only model-risk entries"
        )
    if len(entry.occurrence_evidence_refs) != 1:
        raise DriftIncidentProjectionError(
            "drift model-risk requires exactly one occurrence evidence reference"
        )

    finding_id, evidence_sha256 = _parse_evidence_ref(
        entry.occurrence_evidence_refs[0]
    )
    finding = _canonical_finding(
        monitor,
        finding_id=finding_id,
        as_of=as_of,
    )
    canonical_evidence = _sha256(
        finding.get("evidence_sha256"),
        name="finding.evidence_sha256",
    )
    if evidence_sha256 != canonical_evidence:
        raise DriftIncidentProjectionError(
            "model-risk evidence digest does not match canonical drift finding"
        )

    try:
        state = DriftState(_text(finding.get("state"), name="finding.state"))
        drift_kind = DriftKind(
            _text(finding.get("drift_kind"), name="finding.drift_kind")
        )
    except ValueError as exc:
        raise DriftIncidentProjectionError(
            "canonical drift finding contains unsupported enum data"
        ) from exc
    if state is not DriftState.DRIFT_DETECTED:
        raise DriftIncidentProjectionError(
            "model-risk occurrence requires canonical DRIFT_DETECTED evidence"
        )

    model_version_id = _text(
        finding.get("model_version_id"),
        name="finding.model_version_id",
    )
    if entry.model_version_ids != (model_version_id,):
        raise DriftIncidentProjectionError(
            "model-risk model identity does not match canonical drift finding"
        )
    expected_components = _affected_components(drift_kind)
    if entry.affected_components != expected_components:
        raise DriftIncidentProjectionError(
            "model-risk affected_components do not match canonical drift scope"
        )
    if entry.severity is not _severity(drift_kind):
        raise DriftIncidentProjectionError(
            "model-risk severity does not match canonical drift policy"
        )
    if entry.evidence_state is not RiskEvidenceState.VERIFIED:
        raise DriftIncidentProjectionError(
            "canonical drift projection requires verified evidence state"
        )

    evaluated_at = _canonical_utc(
        finding.get("evaluated_at"),
        name="finding.evaluated_at",
    )
    if entry.opened_at != evaluated_at:
        raise DriftIncidentProjectionError(
            "model-risk opened_at does not match canonical drift finding"
        )
    if entry.status in {RiskStatus.RESOLVED, RiskStatus.SUPERSEDED}:
        raise DriftIncidentProjectionError(
            "drift finding alone cannot authorize terminal model-risk status"
        )
