from __future__ import annotations

import inspect
from functools import wraps
from pathlib import Path
from types import FrameType

from . import _paper_execution_decision_origin as _origin
from . import _paper_execution_decision_origin_instance_guard as _instance_guard
from . import _paper_value_execution_authority as _paper_value_authority
from . import live_decision_loop as _live_decision_loop
from .decision_ledger import JsonlDecisionLedger
from .paper_execution_adoption import PaperExecutionAdoptionError, PaperExecutionAdoptionRuntime


_RUNTIME_EXECUTE_SENTINEL = "_autosport_decision_origin_pristine_runtime_execute"
_CALLSITE_IDENTITY_SENTINEL = "_autosport_decision_origin_frozen_callsites"
_LIVE_INIT_SENTINEL = "_autosport_decision_origin_pristine_live_init"
_LIVE_INIT_GUARD_SENTINEL = "_autosport_decision_origin_live_init_guard"

# The true execution implementation must be preserved once, before any origin
# wrapper can be recaptured by importlib.reload(_paper_execution_decision_origin).
if not hasattr(PaperExecutionAdoptionRuntime, _RUNTIME_EXECUTE_SENTINEL):
    setattr(
        PaperExecutionAdoptionRuntime,
        _RUNTIME_EXECUTE_SENTINEL,
        _origin._ORIGINAL_RUNTIME_EXECUTE,
    )
_STABLE_RUNTIME_EXECUTE = getattr(
    PaperExecutionAdoptionRuntime,
    _RUNTIME_EXECUTE_SENTINEL,
)

# Freeze producer ownership at installation time. Reading mutable class attributes
# on every call would let later monkey-patching redefine which code object is
# accepted as a product authority.
if not hasattr(PaperExecutionAdoptionRuntime, _CALLSITE_IDENTITY_SENTINEL):
    setattr(
        PaperExecutionAdoptionRuntime,
        _CALLSITE_IDENTITY_SENTINEL,
        (
            _live_decision_loop.PersistentLiveDecisionLoop,
            _live_decision_loop.PersistentLiveDecisionLoop._persist_plan.__code__,
            _paper_value_authority.PaperValueAgent,
            _paper_value_authority._ORIGINAL_ON_MARKET_EVENT.__code__,
            _paper_value_authority._resume_durable_paper_value.__code__,
            _paper_value_authority.DecisionRecord,
        ),
    )
(
    _LIVE_LOOP_CLASS,
    _LIVE_PERSIST_PLAN_CODE,
    _PAPER_VALUE_AGENT_CLASS,
    _PAPER_VALUE_FRESH_CODE,
    _PAPER_VALUE_RECOVERY_CODE,
    _DECISION_RECORD_CLASS,
) = getattr(PaperExecutionAdoptionRuntime, _CALLSITE_IDENTITY_SENTINEL)

if not hasattr(_LIVE_LOOP_CLASS, _LIVE_INIT_SENTINEL):
    setattr(_LIVE_LOOP_CLASS, _LIVE_INIT_SENTINEL, _LIVE_LOOP_CLASS.__init__)
_STABLE_LIVE_INIT = getattr(_LIVE_LOOP_CLASS, _LIVE_INIT_SENTINEL)
_STABLE_LIVE_INIT_SIGNATURE = inspect.signature(_STABLE_LIVE_INIT)


def _resolved_path(value: object) -> Path:
    try:
        return Path(value).resolve(strict=False)  # type: ignore[arg-type]
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _origin.PaperExecutionDecisionOriginError(
            "product DecisionLedger path is not a canonical filesystem identity"
        ) from exc


def _require_workspace_decision_ledger(
    ledger: JsonlDecisionLedger,
    workspace: object,
    *,
    producer: str,
) -> None:
    """Fail closed unless authority comes from this product workspace's ledger."""

    expected = _resolved_path(Path(workspace) / "decisions.jsonl")  # type: ignore[arg-type]
    actual = _resolved_path(getattr(ledger, "path", None))
    if actual != expected:
        raise _origin.PaperExecutionDecisionOriginError(
            f"{producer} decision origin requires canonical workspace decisions.jsonl"
        )


@wraps(_STABLE_LIVE_INIT)
def _live_init_with_canonical_decision_ledger(self, *args, **kwargs) -> None:
    """Treat a caller-supplied live ledger as an assertion, never path authority."""

    bound = _STABLE_LIVE_INIT_SIGNATURE.bind(self, *args, **kwargs)
    bound.apply_defaults()
    workspace = bound.arguments.get("workspace")
    supplied_ledger = bound.arguments.get("decision_ledger")
    supplied_runtime = bound.arguments.get("paper_execution")
    if supplied_runtime is not None and supplied_ledger is not None:
        if type(supplied_ledger) is not JsonlDecisionLedger:
            raise _origin.PaperExecutionDecisionOriginError(
                "live decision origin requires exact JsonlDecisionLedger authority"
            )
        _require_workspace_decision_ledger(
            supplied_ledger,
            workspace,
            producer="live",
        )

    _STABLE_LIVE_INIT(self, *args, **kwargs)

    runtime = getattr(self, "paper_execution", None)
    if runtime is not None:
        ledger = getattr(self, "decision_ledger", None)
        if type(ledger) is not JsonlDecisionLedger:
            raise _origin.PaperExecutionDecisionOriginError(
                "live decision origin requires exact JsonlDecisionLedger authority"
            )
        _require_workspace_decision_ledger(
            ledger,
            getattr(self, "workspace", workspace),
            producer="live",
        )


def _origin_from_direct_caller(
    runtime: PaperExecutionAdoptionRuntime,
    decision_id: str,
    caller: FrameType,
):
    """Resolve origin only when the canonical producer directly calls execute."""

    local = caller.f_locals
    if caller.f_code is _LIVE_PERSIST_PLAN_CODE:
        owner = local.get("self")
        if type(owner) is not _LIVE_LOOP_CLASS:
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
        _require_workspace_decision_ledger(
            ledger,
            getattr(owner, "workspace", None),
            producer="live",
        )
        if local.get("decision_id") != decision_id:
            raise _origin.PaperExecutionDecisionOriginError(
                "live decision call-site identity does not match execution plan"
            )
        return _instance_guard._verified_decision_origin_without_instance_dispatch(
            ledger,
            decision_id,
        )

    if caller.f_code is _PAPER_VALUE_FRESH_CODE:
        context = local.get("context")
        agent = local.get("self")
        event = local.get("event")
        if type(agent) is not _PAPER_VALUE_AGENT_CLASS:
            raise _origin.PaperExecutionDecisionOriginError(
                "paper-value origin requires exact PaperValueAgent authority"
            )
        if event is None or agent._material_action_id(context, event) != decision_id:
            raise _origin.PaperExecutionDecisionOriginError(
                "paper-value decision call-site identity does not match execution plan"
            )
        ledger = _origin._exact_context_ledger(runtime, context)
        _require_workspace_decision_ledger(
            ledger,
            _resolved_path(getattr(runtime, "paper_book_path", None)).parent,
            producer="paper-value",
        )
        return _instance_guard._verified_decision_origin_without_instance_dispatch(
            ledger,
            decision_id,
        )

    if caller.f_code is _PAPER_VALUE_RECOVERY_CODE:
        context = local.get("context")
        record = local.get("record")
        if type(record) is not _DECISION_RECORD_CLASS or record.decision_id != decision_id:
            raise _origin.PaperExecutionDecisionOriginError(
                "paper-value recovery call-site identity does not match execution plan"
            )
        ledger = _origin._exact_context_ledger(runtime, context)
        _require_workspace_decision_ledger(
            ledger,
            _resolved_path(getattr(runtime, "paper_book_path", None)).parent,
            producer="paper-value",
        )
        return _instance_guard._verified_decision_origin_without_instance_dispatch(
            ledger,
            decision_id,
        )

    return None


def _matching_product_ancestor(
    runtime: PaperExecutionAdoptionRuntime,
    decision_id: str,
    frame: FrameType | None,
) -> bool:
    """Detect canonical ancestors only to reject nested execution, never authorize."""

    current = frame
    while current is not None:
        local = current.f_locals
        if current.f_code is _LIVE_PERSIST_PLAN_CODE:
            owner = local.get("self")
            if (
                type(owner) is _LIVE_LOOP_CLASS
                and getattr(owner, "paper_execution", None) is runtime
                and local.get("decision_id") == decision_id
            ):
                return True
        elif current.f_code is _PAPER_VALUE_FRESH_CODE:
            context = local.get("context")
            agent = local.get("self")
            event = local.get("event")
            if (
                type(agent) is _PAPER_VALUE_AGENT_CLASS
                and getattr(context, "paper_execution", None) is runtime
                and event is not None
                and agent._material_action_id(context, event) == decision_id
            ):
                return True
        elif current.f_code is _PAPER_VALUE_RECOVERY_CODE:
            context = local.get("context")
            record = local.get("record")
            if (
                getattr(context, "paper_execution", None) is runtime
                and type(record) is _DECISION_RECORD_CLASS
                and record.decision_id == decision_id
            ):
                return True
        current = current.f_back
    return False


def _execute_stable(
    self: PaperExecutionAdoptionRuntime,
    *,
    prepared,
    trigger_id: str,
    started_at: str,
    materialize_exposure: bool,
    observations,
    evidence_registry,
    suspended_action_ids: frozenset[str],
):
    return _STABLE_RUNTIME_EXECUTE(
        self,
        prepared=prepared,
        trigger_id=trigger_id,
        started_at=started_at,
        materialize_exposure=materialize_exposure,
        observations=observations,
        evidence_registry=evidence_registry,
        suspended_action_ids=suspended_action_ids,
    )


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
    # A runtime token can only exist inside this wrapper's own stable execution.
    # Seeing one at entry means a caller tried to smuggle product authority across
    # an invocation boundary.
    if _instance_guard._PRODUCT_ORIGIN_RUNTIME.get() is not None:
        raise _origin.PaperExecutionDecisionOriginError(
            "caller-supplied product-origin runtime context is not authority"
        )
    ambient_origin = _origin._DECISION_ORIGIN.get()

    decision_id = getattr(getattr(prepared, "execution_plan", None), "decision_id", None)
    if type(decision_id) is not str or not decision_id:
        if ambient_origin is not None:
            raise _origin.PaperExecutionDecisionOriginError(
                "caller-supplied decision-origin context cannot authorize execution"
            )
        return _execute_stable(
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

        if ambient_origin is not None:
            if (
                caller is None
                or caller.f_code
                not in {_PAPER_VALUE_FRESH_CODE, _PAPER_VALUE_RECOVERY_CODE}
                or origin is None
                or origin != ambient_origin
            ):
                raise _origin.PaperExecutionDecisionOriginError(
                    "ambient decision origin is not the exact verified paper-value resume origin"
                )

        if origin is None:
            ancestor = None if caller is None else caller.f_back
            if _matching_product_ancestor(self, decision_id, ancestor):
                raise PaperExecutionAdoptionError(
                    "nested PAPER execution cannot inherit product decision-origin authority"
                )
            if ambient_origin is not None:
                raise _origin.PaperExecutionDecisionOriginError(
                    "ambient decision origin cannot authorize non-product execution"
                )
            return _execute_stable(
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

        origin_token = None
        if ambient_origin is None:
            origin_token = _origin._DECISION_ORIGIN.set(origin)
        runtime_token = _instance_guard._PRODUCT_ORIGIN_RUNTIME.set(self)
        try:
            return _execute_stable(
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
            _instance_guard._PRODUCT_ORIGIN_RUNTIME.reset(runtime_token)
            if origin_token is not None:
                _origin._DECISION_ORIGIN.reset(origin_token)
    finally:
        del current
        del caller


def _install() -> None:
    if not getattr(_LIVE_LOOP_CLASS, _LIVE_INIT_GUARD_SENTINEL, False):
        _LIVE_LOOP_CLASS.__init__ = _live_init_with_canonical_decision_ledger
        setattr(_LIVE_LOOP_CLASS, _LIVE_INIT_GUARD_SENTINEL, True)
    if getattr(PaperExecutionAdoptionRuntime, "_autosport_decision_origin_callsite_guard", False):
        return
    PaperExecutionAdoptionRuntime.execute = _execute_with_exact_product_callsite
    PaperExecutionAdoptionRuntime._autosport_decision_origin_callsite_guard = True


_install()


__all__ = []
