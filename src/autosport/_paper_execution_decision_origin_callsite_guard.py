from __future__ import annotations

import inspect
from types import FrameType

from . import _paper_execution_decision_origin as _origin
from . import _paper_value_execution_authority as _paper_value_authority
from . import live_decision_loop as _live_decision_loop
from .decision_ledger import JsonlDecisionLedger
from .paper_execution_adoption import PaperExecutionAdoptionError, PaperExecutionAdoptionRuntime


def _origin_from_direct_caller(
    runtime: PaperExecutionAdoptionRuntime,
    decision_id: str,
    caller: FrameType,
):
    """Resolve origin only when the canonical producer directly calls execute."""

    local = caller.f_locals
    if caller.f_code is _live_decision_loop.PersistentLiveDecisionLoop._persist_plan.__code__:
        owner = local.get("self")
        if type(owner) is not _live_decision_loop.PersistentLiveDecisionLoop:
            raise _origin.PaperExecutionDecisionOriginError(
                "live decision origin requires exact PersistentLiveDecisionLoop authority"
            )
        if getattr(owner, "paper_execution", None) is not runtime:
            raise _origin.PaperExecutionDecisionOriginError(
                "live decision origin runtime binding changed"
            )
        ledger = getattr(owner, "decision_ledger", None)
        if type(ledger) is not JsonlDecisionLedger:
            raise _origin.PaperExecutionDecisionOriginError(
                "live decision origin requires exact JsonlDecisionLedger authority"
            )
        if local.get("decision_id") != decision_id:
            raise _origin.PaperExecutionDecisionOriginError(
                "live decision call-site identity does not match execution plan"
            )
        return _origin.verified_decision_origin(ledger, decision_id)

    if caller.f_code is _paper_value_authority._ORIGINAL_ON_MARKET_EVENT.__code__:
        context = local.get("context")
        agent = local.get("self")
        event = local.get("event")
        if type(agent) is not _paper_value_authority.PaperValueAgent:
            raise _origin.PaperExecutionDecisionOriginError(
                "paper-value origin requires exact PaperValueAgent authority"
            )
        if event is None or agent._material_action_id(context, event) != decision_id:
            raise _origin.PaperExecutionDecisionOriginError(
                "paper-value decision call-site identity does not match execution plan"
            )
        ledger = _origin._exact_context_ledger(runtime, context)
        return _origin.verified_decision_origin(ledger, decision_id)

    if caller.f_code is _paper_value_authority._resume_durable_paper_value.__code__:
        context = local.get("context")
        record = local.get("record")
        if (
            type(record) is not _paper_value_authority.DecisionRecord
            or record.decision_id != decision_id
        ):
            raise _origin.PaperExecutionDecisionOriginError(
                "paper-value recovery call-site identity does not match execution plan"
            )
        ledger = _origin._exact_context_ledger(runtime, context)
        return _origin.verified_decision_origin(ledger, decision_id)

    return None


def _matching_product_ancestor(
    runtime: PaperExecutionAdoptionRuntime,
    decision_id: str,
    frame: FrameType | None,
) -> bool:
    """Detect authority-bearing ancestors only to reject nested execution.

    Ancestor presence never grants authority. It is used solely as a re-entrancy
    fence so arbitrary hooks/callbacks running inside a canonical producer cannot
    create an originless reservation that would poison the subsequent intended
    product execution.
    """

    current = frame
    while current is not None:
        local = current.f_locals
        if current.f_code is _live_decision_loop.PersistentLiveDecisionLoop._persist_plan.__code__:
            owner = local.get("self")
            if (
                type(owner) is _live_decision_loop.PersistentLiveDecisionLoop
                and getattr(owner, "paper_execution", None) is runtime
                and local.get("decision_id") == decision_id
            ):
                return True
        elif current.f_code is _paper_value_authority._ORIGINAL_ON_MARKET_EVENT.__code__:
            context = local.get("context")
            agent = local.get("self")
            event = local.get("event")
            if (
                type(agent) is _paper_value_authority.PaperValueAgent
                and getattr(context, "paper_execution", None) is runtime
                and event is not None
                and agent._material_action_id(context, event) == decision_id
            ):
                return True
        elif current.f_code is _paper_value_authority._resume_durable_paper_value.__code__:
            context = local.get("context")
            record = local.get("record")
            if (
                getattr(context, "paper_execution", None) is runtime
                and type(record) is _paper_value_authority.DecisionRecord
                and record.decision_id == decision_id
            ):
                return True
        current = current.f_back
    return False


def _execute_with_exact_product_callsite(
    self: PaperExecutionAdoptionRuntime,
    *,
    prepared,
    trigger_id: str,
    started_at: str,
    materialize_exposure: bool,
    observations=None,
    evidence_registry=None,
    suspended_action_ids: frozenset[str] = frozenset(),
):
    decision_id = getattr(getattr(prepared, "execution_plan", None), "decision_id", None)
    if type(decision_id) is not str or not decision_id:
        return _origin._ORIGINAL_RUNTIME_EXECUTE(
            self,
            prepared=prepared,
            trigger_id=trigger_id,
            started_at=started_at,
            materialize_exposure=materialize_exposure,
            observations=observations,
            evidence_registry=evidence_registry,
            suspended_action_ids=suspended_action_ids,
        )

    current = inspect.currentframe()
    caller = None
    try:
        caller = current.f_back if current is not None else None
        origin = (
            None
            if caller is None
            else _origin_from_direct_caller(self, decision_id, caller)
        )
        if origin is None:
            ancestor = None if caller is None else caller.f_back
            if _matching_product_ancestor(self, decision_id, ancestor):
                raise PaperExecutionAdoptionError(
                    "nested PAPER execution cannot inherit product decision-origin authority"
                )
            # Isolated execution/replay callers remain valid but carry no product
            # origin and therefore cannot satisfy downstream product admission.
            return _origin._ORIGINAL_RUNTIME_EXECUTE(
                self,
                prepared=prepared,
                trigger_id=trigger_id,
                started_at=started_at,
                materialize_exposure=materialize_exposure,
                observations=observations,
                evidence_registry=evidence_registry,
                suspended_action_ids=suspended_action_ids,
            )
        if trigger_id != origin.decision_id:
            raise PaperExecutionAdoptionError(
                "product execution trigger does not match verified DecisionLedger origin"
            )

        token = _origin._DECISION_ORIGIN.set(origin)
        try:
            return _origin._ORIGINAL_RUNTIME_EXECUTE(
                self,
                prepared=prepared,
                trigger_id=trigger_id,
                started_at=started_at,
                materialize_exposure=materialize_exposure,
                observations=observations,
                evidence_registry=evidence_registry,
                suspended_action_ids=suspended_action_ids,
            )
        finally:
            _origin._DECISION_ORIGIN.reset(token)
    finally:
        # Frame references form cycles; release them deterministically.
        del current
        del caller


def _install() -> None:
    if getattr(PaperExecutionAdoptionRuntime, "_autosport_decision_origin_callsite_guard", False):
        return
    PaperExecutionAdoptionRuntime.execute = _execute_with_exact_product_callsite
    PaperExecutionAdoptionRuntime._autosport_decision_origin_callsite_guard = True


_install()


__all__ = []
