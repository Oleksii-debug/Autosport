from __future__ import annotations

from contextvars import ContextVar
import inspect
import json

from . import _paper_execution_decision_origin as _origin
from . import _paper_execution_reality_legacy as _legacy_reality
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
_SEAL_MARKER = "autosport.paper_execution_decision_origin.instance_guard.seal.v2"
_SEAL_PREFIX = "autosport.paper_execution_decision_origin.instance_guard.seal."

# Preserve the exact context identities across importlib.reload. Installed guard
# closures keep these objects as their execution-capability channel; replacing a
# module attribute must never manufacture a new authority channel.
if "_PRODUCT_ORIGIN_RUNTIME" not in globals():
    _PRODUCT_ORIGIN_RUNTIME: ContextVar[PaperExecutionAdoptionRuntime | None] = ContextVar(
        "autosport_paper_execution_product_origin_runtime",
        default=None,
    )
if "_PRODUCT_ORIGIN_CALLSITE_CODE" not in globals():
    _PRODUCT_ORIGIN_CALLSITE_CODE: ContextVar[object | None] = ContextVar(
        "autosport_paper_execution_product_origin_callsite_code",
        default=None,
    )


def _instance_shadows(obj: object, method_name: str) -> bool:
    namespace = getattr(obj, "__dict__", None)
    return isinstance(namespace, dict) and method_name in namespace


def _initial_seal():
    """Re-derive primitive authority from the canonical owning implementations."""

    return (
        _SEAL_MARKER,
        JsonlDecisionLedger.verified_snapshot,
        _legacy_reality.PaperExecutionLedger.reserve_run,
        _legacy_reality.PaperExecutionLedger._append_event,
        _paper_reality.execute_paper_plan.__code__,
    )


def _seal_matches_canonical(value: object) -> bool:
    if type(value) is not tuple or len(value) != 5 or value[0] != _SEAL_MARKER:
        return False
    canonical = _initial_seal()
    return all(value[index] is canonical[index] for index in range(1, len(canonical)))


def _sealed_tuple_from_callable(candidate: object):
    """Recover only an unchanged seal; flag legacy/rebound seal cells for repair."""

    closure = getattr(candidate, "__closure__", None)
    if not closure:
        return None, False
    saw_guard_seal = False
    for cell in closure:
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if (
            type(value) is tuple
            and value
            and type(value[0]) is str
            and value[0].startswith(_SEAL_PREFIX)
        ):
            saw_guard_seal = True
            if _seal_matches_canonical(value):
                return value, False
    return None, saw_guard_seal


def _build_guard(seal):
    # Capture the capability contexts in this installed guard closure. Every
    # authority-bearing call revalidates the executable seal against the primitive
    # implementations owned by DecisionLedger / legacy PAPER ledger / execute-plan
    # code. A reflected closure-cell replacement therefore fails closed immediately,
    # and reload discards/rebuilds the altered wrapper instead of trusting it.
    product_origin_runtime = _PRODUCT_ORIGIN_RUNTIME
    product_origin_callsite_code = _PRODUCT_ORIGIN_CALLSITE_CODE

    def require_canonical_seal() -> None:
        if not _seal_matches_canonical(seal):
            raise _origin.PaperExecutionDecisionOriginError(
                "decision-origin executable authority seal changed"
            )

    def verified_decision_origin_without_instance_dispatch(
        ledger: JsonlDecisionLedger,
        decision_id: str,
    ) -> _origin.DecisionRecordOrigin:
        require_canonical_seal()
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

        require_canonical_seal()
        runtime = product_origin_runtime.get()
        if type(runtime) is not PaperExecutionAdoptionRuntime:
            raise _origin.PaperExecutionDecisionOriginError(
                "decision origin context is not bound to canonical product execution"
            )
        expected_callsite_code = product_origin_callsite_code.get()
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
                or execute_plan_frame.f_code is not seal[4]
                or execute_plan_frame.f_locals.get("ledger") is not ledger
            ):
                raise _origin.PaperExecutionDecisionOriginError(
                    "decision origin reservation bypassed canonical execute_paper_plan"
                )

            saw_product_wrapper = False
            cursor = execute_plan_frame.f_back
            while cursor is not None:
                if (
                    cursor.f_code is expected_callsite_code
                    and cursor.f_locals.get("self") is runtime
                ):
                    saw_product_wrapper = True
                    break
                cursor = cursor.f_back
            if not saw_product_wrapper:
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
            require_canonical_seal()
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
        require_canonical_seal()
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
    recovered_seal, stale_or_tampered = _sealed_tuple_from_callable(
        PaperExecutionLedger.reserve_run
    )
    reinstall = already_installed and stale_or_tampered
    seal = recovered_seal if recovered_seal is not None else _initial_seal()
    if not _seal_matches_canonical(seal):
        raise RuntimeError("decision-origin canonical executable seal is unavailable")

    # These are compatibility/debug mirrors only. Authority paths validate and use
    # the re-derived canonical seal, never these writable mirrors.
    setattr(JsonlDecisionLedger, _VERIFIED_SNAPSHOT_SENTINEL, seal[1])
    setattr(PaperExecutionLedger, _RESERVE_SENTINEL, seal[2])
    setattr(PaperExecutionLedger, _APPEND_SENTINEL, seal[3])
    runtime_execute_code = getattr(
        PaperExecutionAdoptionRuntime,
        _RUNTIME_EXECUTE_CODE_SENTINEL,
        _origin._ORIGINAL_RUNTIME_EXECUTE.__code__,
    )
    setattr(
        PaperExecutionAdoptionRuntime,
        _RUNTIME_EXECUTE_CODE_SENTINEL,
        runtime_execute_code,
    )
    setattr(PaperExecutionAdoptionRuntime, _EXECUTE_PLAN_CODE_SENTINEL, seal[4])

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
    _STABLE_RUNTIME_EXECUTE_CODE = runtime_execute_code
    _EXECUTE_PAPER_PLAN_CODE = seal[4]
    (
        _verified_decision_origin_without_instance_dispatch,
        _require_canonical_product_reservation_path,
        _CanonicalReservationView,
        _reserve_run_without_shadowed_append,
    ) = _build_guard(seal)

    _origin.verified_decision_origin = _verified_decision_origin_without_instance_dispatch
    if already_installed and not reinstall:
        return
    PaperExecutionLedger.reserve_run = _reserve_run_without_shadowed_append
    PaperExecutionLedger._autosport_decision_origin_instance_guard = True


_install()


__all__ = []
