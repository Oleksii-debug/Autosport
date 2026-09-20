from __future__ import annotations

from contextvars import ContextVar
import inspect
import json

from . import _paper_execution_decision_origin as _origin
from . import paper_execution_reality as _paper_reality
from .decision_ledger import JsonlDecisionLedger
from .paper_execution_adoption import PaperExecutionAdoptionRuntime
from .paper_execution_reality import (
    PaperExecutionLedger,
    PaperExecutionStateError,
)


_VERIFIED_SNAPSHOT_SENTINEL = "_autosport_decision_origin_pristine_verified_snapshot"
_RESERVE_SENTINEL = "_autosport_decision_origin_pristine_reserve_run"
_APPEND_SENTINEL = "_autosport_decision_origin_pristine_append_event"
_RUNTIME_EXECUTE_CODE_SENTINEL = (
    "_autosport_decision_origin_instance_guard_pristine_runtime_execute_code"
)
_EXECUTE_PLAN_CODE_SENTINEL = (
    "_autosport_decision_origin_instance_guard_pristine_execute_plan_code"
)

# A verified DecisionRecordOrigin is evidence, not capability. Only the exact
# canonical product execute wrapper may arm this runtime identity while it calls
# through the stable adoption implementation into execute_paper_plan.
_PRODUCT_ORIGIN_RUNTIME: ContextVar[PaperExecutionAdoptionRuntime | None] = ContextVar(
    "autosport_paper_execution_product_origin_runtime",
    default=None,
)

# Freeze executable identities once. Guard-module reloads must never recapture an
# already-wrapped runtime method or redefine the authority path from mutable code.
if not hasattr(PaperExecutionAdoptionRuntime, _RUNTIME_EXECUTE_CODE_SENTINEL):
    setattr(
        PaperExecutionAdoptionRuntime,
        _RUNTIME_EXECUTE_CODE_SENTINEL,
        _origin._ORIGINAL_RUNTIME_EXECUTE.__code__,
    )
if not hasattr(PaperExecutionAdoptionRuntime, _EXECUTE_PLAN_CODE_SENTINEL):
    setattr(
        PaperExecutionAdoptionRuntime,
        _EXECUTE_PLAN_CODE_SENTINEL,
        _paper_reality.execute_paper_plan.__code__,
    )
_STABLE_RUNTIME_EXECUTE_CODE = getattr(
    PaperExecutionAdoptionRuntime,
    _RUNTIME_EXECUTE_CODE_SENTINEL,
)
_EXECUTE_PAPER_PLAN_CODE = getattr(
    PaperExecutionAdoptionRuntime,
    _EXECUTE_PLAN_CODE_SENTINEL,
)

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


def _require_canonical_product_reservation_path(
    ledger: PaperExecutionLedger,
) -> None:
    """Reject caller-injected ambient origin outside the exact product path."""

    runtime = _PRODUCT_ORIGIN_RUNTIME.get()
    if type(runtime) is not PaperExecutionAdoptionRuntime:
        raise _origin.PaperExecutionDecisionOriginError(
            "decision origin context is not bound to canonical product execution"
        )

    # Import lazily because this guard is installed before the call-site guard.
    from . import _paper_execution_decision_origin_callsite_guard as _callsite_guard

    current = inspect.currentframe()
    reserve_frame = None
    execute_plan_frame = None
    cursor = None
    try:
        reserve_frame = current.f_back if current is not None else None
        execute_plan_frame = (
            reserve_frame.f_back if reserve_frame is not None else None
        )
        if (
            execute_plan_frame is None
            or execute_plan_frame.f_code is not _EXECUTE_PAPER_PLAN_CODE
            or execute_plan_frame.f_locals.get("ledger") is not ledger
        ):
            raise _origin.PaperExecutionDecisionOriginError(
                "decision origin reservation bypassed canonical execute_paper_plan"
            )

        saw_stable_runtime = False
        saw_product_wrapper = False
        cursor = execute_plan_frame.f_back
        while cursor is not None:
            if (
                cursor.f_code is _STABLE_RUNTIME_EXECUTE_CODE
                and cursor.f_locals.get("self") is runtime
            ):
                saw_stable_runtime = True
            if (
                cursor.f_code
                is _callsite_guard._execute_with_exact_product_callsite.__code__
                and cursor.f_locals.get("self") is runtime
            ):
                saw_product_wrapper = True
            cursor = cursor.f_back
        if not saw_stable_runtime or not saw_product_wrapper:
            raise _origin.PaperExecutionDecisionOriginError(
                "decision origin reservation lacks canonical product execution ancestry"
            )
    finally:
        del current
        del reserve_frame
        del execute_plan_frame
        del cursor


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
    _require_canonical_product_reservation_path(self)
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
