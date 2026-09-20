"""Bind one canonical economic decision to PAPER execution before reservation.

The execution ledger is the ordering authority for PAPER attempts.  This module
commits the exact, already-durable DecisionLedger record into that append-only
chain before ``RUN_RESERVED`` can be published.  Campaign admission can then
prove publication order mechanically instead of accepting a matching decision
that was written after execution.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping

from .decision_ledger import DecisionLedgerIntegrityError, JsonlDecisionLedger
from .paper_execution_adoption import (
    PaperExecutionAdoptionResult,
    PaperExecutionAdoptionRuntime,
    PreparedPaperExecution,
)
from .paper_execution_reality import (
    ObservedPaperExecution,
    PaperExecutionEvidenceRegistry,
    PaperExecutionLedger,
    PaperExecutionStateError,
)

_DECISION_ORIGIN_EVENT = "DECISION_ORIGIN_BOUND"
_DECISION_ORIGIN_FIELDS = frozenset(
    {"decision_id", "decision_record_sha256", "decision_record_ordinal"}
)


class PaperExecutionDecisionOriginError(RuntimeError):
    """The PAPER run cannot prove a canonical pre-execution decision origin."""


@dataclass(frozen=True, slots=True)
class PaperExecutionDecisionOrigin:
    run_id: str
    decision_id: str
    decision_record_sha256: str
    decision_record_ordinal: int


def _record_sha256(record) -> str:
    payload = json.dumps(
        record.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _origin_payload(event: object) -> Mapping[str, object]:
    if not isinstance(event, Mapping):
        raise PaperExecutionDecisionOriginError("decision-origin event is invalid")
    payload = event.get("payload")
    if not isinstance(payload, Mapping) or set(payload) != _DECISION_ORIGIN_FIELDS:
        raise PaperExecutionDecisionOriginError("decision-origin payload schema is invalid")
    decision_id = payload.get("decision_id")
    digest = payload.get("decision_record_sha256")
    ordinal = payload.get("decision_record_ordinal")
    if type(decision_id) is not str or not decision_id or decision_id.strip() != decision_id:
        raise PaperExecutionDecisionOriginError("decision-origin decision_id is invalid")
    if (
        type(digest) is not str
        or len(digest) != 64
        or digest.lower() != digest
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        raise PaperExecutionDecisionOriginError("decision-origin digest is invalid")
    if type(ordinal) is not int or ordinal < 0:
        raise PaperExecutionDecisionOriginError("decision-origin ordinal is invalid")
    return payload


def bind_preexecution_decision_origin(
    *,
    runtime: PaperExecutionAdoptionRuntime,
    decision_ledger: JsonlDecisionLedger,
    prepared: PreparedPaperExecution,
    trigger_id: str,
) -> PaperExecutionDecisionOrigin:
    """Commit the exact DecisionLedger record before any run reservation.

    Retry is idempotent only when the already-bound record is byte-identical and
    remains before ``RUN_RESERVED`` in the execution ledger chain.  A legacy or
    otherwise already-started run without this commitment fails closed rather
    than being retroactively blessed.
    """

    if type(runtime) is not PaperExecutionAdoptionRuntime:
        raise TypeError("runtime must be exact PaperExecutionAdoptionRuntime")
    if type(decision_ledger) is not JsonlDecisionLedger:
        raise TypeError("decision_ledger must be exact JsonlDecisionLedger")
    if type(runtime.ledger) is not PaperExecutionLedger:
        raise TypeError("runtime ledger must be exact PaperExecutionLedger")
    if not isinstance(prepared, PreparedPaperExecution):
        raise TypeError("prepared must be PreparedPaperExecution")
    if type(trigger_id) is not str or not trigger_id or trigger_id.strip() != trigger_id:
        raise ValueError("trigger_id must be canonical non-empty text")
    if trigger_id != prepared.execution_plan.decision_id:
        raise PaperExecutionDecisionOriginError(
            "PAPER trigger must equal the execution plan decision identity"
        )

    try:
        records = decision_ledger.verified_records()
    except DecisionLedgerIntegrityError as exc:
        raise PaperExecutionDecisionOriginError(
            "Decision Ledger cannot prove pre-execution origin"
        ) from exc
    matches = [
        (ordinal, record)
        for ordinal, record in enumerate(records)
        if record.decision_id == trigger_id
    ]
    if len(matches) != 1:
        raise PaperExecutionDecisionOriginError(
            "PAPER execution requires exactly one pre-existing canonical decision"
        )
    ordinal, record = matches[0]
    digest = _record_sha256(record)
    run_id = runtime.expected_run_id(prepared, trigger_id)
    expected_payload = {
        "decision_id": trigger_id,
        "decision_record_sha256": digest,
        "decision_record_ordinal": ordinal,
    }

    existing = runtime.ledger.events(run_id)
    origins = [
        event for event in existing if event.get("event_type") == _DECISION_ORIGIN_EVENT
    ]
    if not origins:
        if existing:
            raise PaperExecutionDecisionOriginError(
                "cannot bind decision origin after PAPER execution has started"
            )
        runtime.ledger._append_event(
            event_type=_DECISION_ORIGIN_EVENT,
            run_id=run_id,
            key=f"{run_id}:decision-origin",
            payload=expected_payload,
        )

    events = runtime.ledger.events(run_id)
    origins = [event for event in events if event.get("event_type") == _DECISION_ORIGIN_EVENT]
    if len(origins) != 1:
        raise PaperExecutionDecisionOriginError(
            "PAPER run must contain exactly one decision-origin commitment"
        )
    origin = origins[0]
    payload = _origin_payload(origin)
    if dict(payload) != expected_payload:
        raise PaperExecutionDecisionOriginError(
            "PAPER run decision-origin commitment conflicts with Decision Ledger"
        )
    reservations = [event for event in events if event.get("event_type") == "RUN_RESERVED"]
    if reservations and origin.get("sequence", -1) >= reservations[0].get("sequence", -1):
        raise PaperExecutionDecisionOriginError(
            "decision origin was not committed before PAPER reservation"
        )

    return PaperExecutionDecisionOrigin(
        run_id=run_id,
        decision_id=trigger_id,
        decision_record_sha256=digest,
        decision_record_ordinal=ordinal,
    )


def execute_with_decision_origin(
    *,
    runtime: PaperExecutionAdoptionRuntime,
    decision_ledger: JsonlDecisionLedger,
    prepared: PreparedPaperExecution,
    trigger_id: str,
    started_at: str,
    materialize_exposure: bool,
    observations: Mapping[str, ObservedPaperExecution] | None = None,
    evidence_registry: PaperExecutionEvidenceRegistry | None = None,
    suspended_action_ids: frozenset[str] = frozenset(),
) -> PaperExecutionAdoptionResult:
    """Supported PAPER execution path when later campaign admission is possible."""

    bind_preexecution_decision_origin(
        runtime=runtime,
        decision_ledger=decision_ledger,
        prepared=prepared,
        trigger_id=trigger_id,
    )
    try:
        return runtime.execute(
            prepared=prepared,
            trigger_id=trigger_id,
            started_at=started_at,
            materialize_exposure=materialize_exposure,
            observations=observations,
            evidence_registry=evidence_registry,
            suspended_action_ids=suspended_action_ids,
        )
    except PaperExecutionStateError:
        raise
