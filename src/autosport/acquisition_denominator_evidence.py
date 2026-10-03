from __future__ import annotations

import hashlib
import inspect
import json
import threading
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from .causal_collector import CollectorDeltaStore
from .evaluation_universe import EvaluationUniverse
from .scheduled_source_universe import resolve_scheduled_source_universe
from .source_universe_commitment import SourceUniverseCommitment


_SCHEMA = "autosport.acquisition_denominator_evidence"
_SCHEMA_VERSION = 1
_CANONICAL_CYCLE_EVIDENCE = CollectorDeltaStore.collector_cycle_evidence
_CANONICAL_CYCLE_EVIDENCE_DESCRIPTOR = inspect.getattr_static(
    CollectorDeltaStore, "collector_cycle_evidence"
)
_CANONICAL_CYCLE_EVIDENCE_CODE = _CANONICAL_CYCLE_EVIDENCE.__code__


class AcquisitionDenominatorEvidenceError(ValueError):
    """Scheduled acquisition evidence cannot support the frozen denominator."""


class AcquisitionCoverageStrength(StrEnum):
    """Strongest generic coverage claim this composition can make."""

    INCOMPLETE_OR_UNKNOWN = "INCOMPLETE_OR_UNKNOWN"
    SCHEDULED_CYCLE_WINDOW_COMPLETE = "SCHEDULED_CYCLE_WINDOW_COMPLETE"


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AcquisitionDenominatorEvidenceError(
            "acquisition denominator evidence is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _instant(value: object, name: str) -> datetime:
    if type(value) is not str or not value or value.strip() != value:
        raise AcquisitionDenominatorEvidenceError(
            f"{name} must be a non-empty canonical timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AcquisitionDenominatorEvidenceError(
            f"{name} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AcquisitionDenominatorEvidenceError(
            f"{name} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _expected_path(store: CollectorDeltaStore, expected_store_path: str | Path) -> Path:
    if isinstance(expected_store_path, str):
        if not expected_store_path or expected_store_path.strip() != expected_store_path:
            raise AcquisitionDenominatorEvidenceError(
                "expected_store_path must be a non-empty trimmed path"
            )
        expected = Path(expected_store_path)
    elif isinstance(expected_store_path, Path):
        expected = expected_store_path
    else:
        raise TypeError("expected_store_path must be str or Path")
    if getattr(store, "path", None) != expected:
        raise AcquisitionDenominatorEvidenceError(
            "collector store path does not match product-expected authority path"
        )
    return expected


def _cycle_evidence(
    store: CollectorDeltaStore,
    *,
    source_id: str,
    start_cycle_seq: int,
    end_cycle_seq: int,
) -> tuple[dict[str, object], ...]:
    if (
        inspect.getattr_static(CollectorDeltaStore, "collector_cycle_evidence", None)
        is not _CANONICAL_CYCLE_EVIDENCE_DESCRIPTOR
        or _CANONICAL_CYCLE_EVIDENCE.__code__ is not _CANONICAL_CYCLE_EVIDENCE_CODE
    ):
        raise AcquisitionDenominatorEvidenceError(
            "canonical collector cycle reader is class/code-rebound"
        )
    if "collector_cycle_evidence" in vars(store):
        raise TypeError("canonical collector cycle reader is instance-rebound")
    evidence = _CANONICAL_CYCLE_EVIDENCE(
        store,
        source_id=source_id,
        start_cycle_seq=start_cycle_seq,
        end_cycle_seq=end_cycle_seq,
    )
    if (
        inspect.getattr_static(CollectorDeltaStore, "collector_cycle_evidence", None)
        is not _CANONICAL_CYCLE_EVIDENCE_DESCRIPTOR
        or _CANONICAL_CYCLE_EVIDENCE.__code__ is not _CANONICAL_CYCLE_EVIDENCE_CODE
    ):
        raise AcquisitionDenominatorEvidenceError(
            "canonical collector cycle reader changed during resolution"
        )
    if type(evidence) is not tuple:
        raise AcquisitionDenominatorEvidenceError(
            "collector cycle evidence must be an exact tuple"
        )
    return evidence


def _cycle_window_sha256(
    *,
    source_id: str,
    start_cycle_seq: int,
    end_cycle_seq: int,
    cycles: tuple[dict[str, object], ...],
) -> str:
    return _digest(
        {
            "schema": "autosport.collector_source_universe_cycle_window",
            "schema_version": 1,
            "source_id": source_id,
            "start_cycle_seq": start_cycle_seq,
            "end_cycle_seq": end_cycle_seq,
            "cycles": list(cycles),
        }
    )


@dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
class AcquisitionDenominatorEvidence:
    """Product-issued acquisition coverage bound to one exact frozen universe.

    This is a read-only composition over existing collector schedule/source-universe
    authorities. It creates no collector, evaluation store, campaign registry, or
    provider completeness authority.
    """

    schema_version: int
    evaluation_universe_sha256: str
    evaluation_membership_sha256: str
    source_id: str
    run_id: str
    schedule_id: str
    schedule_commitment_sha256: str
    source_universe_commitment_sha256: str
    source_cycle_evidence_sha256: str
    expected_slot_count: int
    success_nonempty_count: int
    success_empty_count: int
    provider_unavailable_count: int
    local_failure_count: int
    stop_requested_count: int
    pending_or_late_terminal_count: int
    terminal_after_freeze_count: int
    observed_delta_occurrence_count: int
    observed_unique_delta_count: int
    scheduled_start_coverage_complete: bool
    acquisition_complete_by_universe_freeze: bool
    coverage_strength: AcquisitionCoverageStrength
    positive_evaluation_lineage_complete: bool
    external_provider_universe_complete: bool
    promotion_ready: bool
    evidence_sha256: str

    def __new__(cls, *args: object, **kwargs: object) -> "AcquisitionDenominatorEvidence":
        raise TypeError(
            "AcquisitionDenominatorEvidence is product-issued; "
            "use build_acquisition_denominator_evidence"
        )

    @classmethod
    def _issue(
        cls, payload: dict[str, object]
    ) -> "AcquisitionDenominatorEvidence":
        obj = object.__new__(cls)
        for name, value in payload.items():
            object.__setattr__(obj, name, value)
        return obj

    def to_payload(self, *, include_digest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": _SCHEMA,
            "schema_version": self.schema_version,
            "evaluation_universe_sha256": self.evaluation_universe_sha256,
            "evaluation_membership_sha256": self.evaluation_membership_sha256,
            "source_id": self.source_id,
            "run_id": self.run_id,
            "schedule_id": self.schedule_id,
            "schedule_commitment_sha256": self.schedule_commitment_sha256,
            "source_universe_commitment_sha256": (
                self.source_universe_commitment_sha256
            ),
            "source_cycle_evidence_sha256": self.source_cycle_evidence_sha256,
            "expected_slot_count": self.expected_slot_count,
            "success_nonempty_count": self.success_nonempty_count,
            "success_empty_count": self.success_empty_count,
            "provider_unavailable_count": self.provider_unavailable_count,
            "local_failure_count": self.local_failure_count,
            "stop_requested_count": self.stop_requested_count,
            "pending_or_late_terminal_count": self.pending_or_late_terminal_count,
            "terminal_after_freeze_count": self.terminal_after_freeze_count,
            "observed_delta_occurrence_count": self.observed_delta_occurrence_count,
            "observed_unique_delta_count": self.observed_unique_delta_count,
            "scheduled_start_coverage_complete": self.scheduled_start_coverage_complete,
            "acquisition_complete_by_universe_freeze": (
                self.acquisition_complete_by_universe_freeze
            ),
            "coverage_strength": self.coverage_strength.value,
            "positive_evaluation_lineage_complete": (
                self.positive_evaluation_lineage_complete
            ),
            "external_provider_universe_complete": (
                self.external_provider_universe_complete
            ),
            "promotion_ready": self.promotion_ready,
        }
        if include_digest:
            payload["evidence_sha256"] = self.evidence_sha256
        return payload


def _classify_as_of_freeze(
    cycles: tuple[dict[str, object], ...],
    *,
    frozen_at: str,
) -> dict[str, object]:
    frozen = _instant(frozen_at, "evaluation universe frozen_at")
    success_nonempty = 0
    success_empty = 0
    provider_unavailable = 0
    local_failure = 0
    stop_requested = 0
    pending = 0
    terminal_after_freeze = 0
    observed_delta_ids: list[str] = []

    for cycle in cycles:
        if type(cycle) is not dict:
            raise AcquisitionDenominatorEvidenceError(
                "collector cycle evidence item is malformed"
            )
        terminal = cycle.get("terminal")
        if terminal is None:
            pending += 1
            continue
        if type(terminal) is not dict:
            raise AcquisitionDenominatorEvidenceError(
                "collector cycle terminal evidence is malformed"
            )
        completed = _instant(terminal.get("completed_at"), "cycle completed_at")
        if completed > frozen:
            pending += 1
            terminal_after_freeze += 1
            continue

        status = terminal.get("status")
        deltas = terminal.get("observed_deltas")
        if type(deltas) is not list:
            raise AcquisitionDenominatorEvidenceError(
                "collector cycle observed_deltas evidence is malformed"
            )
        for item in deltas:
            if type(item) is not dict or set(item) != {"delta_id", "payload_sha256"}:
                raise AcquisitionDenominatorEvidenceError(
                    "collector cycle delta evidence is malformed"
                )
            delta_id = item.get("delta_id")
            if type(delta_id) is not str or not delta_id:
                raise AcquisitionDenominatorEvidenceError(
                    "collector cycle delta identity is malformed"
                )
            observed_delta_ids.append(delta_id)

        if status == "SUCCESS":
            if deltas:
                success_nonempty += 1
            else:
                success_empty += 1
        elif status == "PROVIDER_UNAVAILABLE":
            if deltas:
                raise AcquisitionDenominatorEvidenceError(
                    "provider-unavailable cycle cannot carry positive observations"
                )
            provider_unavailable += 1
        elif status == "LOCAL_FAILURE":
            local_failure += 1
        elif status == "STOP_REQUESTED":
            stop_requested += 1
        else:
            raise AcquisitionDenominatorEvidenceError(
                "collector cycle terminal status is unsupported"
            )

    return {
        "success_nonempty_count": success_nonempty,
        "success_empty_count": success_empty,
        "provider_unavailable_count": provider_unavailable,
        "local_failure_count": local_failure,
        "stop_requested_count": stop_requested,
        "pending_or_late_terminal_count": pending,
        "terminal_after_freeze_count": terminal_after_freeze,
        "observed_delta_occurrence_count": len(observed_delta_ids),
        "observed_unique_delta_count": len(set(observed_delta_ids)),
    }


def build_acquisition_denominator_evidence(
    store: CollectorDeltaStore,
    candidate_source_universe: SourceUniverseCommitment,
    evaluation_universe: EvaluationUniverse,
    *,
    expected_store_path: str | Path,
    expected_source_id: str,
    expected_run_id: str,
    expected_start_slot_ordinal: int,
    expected_end_slot_ordinal: int,
) -> AcquisitionDenominatorEvidence:
    """Bind frozen evaluation membership to exact scheduled acquisition truth.

    Later terminal/retry evidence remains visible in the collector history but is
    classified as unresolved for an earlier frozen universe. Successful complete
    empty cycles remain explicit observed-zero coverage; failures never become empty.
    """

    if type(store) is not CollectorDeltaStore:
        raise TypeError("store must be the exact canonical CollectorDeltaStore")
    if type(candidate_source_universe) is not SourceUniverseCommitment:
        raise TypeError(
            "candidate_source_universe must be an exact SourceUniverseCommitment"
        )
    if type(evaluation_universe) is not EvaluationUniverse:
        raise TypeError("evaluation_universe must be an exact EvaluationUniverse")
    expected_path = _expected_path(store, expected_store_path)
    if (
        type(expected_source_id) is not str
        or not expected_source_id
        or expected_source_id.strip() != expected_source_id
    ):
        raise AcquisitionDenominatorEvidenceError(
            "expected_source_id must be non-empty canonical text"
        )
    if evaluation_universe.intake_snapshot.source_id != expected_source_id:
        raise AcquisitionDenominatorEvidenceError(
            "evaluation universe source does not match scheduled acquisition authority"
        )

    scheduled = resolve_scheduled_source_universe(
        store,
        candidate_source_universe,
        expected_store_path=expected_path,
        expected_source_id=expected_source_id,
        expected_run_id=expected_run_id,
        expected_start_slot_ordinal=expected_start_slot_ordinal,
        expected_end_slot_ordinal=expected_end_slot_ordinal,
    )
    cycles = _cycle_evidence(
        store,
        source_id=expected_source_id,
        start_cycle_seq=scheduled.start_cycle_seq,
        end_cycle_seq=scheduled.end_cycle_seq,
    )
    if len(cycles) != scheduled.expected_slot_count:
        raise AcquisitionDenominatorEvidenceError(
            "scheduled acquisition cycle evidence is incomplete"
        )
    cycle_sha256 = _cycle_window_sha256(
        source_id=expected_source_id,
        start_cycle_seq=scheduled.start_cycle_seq,
        end_cycle_seq=scheduled.end_cycle_seq,
        cycles=cycles,
    )
    if cycle_sha256 != scheduled.source_cycle_evidence_sha256:
        raise AcquisitionDenominatorEvidenceError(
            "collector cycle evidence changed after source-universe resolution"
        )
    _expected_path(store, expected_path)

    as_of = _classify_as_of_freeze(
        cycles,
        frozen_at=evaluation_universe.frozen_at,
    )
    partition_count = sum(
        int(as_of[name])
        for name in (
            "success_nonempty_count",
            "success_empty_count",
            "provider_unavailable_count",
            "local_failure_count",
            "stop_requested_count",
            "pending_or_late_terminal_count",
        )
    )
    if partition_count != scheduled.expected_slot_count:
        raise AcquisitionDenominatorEvidenceError(
            "acquisition denominator partition does not equal frozen schedule"
        )
    complete_by_freeze = (
        scheduled.scheduled_start_coverage_complete
        and scheduled.late_start_count == 0
        and int(as_of["provider_unavailable_count"]) == 0
        and int(as_of["local_failure_count"]) == 0
        and int(as_of["stop_requested_count"]) == 0
        and int(as_of["pending_or_late_terminal_count"]) == 0
    )

    coverage_strength = (
        AcquisitionCoverageStrength.SCHEDULED_CYCLE_WINDOW_COMPLETE
        if complete_by_freeze
        else AcquisitionCoverageStrength.INCOMPLETE_OR_UNKNOWN
    )

    payload: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "evaluation_universe_sha256": evaluation_universe.universe_sha256,
        "evaluation_membership_sha256": evaluation_universe.membership_sha256,
        "source_id": expected_source_id,
        "run_id": scheduled.run_id,
        "schedule_id": scheduled.schedule_id,
        "schedule_commitment_sha256": scheduled.schedule_commitment_sha256,
        "source_universe_commitment_sha256": (
            scheduled.source_universe_commitment_sha256
        ),
        "source_cycle_evidence_sha256": cycle_sha256,
        "expected_slot_count": scheduled.expected_slot_count,
        **as_of,
        "scheduled_start_coverage_complete": (
            scheduled.scheduled_start_coverage_complete
        ),
        "acquisition_complete_by_universe_freeze": complete_by_freeze,
        "coverage_strength": coverage_strength,
        # Current main has no exact scheduled-cycle -> provider-evidence ->
        # EvaluationUniverse positive-row lineage. Active #2050/#1185 own that
        # adjacent authority. Keep this false mechanically rather than inferring
        # linkage from source_id, timestamps, counts, or matching hashes.
        "positive_evaluation_lineage_complete": False,
        "external_provider_universe_complete": False,
        "promotion_ready": False,
    }
    digest_payload = {"schema": _SCHEMA, **payload}
    evidence_sha256 = _digest(digest_payload)
    evidence = AcquisitionDenominatorEvidence._issue(
        {**payload, "evidence_sha256": evidence_sha256}
    )
    return evidence


def require_complete_acquisition_coverage(
    evidence: AcquisitionDenominatorEvidence,
) -> AcquisitionDenominatorEvidence:
    """Require structurally valid complete scheduled-acquisition evidence.

    Product issuance identity is enforced by the installed canonical wrapper below.
    This raw implementation is retained only inside that closure.
    """

    if type(evidence) is not AcquisitionDenominatorEvidence:
        raise TypeError("evidence must be exact AcquisitionDenominatorEvidence")
    try:
        expected_digest = _digest(
            {"schema": _SCHEMA, **evidence.to_payload(include_digest=False)}
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise AcquisitionDenominatorEvidenceError(
            "acquisition denominator evidence structure is invalid"
        ) from exc
    if evidence.evidence_sha256 != expected_digest:
        raise AcquisitionDenominatorEvidenceError(
            "acquisition denominator evidence structure/digest is invalid"
        )
    if not evidence.acquisition_complete_by_universe_freeze:
        raise AcquisitionDenominatorEvidenceError(
            "frozen evaluation universe has failed, stopped, pending, late, "
            "or post-cutoff scheduled acquisition coverage"
        )
    return evidence


def _install_acquisition_denominator_authority() -> None:
    lock = threading.RLock()
    issued: dict[
        int,
        tuple[weakref.ReferenceType[AcquisitionDenominatorEvidence], str],
    ] = {}
    raw_build = build_acquisition_denominator_evidence
    raw_require = require_complete_acquisition_coverage

    def authoritative_build(*args: object, **kwargs: object) -> AcquisitionDenominatorEvidence:
        evidence = raw_build(*args, **kwargs)
        identity = id(evidence)
        digest = evidence.evidence_sha256

        def clear(
            reference: weakref.ReferenceType[AcquisitionDenominatorEvidence],
            *,
            _identity: int = identity,
        ) -> None:
            with lock:
                record = issued.get(_identity)
                if record is not None and record[0] is reference:
                    issued.pop(_identity, None)

        reference = weakref.ref(evidence, clear)
        with lock:
            issued[identity] = (reference, digest)
        return evidence

    def authoritative_require(
        evidence: AcquisitionDenominatorEvidence,
    ) -> AcquisitionDenominatorEvidence:
        if type(evidence) is not AcquisitionDenominatorEvidence:
            raise TypeError("evidence must be exact AcquisitionDenominatorEvidence")
        try:
            current_digest = evidence.evidence_sha256
        except (AttributeError, TypeError, ValueError) as exc:
            raise AcquisitionDenominatorEvidenceError(
                "acquisition denominator evidence structure is invalid"
            ) from exc
        with lock:
            record = issued.get(id(evidence))
        if (
            record is None
            or record[0]() is not evidence
            or record[1] != current_digest
        ):
            raise AcquisitionDenominatorEvidenceError(
                "acquisition denominator evidence is not current product-issued authority"
            )
        return raw_require(evidence)

    globals()["build_acquisition_denominator_evidence"] = authoritative_build
    globals()["require_complete_acquisition_coverage"] = authoritative_require


_install_acquisition_denominator_authority()
del _install_acquisition_denominator_authority


__all__ = [
    "AcquisitionCoverageStrength",
    "AcquisitionDenominatorEvidence",
    "AcquisitionDenominatorEvidenceError",
    "build_acquisition_denominator_evidence",
    "require_complete_acquisition_coverage",
]
