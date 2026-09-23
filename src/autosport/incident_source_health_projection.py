"""Authoritative provider-health evidence projection into the incident register.

This module is a read-only composition boundary. SourceHealthStore remains the
only provider/source-health authority and incident_risk_register remains the
only incident schema/lifecycle authority. The adapter derives immutable evidence
references from the validated durable source-health history; caller-authored
severity, closure booleans, error strings, and quality-flag text never become
incident authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Final

from .incident_risk_register import (
    IncidentRiskEntry,
    RegisterEntryKind,
    RiskEvidenceState,
    RiskSeverity,
    RiskStatus,
    derive_occurrence_entry_id,
    validate_successor,
)
from .ingestion_health import SourceHealthStore, parse_source_timestamp


_EVIDENCE_SCHEMA: Final = "autosport.source-health-incident-evidence"
_EVIDENCE_SCHEMA_VERSION: Final = 1
_EVIDENCE_PREFIX: Final = "source-health-evidence:"
_SOURCE_SCOPE_PREFIX: Final = "source-health-scope:"
_IMPAIRED_STATUSES: Final = frozenset({"degraded", "failed"})


class SourceHealthIncidentProjectionError(RuntimeError):
    """Provider-health evidence cannot be represented without weakening causality."""


@dataclass(frozen=True, slots=True)
class SourceHealthEvidence:
    """Secret-safe identity for one validated durable health transition."""

    transition_order: int
    recorded_at: str
    status: str
    evidence_ref: str
    source_scope_ref: str


def _utf8(value: str, *, field_name: str) -> bytes:
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SourceHealthIncidentProjectionError(
            f"{field_name} is not valid UTF-8"
        ) from exc


def _source_scope_ref(source_id: str) -> str:
    digest = hashlib.sha256(_utf8(source_id, field_name="source_id")).hexdigest()
    return f"{_SOURCE_SCOPE_PREFIX}{digest}"


def _canonical_evidence_ref(
    *,
    source_id: str,
    transition_order: int,
    recorded_at: str,
    status: str,
) -> str:
    # Bind only structural authority needed by this projection. SourceHealthStore
    # may retain provider exception/cursor text for diagnostics; copying or hashing
    # those free-text values into operator evidence would create an unnecessary
    # secret-bearing derivative. transition_order + canonical source/time/status
    # uniquely identifies the validated transition used by this adapter.
    payload = {
        "schema": _EVIDENCE_SCHEMA,
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "source_id": source_id,
        "transition_order": transition_order,
        "recorded_at": recorded_at,
        "status": status,
    }
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SourceHealthIncidentProjectionError(
            "source-health evidence is not canonically serializable"
        ) from exc
    return f"{_EVIDENCE_PREFIX}{hashlib.sha256(encoded).hexdigest()}"


def _canonical_incident_time(recorded_at: str) -> str:
    """Map accepted source-health time into the register's canonical UTC form."""

    return parse_source_timestamp(recorded_at).isoformat()


def _evidence_stream(
    store: SourceHealthStore,
    source_id: str,
) -> tuple[SourceHealthEvidence, ...]:
    if not isinstance(store, SourceHealthStore):
        raise TypeError("store must be a SourceHealthStore")
    if not isinstance(source_id, str) or not source_id or source_id.strip() != source_id:
        raise ValueError("source_id must be a non-empty trimmed string")

    raw = store._read()
    schema_version = raw["schema_version"]
    source_scope = _source_scope_ref(source_id)

    if schema_version == 1:
        state_payload = raw["sources"].get(source_id)
        if state_payload is None:
            return ()
        state = store._state_from_payload(state_payload)
        recorded_at = store._transition_at(state)
        if recorded_at is None:
            raise SourceHealthIncidentProjectionError(
                "persisted source health lacks transition evidence time"
            )
        rows = ((1, recorded_at, state_payload),)
    else:
        entries = raw["history"].get(source_id, ())
        rows = tuple(
            (
                (
                    entry["transition_order"]
                    if schema_version == 3
                    else index
                ),
                entry["recorded_at"],
                entry["state"],
            )
            for index, entry in enumerate(entries, start=1)
        )

    evidence: list[SourceHealthEvidence] = []
    previous_order = 0
    previous_time = None
    for transition_order, recorded_at, state_payload in rows:
        if (
            isinstance(transition_order, bool)
            or not isinstance(transition_order, int)
            or transition_order != previous_order + 1
        ):
            raise SourceHealthIncidentProjectionError(
                "source-health transition order is not contiguous"
            )
        state = store._state_from_payload(
            state_payload,
            normalize_failed_flags=False,
        )
        canonical_time = _canonical_incident_time(recorded_at)
        parsed_time = parse_source_timestamp(canonical_time)
        if previous_time is not None and parsed_time < previous_time:
            raise SourceHealthIncidentProjectionError(
                "source-health evidence time moved backwards"
            )
        evidence.append(
            SourceHealthEvidence(
                transition_order=transition_order,
                recorded_at=canonical_time,
                status=state.status,
                evidence_ref=_canonical_evidence_ref(
                    source_id=source_id,
                    transition_order=transition_order,
                    recorded_at=recorded_at,
                    status=state.status,
                ),
                source_scope_ref=source_scope,
            )
        )
        previous_order = transition_order
        previous_time = parsed_time
    return tuple(evidence)


def resolve_source_health_evidence(
    store: SourceHealthStore,
    *,
    source_id: str,
    evidence_ref: str,
) -> SourceHealthEvidence:
    """Re-resolve one product-derived evidence reference from canonical health history."""

    if (
        not isinstance(evidence_ref, str)
        or not evidence_ref.startswith(_EVIDENCE_PREFIX)
        or len(evidence_ref) != len(_EVIDENCE_PREFIX) + 64
    ):
        raise SourceHealthIncidentProjectionError(
            "invalid source-health evidence reference"
        )
    for evidence in _evidence_stream(store, source_id):
        if evidence.evidence_ref == evidence_ref:
            return evidence
    raise SourceHealthIncidentProjectionError(
        "source-health evidence reference is absent from canonical history"
    )


def _severity(status: str) -> RiskSeverity:
    if status == "degraded":
        return RiskSeverity.MEDIUM
    if status == "failed":
        return RiskSeverity.HIGH
    raise SourceHealthIncidentProjectionError(
        "only impaired source-health states have incident severity"
    )


def _open_summary(status: str) -> str:
    if status == "degraded":
        return (
            "Канонічний стан джерела даних деградований. Залежні рішення мають "
            "залишатися fail-closed згідно з власними правилами здоров'я джерела."
        )
    if status == "failed":
        return (
            "Канонічний стан джерела даних недоступний. Відсутність нових даних "
            "не є доказом нульового результату або відновлення."
        )
    raise SourceHealthIncidentProjectionError("unsupported impaired source-health state")


def _entry(
    *,
    occurrence_ref: str,
    source_scope_ref: str,
    revision: int,
    opened_at: str,
    updated_at: str,
    current_status: str,
    evidence_refs: tuple[str, ...],
    previous_severity: RiskSeverity | None = None,
) -> IncidentRiskEntry:
    affected_components = ("provider_health", source_scope_ref)
    entry_id = derive_occurrence_entry_id(
        kind=RegisterEntryKind.INCIDENT,
        affected_components=affected_components,
        occurrence_evidence_refs=(occurrence_ref,),
    )

    if current_status == "healthy":
        if previous_severity is None:
            raise SourceHealthIncidentProjectionError(
                "healthy closure requires a preceding impaired incident"
            )
        return IncidentRiskEntry(
            entry_id=entry_id,
            revision=revision,
            kind=RegisterEntryKind.INCIDENT,
            severity=previous_severity,
            status=RiskStatus.RESOLVED,
            evidence_state=RiskEvidenceState.VERIFIED,
            opened_at=opened_at,
            updated_at=updated_at,
            title="Стан джерела даних відновлено",
            summary=(
                "Канонічний SourceHealthStore зафіксував здоровий стан після "
                "попереднього погіршення."
            ),
            mitigation=(
                "Відновлення підтверджено наступним канонічним успішним transition "
                "SourceHealthStore без прапорців якості."
            ),
            residual_risk=(
                "Історія інциденту зберігається; цей запис не надає execution, "
                "release або readiness authority."
            ),
            affected_components=affected_components,
            occurrence_evidence_refs=(occurrence_ref,),
            evidence_refs=evidence_refs,
            model_version_ids=(),
            requires_operator_action=False,
        )

    severity = _severity(current_status)
    return IncidentRiskEntry(
        entry_id=entry_id,
        revision=revision,
        kind=RegisterEntryKind.INCIDENT,
        severity=severity,
        status=RiskStatus.OPEN,
        evidence_state=RiskEvidenceState.VERIFIED,
        opened_at=opened_at,
        updated_at=updated_at,
        title="Проблема зі станом джерела даних",
        summary=_open_summary(current_status),
        mitigation="",
        residual_risk=(
            "До канонічного здорового transition стан джерела лишається "
            "операційно невизначеним або деградованим."
        ),
        affected_components=affected_components,
        occurrence_evidence_refs=(occurrence_ref,),
        evidence_refs=evidence_refs,
        model_version_ids=(),
        requires_operator_action=True,
    )


def project_source_health_incidents(
    store: SourceHealthStore,
    *,
    source_id: str,
) -> tuple[IncidentRiskEntry, ...]:
    """Project canonical source-health history into immutable register revisions.

    A contiguous degraded/failed interval is one incident occurrence. The first
    degraded/failed transition is immutable occurrence evidence. Every later
    impaired transition adds evidence, and only a later canonical healthy
    transition may close that occurrence.

    IncidentRiskEntry currently orders successor revisions only by strictly
    increasing UTC timestamps, while SourceHealthStore schema v3 correctly
    supports equal-time transitions using transition_order. Rather than invent
    timestamp precision or rewrite a persisted revision, this adapter fails closed
    when two transitions belonging to one incident share the same evidence time.
    """

    evidence_stream = _evidence_stream(store, source_id)
    revisions: list[IncidentRiskEntry] = []
    current: IncidentRiskEntry | None = None
    occurrence_ref: str | None = None
    opened_at: str | None = None
    cumulative_refs: set[str] = set()

    for evidence in evidence_stream:
        if evidence.status in _IMPAIRED_STATUSES:
            if current is None or current.status is RiskStatus.RESOLVED:
                occurrence_ref = evidence.evidence_ref
                opened_at = evidence.recorded_at
                cumulative_refs = {evidence.evidence_ref}
                current = _entry(
                    occurrence_ref=occurrence_ref,
                    source_scope_ref=evidence.source_scope_ref,
                    revision=1,
                    opened_at=opened_at,
                    updated_at=evidence.recorded_at,
                    current_status=evidence.status,
                    evidence_refs=tuple(sorted(cumulative_refs)),
                )
                revisions.append(current)
                continue

            assert occurrence_ref is not None
            assert opened_at is not None
            if evidence.recorded_at <= current.updated_at:
                raise SourceHealthIncidentProjectionError(
                    "equal-time source-health successors cannot be represented "
                    "without inventing incident evidence time"
                )
            cumulative_refs.add(evidence.evidence_ref)
            candidate = _entry(
                occurrence_ref=occurrence_ref,
                source_scope_ref=evidence.source_scope_ref,
                revision=current.revision + 1,
                opened_at=opened_at,
                updated_at=evidence.recorded_at,
                current_status=evidence.status,
                evidence_refs=tuple(sorted(cumulative_refs)),
            )
            validate_successor(current, candidate)
            current = candidate
            revisions.append(current)
            continue

        if evidence.status == "healthy":
            if current is None or current.status is RiskStatus.RESOLVED:
                continue
            assert occurrence_ref is not None
            assert opened_at is not None
            if evidence.recorded_at <= current.updated_at:
                raise SourceHealthIncidentProjectionError(
                    "equal-time source-health closure cannot be represented "
                    "without inventing incident evidence time"
                )
            cumulative_refs.add(evidence.evidence_ref)
            candidate = _entry(
                occurrence_ref=occurrence_ref,
                source_scope_ref=evidence.source_scope_ref,
                revision=current.revision + 1,
                opened_at=opened_at,
                updated_at=evidence.recorded_at,
                current_status="healthy",
                evidence_refs=tuple(sorted(cumulative_refs)),
                previous_severity=current.severity,
            )
            validate_successor(current, candidate)
            current = candidate
            revisions.append(current)
            continue

        if evidence.status == "unknown":
            if current is not None and current.status is not RiskStatus.RESOLVED:
                raise SourceHealthIncidentProjectionError(
                    "unknown source health cannot close an active incident"
                )
            continue

        raise SourceHealthIncidentProjectionError(
            f"unsupported source-health status: {evidence.status}"
        )

    return tuple(revisions)


def validate_source_health_incident_evidence(
    store: SourceHealthStore,
    *,
    source_id: str,
    entry: IncidentRiskEntry,
) -> None:
    """Re-resolve one register entry against canonical provider-health history.

    This validator is intentionally narrower than the generic incident lifecycle:
    it proves only provider/source-health evidence. Acknowledgement or mitigation
    may change presentation/lifecycle metadata elsewhere, but neither can replace
    canonical health evidence or manufacture a recovery.
    """

    if not isinstance(entry, IncidentRiskEntry):
        raise TypeError("entry must be an IncidentRiskEntry")
    if entry.kind is not RegisterEntryKind.INCIDENT:
        raise SourceHealthIncidentProjectionError(
            "source-health evidence can validate only incident entries"
        )

    stream = _evidence_stream(store, source_id)
    by_ref = {evidence.evidence_ref: evidence for evidence in stream}
    if len(entry.occurrence_evidence_refs) != 1:
        raise SourceHealthIncidentProjectionError(
            "source-health incident requires exactly one occurrence evidence reference"
        )
    occurrence_ref = entry.occurrence_evidence_refs[0]
    occurrence = by_ref.get(occurrence_ref)
    if occurrence is None:
        raise SourceHealthIncidentProjectionError(
            "source-health occurrence evidence is absent from canonical history"
        )
    if occurrence.status not in _IMPAIRED_STATUSES:
        raise SourceHealthIncidentProjectionError(
            "source-health occurrence must begin with an impaired transition"
        )

    expected_components = ("provider_health", occurrence.source_scope_ref)
    if entry.affected_components != expected_components:
        raise SourceHealthIncidentProjectionError(
            "incident affected_components do not match canonical source scope"
        )
    if entry.opened_at != occurrence.recorded_at:
        raise SourceHealthIncidentProjectionError(
            "incident opened_at does not match occurrence evidence time"
        )
    if entry.evidence_state is not RiskEvidenceState.VERIFIED:
        raise SourceHealthIncidentProjectionError(
            "canonical source-health projection requires verified evidence state"
        )

    resolved_evidence: list[SourceHealthEvidence] = []
    for evidence_ref in entry.evidence_refs:
        evidence = by_ref.get(evidence_ref)
        if evidence is None:
            raise SourceHealthIncidentProjectionError(
                "incident evidence is absent from canonical source-health history"
            )
        resolved_evidence.append(evidence)

    if not resolved_evidence:
        raise SourceHealthIncidentProjectionError(
            "source-health incident requires canonical evidence"
        )
    resolved_evidence.sort(key=lambda evidence: evidence.transition_order)
    if resolved_evidence[0].transition_order != occurrence.transition_order:
        raise SourceHealthIncidentProjectionError(
            "source-health incident evidence must begin at occurrence transition"
        )

    last = resolved_evidence[-1]
    expected_segment = tuple(
        evidence
        for evidence in stream
        if occurrence.transition_order
        <= evidence.transition_order
        <= last.transition_order
    )
    if tuple(item.evidence_ref for item in expected_segment) != tuple(
        item.evidence_ref for item in resolved_evidence
    ):
        raise SourceHealthIncidentProjectionError(
            "source-health incident evidence must be a contiguous canonical transition segment"
        )

    healthy_positions = [
        index
        for index, evidence in enumerate(expected_segment)
        if evidence.status == "healthy"
    ]
    if healthy_positions and healthy_positions != [len(expected_segment) - 1]:
        raise SourceHealthIncidentProjectionError(
            "source-health incident evidence cannot continue past its healthy closure"
        )
    if any(
        evidence.status not in _IMPAIRED_STATUSES and evidence.status != "healthy"
        for evidence in expected_segment
    ):
        raise SourceHealthIncidentProjectionError(
            "source-health incident evidence contains unsupported health state"
        )

    impaired = tuple(
        evidence for evidence in expected_segment if evidence.status in _IMPAIRED_STATUSES
    )
    if not impaired:
        raise SourceHealthIncidentProjectionError(
            "source-health incident lacks impaired evidence"
        )
    expected_severity = _severity(impaired[-1].status)
    if entry.severity is not expected_severity:
        raise SourceHealthIncidentProjectionError(
            "incident severity does not match canonical source-health evidence"
        )

    closed = last.status == "healthy"
    if closed:
        if entry.status is not RiskStatus.RESOLVED:
            raise SourceHealthIncidentProjectionError(
                "healthy closure evidence requires resolved incident status"
            )
    elif entry.status in {RiskStatus.RESOLVED, RiskStatus.SUPERSEDED}:
        raise SourceHealthIncidentProjectionError(
            "terminal incident status requires canonical healthy closure evidence"
        )

    if parse_source_timestamp(entry.updated_at) < parse_source_timestamp(last.recorded_at):
        raise SourceHealthIncidentProjectionError(
            "incident updated_at precedes its latest canonical health evidence"
        )


def current_source_health_incident(
    store: SourceHealthStore,
    *,
    source_id: str,
) -> IncidentRiskEntry | None:
    """Return only the currently unresolved provider-health incident, if any."""

    history = project_source_health_incidents(store, source_id=source_id)
    if not history:
        return None
    latest = history[-1]
    if latest.status is RiskStatus.RESOLVED:
        return None
    return latest
