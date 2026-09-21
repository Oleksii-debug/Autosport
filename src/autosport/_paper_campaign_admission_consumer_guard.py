"""Fail closed on mutable PAPER-admission authority objects.

Exact concrete ledger/runtime types prevent subclass-based authority forgery, but
non-data-descriptor methods can still be replaced per instance through ``__dict__``.
The coordinator itself can also be pointed at different exact authorities after
construction. Admission therefore pins the original authority objects outside the
coordinator instance, rejects replacement/shadowing immediately before every read,
and performs ledger reads through exact-class views so a racing mutation cannot
redirect durable execution or decision history.
"""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from weakref import WeakKeyDictionary

from .decision_ledger import JsonlDecisionLedger
from .paper_campaign_admission import (
    PaperCampaignAdmissionCoordinator,
    PaperCampaignAdmissionError,
)
from .paper_campaign_runtime import PaperCampaignRuntime
from .paper_execution_reality import PaperExecutionLedger
from .paper_settlement_learning import PaperSettlementLearningBridge

_GUARD_MARKER = "__autosport_exact_admission_authority_guard_v5__"
_ORIGINAL_INIT = PaperCampaignAdmissionCoordinator.__init__
_ORIGINAL_GETATTRIBUTE = PaperCampaignAdmissionCoordinator.__getattribute__
_ORIGINAL_RESOLVED_EXECUTION_DECISION_ID = (
    PaperCampaignAdmissionCoordinator._resolved_execution_decision_id
)
_ORIGINAL_EXECUTION_ATTEMPT = PaperCampaignAdmissionCoordinator._execution_attempt
_AUTHORITY_BINDINGS = WeakKeyDictionary()
_AUTHORITY_BINDINGS_LOCK = RLock()
_RUNTIME_METHODS = (
    "_parameters_with_reflection_commitment",
    "begin_and_bind_paper_ticket",
)
_BRIDGE_METHODS = ("_read",)


def _has_instance_shadow(value: object, method_name: str) -> bool:
    namespace = getattr(value, "__dict__", None)
    return isinstance(namespace, dict) and method_name in namespace


def _reject_instance_shadow(value: object, method_name: str, label: str) -> None:
    if _has_instance_shadow(value, method_name):
        raise PaperCampaignAdmissionError(
            f"{label} authority method is shadowed on the exact instance"
        )


def _reject_runtime_shadows(runtime: PaperCampaignRuntime) -> None:
    for method_name in _RUNTIME_METHODS:
        _reject_instance_shadow(runtime, method_name, "PAPER campaign runtime")


def _reject_bridge_shadows(bridge: PaperSettlementLearningBridge) -> None:
    for method_name in _BRIDGE_METHODS:
        _reject_instance_shadow(bridge, method_name, "PAPER settlement-learning bridge")


def _canonical_decision_path(state_path: object) -> Path:
    return (Path(state_path).parent / "decisions.jsonl").resolve(strict=False)


def _assert_decision_ledger_path(
    decision_ledger: JsonlDecisionLedger,
    expected_path: Path,
) -> None:
    if decision_ledger.path.resolve(strict=False) != expected_path:
        raise PaperCampaignAdmissionError(
            "Decision Ledger must be the canonical workspace decisions.jsonl"
        )


class _ExactDecisionLedgerReadView:
    __slots__ = ("_ledger",)

    def __init__(self, ledger: JsonlDecisionLedger) -> None:
        self._ledger = ledger

    def verified_records(self):
        return JsonlDecisionLedger.verified_records(self._ledger)


class _ExactExecutionLedgerReadView:
    __slots__ = ("_ledger",)

    def __init__(self, ledger: PaperExecutionLedger) -> None:
        self._ledger = ledger

    def events(self, run_id: str):
        return PaperExecutionLedger.events(self._ledger, run_id)


class _PinnedCoordinatorReadView:
    """Run legacy resolver logic without routing reads through mutable public fields."""

    __slots__ = ("_coordinator", "decision_ledger", "execution_ledger")

    def __init__(
        self,
        coordinator: PaperCampaignAdmissionCoordinator,
        *,
        decision_ledger: JsonlDecisionLedger,
        execution_ledger: PaperExecutionLedger,
    ) -> None:
        self._coordinator = coordinator
        self.decision_ledger = _ExactDecisionLedgerReadView(decision_ledger)
        self.execution_ledger = _ExactExecutionLedgerReadView(execution_ledger)

    def __getattr__(self, name: str):
        return getattr(self._coordinator, name)


def _binding_for_optional(self: PaperCampaignAdmissionCoordinator):
    with _AUTHORITY_BINDINGS_LOCK:
        return _AUTHORITY_BINDINGS.get(self)


def _binding_for(self: PaperCampaignAdmissionCoordinator):
    binding = _binding_for_optional(self)
    if binding is None:
        raise PaperCampaignAdmissionError(
            "PAPER admission authority binding is unavailable"
        )
    return binding


def _guarded_getattribute(self: PaperCampaignAdmissionCoordinator, name: str):
    value = _ORIGINAL_GETATTRIBUTE(self, name)
    if name not in {"decision_ledger", "execution_ledger", "runtime"}:
        return value
    binding = _binding_for_optional(self)
    if binding is None:
        return value
    expected = {
        "decision_ledger": binding[0],
        "execution_ledger": binding[1],
        "runtime": binding[3],
    }[name]
    if value is not expected:
        labels = {
            "decision_ledger": "Decision Ledger",
            "execution_ledger": "PAPER execution",
            "runtime": "PAPER campaign runtime",
        }
        raise PaperCampaignAdmissionError(
            f"{labels[name]} authority changed after admission construction"
        )
    return value


def _assert_raw_authority_fields(
    self: PaperCampaignAdmissionCoordinator,
    *,
    decision_ledger: JsonlDecisionLedger,
    execution_ledger: PaperExecutionLedger,
    runtime: PaperCampaignRuntime,
) -> None:
    if _ORIGINAL_GETATTRIBUTE(self, "decision_ledger") is not decision_ledger:
        raise PaperCampaignAdmissionError(
            "Decision Ledger authority changed after admission construction"
        )
    if _ORIGINAL_GETATTRIBUTE(self, "execution_ledger") is not execution_ledger:
        raise PaperCampaignAdmissionError(
            "PAPER execution authority changed after admission construction"
        )
    if _ORIGINAL_GETATTRIBUTE(self, "runtime") is not runtime:
        raise PaperCampaignAdmissionError(
            "PAPER campaign runtime authority changed after admission construction"
        )


def _assert_runtime_binding(
    self: PaperCampaignAdmissionCoordinator,
    runtime: PaperCampaignRuntime,
    settlement_bridge: PaperSettlementLearningBridge,
    environment: object,
    agent_loop: object,
) -> None:
    if _ORIGINAL_GETATTRIBUTE(self, "runtime") is not runtime:
        raise PaperCampaignAdmissionError(
            "PAPER campaign runtime authority changed after admission construction"
        )
    if type(runtime) is not PaperCampaignRuntime:
        raise PaperCampaignAdmissionError(
            "PAPER campaign runtime must remain exact PaperCampaignRuntime"
        )
    if runtime.settlement_bridge is not settlement_bridge:
        raise PaperCampaignAdmissionError(
            "PAPER campaign settlement authority changed after admission construction"
        )
    if type(settlement_bridge) is not PaperSettlementLearningBridge:
        raise PaperCampaignAdmissionError(
            "PAPER campaign settlement authority must remain exact "
            "PaperSettlementLearningBridge"
        )
    if runtime.environment is not environment:
        raise PaperCampaignAdmissionError(
            "PAPER campaign environment authority changed after admission construction"
        )
    if settlement_bridge.agent_loop is not agent_loop:
        raise PaperCampaignAdmissionError(
            "PAPER campaign AgentLoop authority changed after admission construction"
        )
    _reject_runtime_shadows(runtime)
    _reject_bridge_shadows(settlement_bridge)


def _guarded_init(
    self: PaperCampaignAdmissionCoordinator,
    state_path,
    *,
    paper_book_path,
    decision_ledger: JsonlDecisionLedger,
    runtime,
    execution_ledger: PaperExecutionLedger,
) -> None:
    if type(runtime) is not PaperCampaignRuntime:
        raise TypeError("runtime must be exact PaperCampaignRuntime")
    if type(runtime.settlement_bridge) is not PaperSettlementLearningBridge:
        raise TypeError(
            "runtime settlement_bridge must be exact PaperSettlementLearningBridge"
        )
    _reject_runtime_shadows(runtime)
    _reject_bridge_shadows(runtime.settlement_bridge)
    expected_decision_path = _canonical_decision_path(state_path)
    if type(decision_ledger) is JsonlDecisionLedger:
        _assert_decision_ledger_path(decision_ledger, expected_decision_path)
        _reject_instance_shadow(
            decision_ledger,
            "verified_records",
            "Decision Ledger",
        )
    if type(execution_ledger) is PaperExecutionLedger:
        _reject_instance_shadow(
            execution_ledger,
            "events",
            "PAPER execution",
        )
    _ORIGINAL_INIT(
        self,
        state_path,
        paper_book_path=paper_book_path,
        decision_ledger=decision_ledger,
        runtime=runtime,
        execution_ledger=execution_ledger,
    )
    with _AUTHORITY_BINDINGS_LOCK:
        _AUTHORITY_BINDINGS[self] = (
            decision_ledger,
            execution_ledger,
            expected_decision_path,
            runtime,
            runtime.settlement_bridge,
            runtime.environment,
            runtime.settlement_bridge.agent_loop,
            RLock(),
        )


def _guarded_resolved_execution_decision_id(self, *args, **kwargs):
    (
        decision_ledger,
        execution_ledger,
        expected_decision_path,
        runtime,
        settlement_bridge,
        environment,
        agent_loop,
        lock,
    ) = _binding_for(self)
    with lock:
        _assert_raw_authority_fields(
            self,
            decision_ledger=decision_ledger,
            execution_ledger=execution_ledger,
            runtime=runtime,
        )
        _assert_runtime_binding(
            self,
            runtime,
            settlement_bridge,
            environment,
            agent_loop,
        )
        _assert_decision_ledger_path(decision_ledger, expected_decision_path)
        _reject_instance_shadow(
            decision_ledger,
            "verified_records",
            "Decision Ledger",
        )
        reader = _PinnedCoordinatorReadView(
            self,
            decision_ledger=decision_ledger,
            execution_ledger=execution_ledger,
        )
        result = _ORIGINAL_RESOLVED_EXECUTION_DECISION_ID(reader, *args, **kwargs)
        _assert_raw_authority_fields(
            self,
            decision_ledger=decision_ledger,
            execution_ledger=execution_ledger,
            runtime=runtime,
        )
        _assert_decision_ledger_path(decision_ledger, expected_decision_path)
        _reject_instance_shadow(
            decision_ledger,
            "verified_records",
            "Decision Ledger",
        )
        return result


def _guarded_execution_attempt(self, *args, **kwargs):
    (
        decision_ledger,
        execution_ledger,
        expected_decision_path,
        runtime,
        settlement_bridge,
        environment,
        agent_loop,
        lock,
    ) = _binding_for(self)
    with lock:
        _assert_raw_authority_fields(
            self,
            decision_ledger=decision_ledger,
            execution_ledger=execution_ledger,
            runtime=runtime,
        )
        _assert_runtime_binding(
            self,
            runtime,
            settlement_bridge,
            environment,
            agent_loop,
        )
        _assert_decision_ledger_path(decision_ledger, expected_decision_path)
        _reject_instance_shadow(
            execution_ledger,
            "events",
            "PAPER execution",
        )
        reader = _PinnedCoordinatorReadView(
            self,
            decision_ledger=decision_ledger,
            execution_ledger=execution_ledger,
        )
        result = _ORIGINAL_EXECUTION_ATTEMPT(reader, *args, **kwargs)
        _assert_raw_authority_fields(
            self,
            decision_ledger=decision_ledger,
            execution_ledger=execution_ledger,
            runtime=runtime,
        )
        _assert_decision_ledger_path(decision_ledger, expected_decision_path)
        _reject_instance_shadow(
            execution_ledger,
            "events",
            "PAPER execution",
        )
        return result


if not getattr(PaperCampaignAdmissionCoordinator, _GUARD_MARKER, False):
    PaperCampaignAdmissionCoordinator.__init__ = _guarded_init
    PaperCampaignAdmissionCoordinator.__getattribute__ = _guarded_getattribute
    PaperCampaignAdmissionCoordinator._resolved_execution_decision_id = (
        _guarded_resolved_execution_decision_id
    )
    PaperCampaignAdmissionCoordinator._execution_attempt = _guarded_execution_attempt
    setattr(PaperCampaignAdmissionCoordinator, _GUARD_MARKER, True)
