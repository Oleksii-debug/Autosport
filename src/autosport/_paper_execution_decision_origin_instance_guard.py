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
_CALLSITE_EXECUTE_CODE_SENTINEL = (
    "_autosport_decision_origin_pristine_product_callsite_code"
)
_SEAL_MARKER = "autosport.paper_execution_decision_origin.instance_guard.seal.v1"

_PRODUCT_ORIGIN_RUNTIME: ContextVar[PaperExecutionAdoptionRuntime | None] = ContextVar(
    "autosport_paper_execution_product_origin_runtime",
    default=None,
)


def _instance_shadows(obj: object, method_name: str) -> bool:
    namespace = getattr(obj, "__dict__", None)
    return isinstance(namespace, dict) and method_name in namespace


def _sealed_tuple_from_callable(candidate: object):
    closure = getattr(candidate, "__closure__", None)
    if not closure:
        return None
    for cell in closure:
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if type(value) is tuple and len(value) == 6 and value[0] == _SEAL_MARKER:
            return value
    return None


def _initial_seal():
    return (
        _SEAL_MARKER,
        JsonlDecisionLedger.verified_snapshot,
        _origin._ORIGINAL_LEDGER_RESERVE,
        PaperExecutionLedger._append_event,
        _origin._ORIGINAL_RUNTIME_EXECUTE.__code__,
        _paper_reality.execute_paper_plan.__code__,
    )


def _build_guard(seal):
    def verified_decision_origin_without_instance_dispatch(
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

        snapshot = seal[1](ledger)
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

    def require_canonical_product_reservation_path(
        ledger: PaperExecutionLedger,
    ) -> None:
        """Reject caller-injected ambient origin outside the exact product path."""

        runtime = _PRODUCT_ORIGIN_RUNTIME.get()
        if type(runtime) is not PaperExecutionAdoptionRuntime:
            raise _origin.PaperExecutionDecisionOriginError(
                "decision origin context is not bound to canonical product execution"
            )
        expected_callsite_code = getattr(
            PaperExecutionAdoptionRuntime,
            _CALLSITE_EXECUTE_CODE_SENTINEL,
            None,
        )
        if expected_callsite_code is None:
            raise _origin.PaperExecutionDecisionOriginError(
                "canonical product callsite identity is unavailable"
            )

        current = inspect.currentframe()
        reserve_frame = None
        execute_plan_frame = None
        cursor = None
        try:
            reserve_frame = current.f_back if current is not None else None
            execute_plan_frame = reserve_frame.f_back if reserve_frame is not None else None
            if (
                execute_plan_frame is None
                or execute_plan_frame.f_code is not seal[5]
                or execute_plan_frame.f_locals.get("ledger") is not ledger
            ):
                raise _origin.PaperExecutionDecisionOriginError(
                    "decision origin reservation bypassed canonical execute_paper_plan"
                )

            saw_stable_runtime = False
            saw_product_wrapper = False
            cursor = execute_plan_frame.f_back
            while cursor is not None:
                if cursor.f_code is seal[4] and cursor.f_locals.get("self") is runtime:
                    saw_stable_runtime = True
                if (
                    cursor.f_code is expected_callsite_code
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

    class CanonicalReservationView:
        """Use canonical reserve validation while pinning the durable append sink."""

        __slots__ = ("_ledger", "_origin")

        def __init__(
            self,
            ledger: PaperExecutionLedger,
            origin: _origin.DecisionRecordOrigin,
        ) -> None:
            self._ledger = ledger
            self._origin = origin

        def _append_event(
            self,
            *,
            event_type: str,
            run_id: str,
            key: str,
            payload,
        ) -> None:
            if event_type != "RUN_RESERVED":
                raise _origin.PaperExecutionDecisionOriginError(
                    "canonical reserve path emitted unexpected event type"
                )
            if type(payload) is not dict or "decision_origin" in payload:
                raise _origin.PaperExecutionDecisionOriginError(
                    "canonical reserve payload cannot predeclare decision origin"
                )
            bound_payload = dict(payload)
            bound_payload["decision_origin"] = self._origin.to_dict()
            seal[3](
                self._ledger,
                event_type=event_type,
                run_id=run_id,
                key=key,
                payload=bound_payload,
            )

    def reserve_run_without_shadowed_append(
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
            return seal[2](
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
        require_canonical_product_reservation_path(self)
        if origin.decision_id != trigger_id or origin.decision_id != plan.decision_id:
            raise PaperExecutionStateError(
                "decision origin does not match execution trigger/plan decision identity"
            )

        return seal[2](
            CanonicalReservationView(self, origin),
            run_id=run_id,
            trigger_id=trigger_id,
            plan=plan,
            config=config,
            started_at=started_at,
            observation_evidence_ids=observation_evidence_ids,
        )

    return (
        verified_decision_origin_without_instance_dispatch,
        require_canonical_product_reservation_path,
        CanonicalReservationView,
        reserve_run_without_shadowed_append,
    )


def _install() -> None:
    already_installed = bool(
        getattr(PaperExecutionLedger, "_autosport_decision_origin_instance_guard", False)
    )
    seal = _sealed_tuple_from_callable(PaperExecutionLedger.reserve_run)
    if already_installed and seal is None:
        raise RuntimeError("decision-origin instance guard executable seal is unavailable")
    if seal is None:
        seal = _initial_seal()

    # These names remain available for compatibility/tests, but no authority path
    # reads them. Reload repairs any tampering from the closure-held executable seal.
    setattr(JsonlDecisionLedger, _VERIFIED_SNAPSHOT_SENTINEL, seal[1])
    setattr(PaperExecutionLedger, _RESERVE_SENTINEL, seal[2])
    setattr(PaperExecutionLedger, _APPEND_SENTINEL, seal[3])
    setattr(PaperExecutionAdoptionRuntime, _RUNTIME_EXECUTE_CODE_SENTINEL, seal[4])
    setattr(PaperExecutionAdoptionRuntime, _EXECUTE_PLAN_CODE_SENTINEL, seal[5])

    global _STABLE_VERIFIED_SNAPSHOT
    global _STABLE_RESERVE_RUN
    global _STABLE_APPEND_EVENT
    global _STABLE_RUNTIME_EXECUTE_CODE
    global _EXECUTE_PAPER_PLAN_CODE
    global _verified_decision_origin_without_instance_dispatch
    global _require_canonical_product_reservation_path
    global _CanonicalReservationView
    global _reserve_run_without_shadowed_append
    _STABLE_VERIFIED_SNAPSHOT = seal[1]
    _STABLE_RESERVE_RUN = seal[2]
    _STABLE_APPEND_EVENT = seal[3]
    _STABLE_RUNTIME_EXECUTE_CODE = seal[4]
    _EXECUTE_PAPER_PLAN_CODE = seal[5]
    (
        _verified_decision_origin_without_instance_dispatch,
        _require_canonical_product_reservation_path,
        _CanonicalReservationView,
        _reserve_run_without_shadowed_append,
    ) = _build_guard(seal)

    _origin.verified_decision_origin = _verified_decision_origin_without_instance_dispatch
    if already_installed:
        return
    PaperExecutionLedger.reserve_run = _reserve_run_without_shadowed_append
    PaperExecutionLedger._autosport_decision_origin_instance_guard = True


_install()


__all__ = []
