from __future__ import annotations

import inspect

from . import _paper_execution_decision_origin as _origin
from . import _paper_value_execution_authority as _paper_value_authority
from . import live_decision_loop as _live_decision_loop
from .decision_ledger import JsonlDecisionLedger
from .paper_execution_adoption import PaperExecutionAdoptionRuntime


def _resolve_product_origin_from_direct_call(
    runtime: PaperExecutionAdoptionRuntime,
    decision_id: str,
):
    """Resolve product origin only from the exact direct producer call site.

    A canonical producer frame is not an ambient capability. In particular, code
    invoked by ``PersistentLiveDecisionLoop.post_append_hook`` executes while
    ``_persist_plan`` remains an ancestor frame and must not inherit authority to
    mint product DecisionLedger origin. Only the frame that directly invoked the
    installed execution wrapper may authorize the origin.
    """

    current = inspect.currentframe()
    wrapper = None
    caller = None
    try:
        if current is None:
            return None
        wrapper = current.f_back
        if (
            wrapper is None
            or wrapper.f_code is not _origin._execute_with_product_decision_origin.__code__
        ):
            return None
        caller = wrapper.f_back
        if caller is None:
            return None

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
    finally:
        # Frame references form cycles; drop them deterministically after every
        # resolution attempt instead of retaining execution state accidentally.
        del current
        del wrapper
        del caller


def _install() -> None:
    if _origin._resolve_product_origin_from_stack is _resolve_product_origin_from_direct_call:
        return
    _origin._resolve_product_origin_from_stack = _resolve_product_origin_from_direct_call


_install()


__all__ = []
