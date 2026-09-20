from __future__ import annotations

import json

from . import _paper_execution_decision_origin as _origin
from .decision_ledger import JsonlDecisionLedger
from .paper_execution_reality import (
    PaperExecutionLedger,
    PaperExecutionStateError,
)


_VERIFIED_SNAPSHOT_SENTINEL = "_autosport_decision_origin_pristine_verified_snapshot"
_RESERVE_SENTINEL = "_autosport_decision_origin_pristine_reserve_run"
_APPEND_SENTINEL = "_autosport_decision_origin_pristine_append_event"

# Preserve the exact pre-origin authority methods once. Re-importing/reloading guard
# modules must never capture an already-installed wrapper as its own "original".
if not hasattr(JsonlDecisionLedger, _VERIFIED_SNAPSHOT_SENTINEL):
    setattr(
        JsonlDecisionLedger,
        _VERIFIED_SNAPSHOT_SENTINEL,
        JsonlDecisionLedger.verified_snapshot,
    )
if not hasattr(PaperExecutionLedger, _RESERVE_SENTINEL):
    setattr(
        PaperExecutionLedger,
        _RESERVE_SENTINEL,
        _origin._ORIGINAL_LEDGER_RESERVE,
    )
if not hasattr(PaperExecutionLedger, _APPEND_SENTINEL):
    setattr(
        PaperExecutionLedger,
        _APPEND_SENTINEL,
        PaperExecutionLedger._append_event,
    )

_STABLE_VERIFIED_SNAPSHOT = getattr(JsonlDecisionLedger, _VERIFIED_SNAPSHOT_SENTINEL)
_STABLE_RESERVE_RUN = getattr(PaperExecutionLedger, _RESERVE_SENTINEL)
_STABLE_APPEND_EVENT = getattr(PaperExecutionLedger, _APPEND_SENTINEL)


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

    snapshot = _STABLE_VERIFIED_SNAPSHOT(ledger)
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
    origin = _origin._DECISION_ORIGIN.get()
    if origin is None:
        return _STABLE_RESERVE_RUN(
            self,
            run_id=run_id,
            trigger_id=trigger_id,
            plan=plan,
            config=config,
            started_at=started_at,
            observation_evidence_ids=observation_evidence_ids,
        )

    if type(self) is not PaperExecutionLedger:
        raise _origin.PaperExecutionDecisionOriginError(
            "origin-bound execution requires exact PaperExecutionLedger authority"
        )
    if _instance_shadows(self, "_append_event"):
        raise _origin.PaperExecutionDecisionOriginError(
            "execution ledger shadows authority method _append_event"
        )
    if origin.decision_id != trigger_id or origin.decision_id != plan.decision_id:
        raise PaperExecutionStateError(
            "decision origin does not match execution trigger/plan decision identity"
        )

    payload = {
        "trigger_id": trigger_id,
        "plan_id": plan.plan_id,
        "plan_fingerprint": plan.fingerprint,
        "model_fingerprint": config.fingerprint,
        "started_at": started_at,
        "action_ids": [action.action_id for action in plan.actions],
        "observation_evidence_ids": dict(sorted(observation_evidence_ids.items())),
        "decision_origin": origin.to_dict(),
    }
    _STABLE_APPEND_EVENT(
        self,
        event_type="RUN_RESERVED",
        run_id=run_id,
        key=f"{run_id}:reserve",
        payload=payload,
    )


def _install() -> None:
    _origin.verified_decision_origin = _verified_decision_origin_without_instance_dispatch
    if getattr(PaperExecutionLedger, "_autosport_decision_origin_instance_guard", False):
        return
    PaperExecutionLedger.reserve_run = _reserve_run_without_shadowed_append
    PaperExecutionLedger._autosport_decision_origin_instance_guard = True


_install()


__all__ = []
