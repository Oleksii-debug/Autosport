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

from threading import RLock
from weakref import WeakKeyDictionary

from .decision_ledger import JsonlDecisionLedger
from .paper_campaign_admission import (
    PaperCampaignAdmissionCoordinator,
    PaperCampaignAdmissionError,
)
from .paper_campaign_runtime import PaperCampaignRuntime
from .paper_execution_reality import PaperExecutionLedger

_GUARD_MARKER = "__autosport_exact_admission_authority_guard_v4__"
_ORIGINAL_INIT = PaperCampaignAdmissionCoordinator.__init__
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


def _binding_for(self: PaperCampaignAdmissionCoordinator):
    with _AUTHORITY_BINDINGS_LOCK:
        binding = _AUTHORITY_BINDINGS.get(self)
    if binding is None:
        raise PaperCampaignAdmissionError(
            "PAPER admission authority binding is unavailable"
        )
    return binding


def _assert_runtime_binding(
    self: PaperCampaignAdmissionCoordinator,
    runtime: PaperCampaignRuntime,
    settlement_bridge: object,
) -> None:
    if self.runtime is not runtime:
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
    _reject_runtime_shadows(runtime)


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
    _reject_runtime_shadows(runtime)
    if type(decision_ledger) is JsonlDecisionLedger:
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
            runtime,
            runtime.settlement_bridge,
            RLock(),
        )


def _guarded_resolved_execution_decision_id(self, *args, **kwargs):
    decision_ledger, _execution_ledger, runtime, settlement_bridge, lock = _binding_for(self)
    with lock:
        _assert_runtime_binding(self, runtime, settlement_bridge)
        if self.decision_ledger is not decision_ledger:
            raise PaperCampaignAdmissionError(
                "Decision Ledger authority changed after admission construction"
            )
        _reject_instance_shadow(
            decision_ledger,
            "verified_records",
            "Decision Ledger",
        )
        self.decision_ledger = _ExactDecisionLedgerReadView(decision_ledger)
        try:
            return _ORIGINAL_RESOLVED_EXECUTION_DECISION_ID(self, *args, **kwargs)
        finally:
            self.decision_ledger = decision_ledger


def _guarded_execution_attempt(self, *args, **kwargs):
    _decision_ledger, execution_ledger, runtime, settlement_bridge, lock = _binding_for(self)
    with lock:
        _assert_runtime_binding(self, runtime, settlement_bridge)
        if self.execution_ledger is not execution_ledger:
            raise PaperCampaignAdmissionError(
                "PAPER execution authority changed after admission construction"
            )
        _reject_instance_shadow(
            execution_ledger,
            "events",
            "PAPER execution",
        )
        self.execution_ledger = _ExactExecutionLedgerReadView(execution_ledger)
        try:
            return _ORIGINAL_EXECUTION_ATTEMPT(self, *args, **kwargs)
        finally:
            self.execution_ledger = execution_ledger


if not getattr(PaperCampaignAdmissionCoordinator, _GUARD_MARKER, False):
    PaperCampaignAdmissionCoordinator.__init__ = _guarded_init
    PaperCampaignAdmissionCoordinator._resolved_execution_decision_id = (
        _guarded_resolved_execution_decision_id
    )
    PaperCampaignAdmissionCoordinator._execution_attempt = _guarded_execution_attempt
    setattr(PaperCampaignAdmissionCoordinator, _GUARD_MARKER, True)
