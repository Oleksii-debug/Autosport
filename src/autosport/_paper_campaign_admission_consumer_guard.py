"""Fail closed on exact-instance authority method shadowing during PAPER admission.

Exact concrete ledger types prevent subclass-based authority forgery, but their
non-data-descriptor methods can still be replaced per instance through ``__dict__``.
Campaign admission must reject those objects immediately before every authority-
bearing read.  The guarded read then uses a tiny exact-class view so a mutation
racing after the check still cannot redirect durable execution or decision history.
"""

from __future__ import annotations

from threading import RLock

from .decision_ledger import JsonlDecisionLedger
from .paper_campaign_admission import (
    PaperCampaignAdmissionCoordinator,
    PaperCampaignAdmissionError,
)
from .paper_execution_reality import PaperExecutionLedger

_GUARD_MARKER = "__autosport_exact_admission_authority_guard_v2__"
_GUARD_LOCK_ATTR = "_exact_admission_authority_guard_lock"
_ORIGINAL_INIT = PaperCampaignAdmissionCoordinator.__init__
_ORIGINAL_RESOLVED_EXECUTION_DECISION_ID = (
    PaperCampaignAdmissionCoordinator._resolved_execution_decision_id
)
_ORIGINAL_EXECUTION_ATTEMPT = PaperCampaignAdmissionCoordinator._execution_attempt


def _has_instance_shadow(value: object, method_name: str) -> bool:
    namespace = getattr(value, "__dict__", None)
    return isinstance(namespace, dict) and method_name in namespace


def _reject_instance_shadow(value: object, method_name: str, label: str) -> None:
    if _has_instance_shadow(value, method_name):
        raise PaperCampaignAdmissionError(
            f"{label} authority method is shadowed on the exact instance"
        )


class _ExactDecisionLedgerReadView:
    __slots__ = ("_ledger",)

    def __init__(self, ledger: JsonlDecisionLedger) -> None:
        self._ledger = ledger

    def verified_records(self):
        # Bind the authority read to the exact class implementation instead of
        # normal instance dispatch, which can be redirected through __dict__.
        return JsonlDecisionLedger.verified_records(self._ledger)


class _ExactExecutionLedgerReadView:
    __slots__ = ("_ledger",)

    def __init__(self, ledger: PaperExecutionLedger) -> None:
        self._ledger = ledger

    def events(self, run_id: str):
        return PaperExecutionLedger.events(self._ledger, run_id)


def _guarded_init(
    self: PaperCampaignAdmissionCoordinator,
    state_path,
    *,
    paper_book_path,
    decision_ledger: JsonlDecisionLedger,
    runtime,
    execution_ledger: PaperExecutionLedger,
) -> None:
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
    setattr(self, _GUARD_LOCK_ATTR, RLock())


def _guarded_resolved_execution_decision_id(self, *args, **kwargs):
    lock = getattr(self, _GUARD_LOCK_ATTR)
    with lock:
        ledger = self.decision_ledger
        if type(ledger) is not JsonlDecisionLedger:
            raise PaperCampaignAdmissionError(
                "Decision Ledger authority changed after admission construction"
            )
        _reject_instance_shadow(ledger, "verified_records", "Decision Ledger")
        self.decision_ledger = _ExactDecisionLedgerReadView(ledger)
        try:
            return _ORIGINAL_RESOLVED_EXECUTION_DECISION_ID(self, *args, **kwargs)
        finally:
            self.decision_ledger = ledger


def _guarded_execution_attempt(self, *args, **kwargs):
    lock = getattr(self, _GUARD_LOCK_ATTR)
    with lock:
        ledger = self.execution_ledger
        if type(ledger) is not PaperExecutionLedger:
            raise PaperCampaignAdmissionError(
                "PAPER execution authority changed after admission construction"
            )
        _reject_instance_shadow(ledger, "events", "PAPER execution")
        self.execution_ledger = _ExactExecutionLedgerReadView(ledger)
        try:
            return _ORIGINAL_EXECUTION_ATTEMPT(self, *args, **kwargs)
        finally:
            self.execution_ledger = ledger


if not getattr(PaperCampaignAdmissionCoordinator, _GUARD_MARKER, False):
    PaperCampaignAdmissionCoordinator.__init__ = _guarded_init
    PaperCampaignAdmissionCoordinator._resolved_execution_decision_id = (
        _guarded_resolved_execution_decision_id
    )
    PaperCampaignAdmissionCoordinator._execution_attempt = _guarded_execution_attempt
    setattr(PaperCampaignAdmissionCoordinator, _GUARD_MARKER, True)
