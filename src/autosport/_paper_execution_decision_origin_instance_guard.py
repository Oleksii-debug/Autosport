from __future__ import annotations

import json

from . import _paper_execution_decision_origin as _origin
from .decision_ledger import JsonlDecisionLedger
from .paper_execution_reality import PaperExecutionLedger


_ORIGIN_RESERVE_RUN = PaperExecutionLedger.reserve_run


def _instance_shadows(obj: object, method_name: str) -> bool:
    namespace = getattr(obj, "__dict__", None)
    return isinstance(namespace, dict) and method_name in namespace


def _verified_decision_origin_without_instance_dispatch(
    ledger: JsonlDecisionLedger,
    decision_id: str,
) -> _origin.DecisionRecordOrigin:
    if type(ledger) is not JsonlDecisionLedger:
        raise _origin.PaperExecutionDecisionOriginError(
            "decision origin requires exact JsonlDecisionLedger authority"
        )
    if _instance_shadows(ledger, "verified_snapshot"):
        raise _origin.PaperExecutionDecisionOriginError(
            "decision ledger shadows authority method verified_snapshot"
        )
    if type(decision_id) is not str or not decision_id or decision_id.strip() != decision_id:
        raise ValueError("decision_id must be non-empty canonical text")

    # Invoke the exact class implementation so authority cannot be redirected by
    # per-instance method dispatch after exact-type validation.
    snapshot = JsonlDecisionLedger.verified_snapshot(ledger)
    matches: list[_origin.DecisionRecordOrigin] = []
    for line in snapshot.payload.decode("utf-8").splitlines():
        envelope = json.loads(line)
        record = envelope["record"]
        if record.get("decision_id") != decision_id:
            continue
        matches.append(
            _origin.DecisionRecordOrigin(
                decision_id=decision_id,
                record_sha256=envelope["sha256"],
            )
        )
    if len(matches) != 1:
        raise _origin.PaperExecutionDecisionOriginError(
            "decision origin requires exactly one verified durable DecisionLedger record"
        )
    return matches[0]


def _reserve_run_without_shadowed_append(
    self: PaperExecutionLedger,
    *,
    run_id: str,
    trigger_id: str,
    plan,
    config,
    started_at: str,
    observation_evidence_ids,
) -> None:
    if _origin._DECISION_ORIGIN.get() is not None and _instance_shadows(
        self, "_append_event"
    ):
        raise _origin.PaperExecutionDecisionOriginError(
            "execution ledger shadows authority method _append_event"
        )
    return _ORIGIN_RESERVE_RUN(
        self,
        run_id=run_id,
        trigger_id=trigger_id,
        plan=plan,
        config=config,
        started_at=started_at,
        observation_evidence_ids=observation_evidence_ids,
    )


def _install() -> None:
    if getattr(PaperExecutionLedger, "_autosport_decision_origin_instance_guard", False):
        return
    _origin.verified_decision_origin = _verified_decision_origin_without_instance_dispatch
    PaperExecutionLedger.reserve_run = _reserve_run_without_shadowed_append
    PaperExecutionLedger._autosport_decision_origin_instance_guard = True


_install()


__all__ = []
