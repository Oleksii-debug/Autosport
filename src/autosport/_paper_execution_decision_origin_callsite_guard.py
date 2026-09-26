from __future__ import annotations

import inspect
from functools import wraps
from pathlib import Path
from types import FrameType

from . import _paper_execution_decision_origin as _origin
from . import _paper_execution_decision_origin_instance_guard as _instance_guard
from . import _paper_exposure_scope_provenance_guard as _exposure_scope_guard
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
_PAPER_VALUE_MATERIAL_ACTION_ID = _PAPER_VALUE_AGENT_CLASS._material_action_id

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
    _resolved_path_fn=_resolved_path,
) -> None:
    """Fail closed unless authority comes from this product workspace's ledger."""

    expected = _resolved_path_fn(Path(workspace) / "decisions.jsonl")  # type: ignore[arg-type]
    actual = _resolved_path_fn(getattr(ledger, "path", None))
    if actual != expected:
        raise _origin.PaperExecutionDecisionOriginError(
            f"{producer} decision origin requires canonical workspace decisions.jsonl"
        )


def _make_live_init_guard(stable_live_init, stable_signature, require_workspace_fn):
    @wraps(stable_live_init)
    def live_init_with_canonical_decision_ledger(self, *args, **kwargs) -> None:
        """Treat a caller-supplied live ledger as an assertion, never path authority."""

        bound = stable_signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        workspace = bound.arguments.get("workspace")
        supplied_ledger = bound.arguments.get("decision_ledger")
        supplied_runtime = bound.arguments.get("paper_execution")
        if supplied_runtime is not None and supplied_ledger is not None:
            if type(supplied_ledger) is not JsonlDecisionLedger:
                raise _origin.PaperExecutionDecisionOriginError(
                    "live decision origin requires exact JsonlDecisionLedger authority"
                )
            require_workspace_fn(
                supplied_ledger,
                workspace,
                producer="live",
            )

        stable_live_init(self, *args, **kwargs)

        runtime = getattr(self, "paper_execution", None)
        if runtime is not None:
            ledger = getattr(self, "decision_ledger", None)
            if type(ledger) is not JsonlDecisionLedger:
                raise _origin.PaperExecutionDecisionOriginError(
                    "live decision origin requires exact JsonlDecisionLedger authority"
                )
            require_workspace_fn(
                ledger,
                getattr(self, "workspace", workspace),
                producer="live",
            )

    return live_init_with_canonical_decision_ledger


_live_init_with_canonical_decision_ledger = _make_live_init_guard(
    _STABLE_LIVE_INIT,
    _STABLE_LIVE_INIT_SIGNATURE,
    _require_workspace_decision_ledger,
)


def _origin_from_direct_caller(
    runtime: PaperExecutionAdoptionRuntime,
    decision_id: str,
    caller: FrameType,
    *,
    _live_loop_class=_LIVE_LOOP_CLASS,
    _live_persist_plan_code=_LIVE_PERSIST_PLAN_CODE,
    _paper_value_agent_class=_PAPER_VALUE_AGENT_CLASS,
    _paper_value_material_action_id=_PAPER_VALUE_MATERIAL_ACTION_ID,
    _paper_value_fresh_code=_PAPER_VALUE_FRESH_CODE,
    _paper_value_recovery_code=_PAPER_VALUE_RECOVERY_CODE,
    _decision_record_class=_DECISION_RECORD_CLASS,
    _verified_origin=_instance_guard._verified_decision_origin_without_instance_dispatch,
    _require_workspace_fn=_require_workspace_decision_ledger,
    _resolved_path_fn=_resolved_path,
    _exact_context_ledger=_origin._exact_context_ledger,
):
    """Resolve origin only when the canonical producer directly calls execute."""

    local = caller.f_locals
    if caller.f_code is _live_persist_plan_code:
        owner = local.get("self")
        if type(owner) is not _live_loop_class:
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
        _require_workspace_fn(
            ledger,
            getattr(owner, "workspace", None),
            producer="live",
        )
        if local.get("decision_id") != decision_id:
            raise _origin.PaperExecutionDecisionOriginError(
                "live decision call-site identity does not match execution plan"
            )
        return _verified_origin(
            ledger,
            decision_id,
        )

    if caller.f_code is _paper_value_fresh_code:
        context = local.get("context")
        agent = local.get("self")
        event = local.get("event")
        if type(agent) is not _paper_value_agent_class:
            raise _origin.PaperExecutionDecisionOriginError(
                "paper-value origin requires exact PaperValueAgent authority"
            )
        if event is None or _paper_value_material_action_id(context, event) != decision_id:
            raise _origin.PaperExecutionDecisionOriginError(
                "paper-value decision call-site identity does not match execution plan"
            )
        ledger = _exact_context_ledger(runtime, context)
        _require_workspace_fn(
            ledger,
            _resolved_path_fn(getattr(runtime, "paper_book_path", None)).parent,
            producer="paper-value",
        )
        return _verified_origin(
            ledger,
            decision_id,
        )

    if caller.f_code is _paper_value_recovery_code:
        context = local.get("context")
        record = local.get("record")
        if type(record) is not _decision_record_class or record.decision_id != decision_id:
            raise _origin.PaperExecutionDecisionOriginError(
                "paper-value recovery call-site identity does not match execution plan"
            )
        ledger = _exact_context_ledger(runtime, context)
        _require_workspace_fn(
            ledger,
            _resolved_path_fn(getattr(runtime, "paper_book_path", None)).parent,
            producer="paper-value",
        )
        return _verified_origin(
            ledger,
            decision_id,
        )

    return None


def _matching_product_ancestor(
    runtime: PaperExecutionAdoptionRuntime,
    decision_id: str,
    frame: FrameType | None,
    *,
    _live_loop_class=_LIVE_LOOP_CLASS,
    _live_persist_plan_code=_LIVE_PERSIST_PLAN_CODE,
    _paper_value_agent_class=_PAPER_VALUE_AGENT_CLASS,
    _paper_value_material_action_id=_PAPER_VALUE_MATERIAL_ACTION_ID,
    _paper_value_fresh_code=_PAPER_VALUE_FRESH_CODE,
    _paper_value_recovery_code=_PAPER_VALUE_RECOVERY_CODE,
    _decision_record_class=_DECISION_RECORD_CLASS,
) -> bool:
    """Detect canonical ancestors only to reject nested execution, never authorize."""

    current = frame
    while current is not None:
        local = current.f_locals
        if current.f_code is _live_persist_plan_code:
            owner = local.get("self")
            if (
                type(owner) is _live_loop_class
                and getattr(owner, "paper_execution", None) is runtime
                and local.get("decision_id") == decision_id
            ):
                return True
        elif current.f_code is _paper_value_fresh_code:
            context = local.get("context")
            agent = local.get("self")
            event = local.get("event")
            if (
                type(agent) is _paper_value_agent_class
                and getattr(context, "paper_execution", None) is runtime
                and event is not None
                and _paper_value_material_action_id(context, event) == decision_id
            ):
                return True
        elif current.f_code is _paper_value_recovery_code:
            context = local.get("context")
            record = local.get("record")
            if (
                getattr(context, "paper_execution", None) is runtime
                and type(record) is _decision_record_class
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
    _stable_runtime_execute=_STABLE_RUNTIME_EXECUTE,
):
    return _stable_runtime_execute(
        self,
        prepared=prepared,
        trigger_id=trigger_id,
        started_at=started_at,
        materialize_exposure=materialize_exposure,
        observations=observations,
        evidence_registry=evidence_registry,
        suspended_action_ids=suspended_action_ids,
    )


def _make_execute_guard(
    origin_from_direct_caller_fn,
    matching_product_ancestor_fn,
    execute_stable_fn,
    product_origin_runtime,
    product_origin_callsite_code,
    paper_value_fresh_code,
    paper_value_recovery_code,
):
    def execute_with_exact_product_callsite(
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
        # Capability objects and trusted helpers are closure-held and cannot be
        # replaced through caller kwargs or module-global alias substitution.
        if (
            product_origin_runtime.get() is not None
            or product_origin_callsite_code.get() is not None
        ):
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
            return execute_stable_fn(
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
                else origin_from_direct_caller_fn(self, decision_id, caller)
            )

            if ambient_origin is not None:
                if (
                    caller is None
                    or caller.f_code
                    not in {paper_value_fresh_code, paper_value_recovery_code}
                    or origin is None
                    or origin != ambient_origin
                ):
                    raise _origin.PaperExecutionDecisionOriginError(
                        "ambient decision origin is not the exact verified paper-value resume origin"
                    )

            if origin is None:
                ancestor = None if caller is None else caller.f_back
                if matching_product_ancestor_fn(self, decision_id, ancestor):
                    raise PaperExecutionAdoptionError(
                        "nested PAPER execution cannot inherit product decision-origin authority"
                    )
                if ambient_origin is not None:
                    raise _origin.PaperExecutionDecisionOriginError(
                        "ambient decision origin cannot authorize non-product execution"
                    )
                return execute_stable_fn(
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
            if current is None:
                raise _origin.PaperExecutionDecisionOriginError(
                    "canonical product callsite frame is unavailable"
                )

            origin_token = None
            if ambient_origin is None:
                origin_token = _origin._DECISION_ORIGIN.set(origin)
            runtime_token = product_origin_runtime.set(self)
            callsite_token = product_origin_callsite_code.set(current.f_code)
            try:
                return execute_stable_fn(
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
                product_origin_callsite_code.reset(callsite_token)
                product_origin_runtime.reset(runtime_token)
                if origin_token is not None:
                    _origin._DECISION_ORIGIN.reset(origin_token)
        finally:
            del current
            del caller

    return execute_with_exact_product_callsite


_execute_with_exact_product_callsite = _make_execute_guard(
    _origin_from_direct_caller,
    _matching_product_ancestor,
    _execute_stable,
    _instance_guard._PRODUCT_ORIGIN_RUNTIME,
    _instance_guard._PRODUCT_ORIGIN_CALLSITE_CODE,
    _PAPER_VALUE_FRESH_CODE,
    _PAPER_VALUE_RECOVERY_CODE,
)
_execute_with_exact_product_callsite = _exposure_scope_guard.bind_canonical_execute(
    _execute_with_exact_product_callsite
)


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
