"""Fail closed on exact-instance authority method shadowing during PAPER admission.

Exact concrete ledger types prevent subclass-based authority forgery, but their
non-data-descriptor methods can still be replaced per instance through ``__dict__``.
Campaign admission must reject those objects immediately before every authority-
bearing read so post-construction mutation cannot virtualize durable execution or
decision history.
"""

from __future__ import annotations

from .decision_ledger import JsonlDecisionLedger
from .paper_campaign_admission import (
    PaperCampaignAdmissionCoordinator,
    PaperCampaignAdmissionError,
)
from .paper_execution_reality import PaperExecutionLedger

_GUARD_MARKER = "__autosport_exact_admission_authority_guard_v2__"
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


def _guarded_resolved_execution_decision_id(self, *args, **kwargs):
    # __init__ is not a sufficient authority boundary: callers retain the exact
    # ledger object and can mutate its instance namespace after construction.
    # The public coordinator already enforces exact concrete ledger types, so an
    # unshadowed lookup here resolves the canonical class implementation.
    _reject_instance_shadow(
        self.decision_ledger,
        "verified_records",
        "Decision Ledger",
    )
    return _ORIGINAL_RESOLVED_EXECUTION_DECISION_ID(self, *args, **kwargs)


def _guarded_execution_attempt(self, *args, **kwargs):
    _reject_instance_shadow(
        self.execution_ledger,
        "events",
        "PAPER execution",
    )
    return _ORIGINAL_EXECUTION_ATTEMPT(self, *args, **kwargs)


if not getattr(PaperCampaignAdmissionCoordinator, _GUARD_MARKER, False):
    PaperCampaignAdmissionCoordinator.__init__ = _guarded_init
    PaperCampaignAdmissionCoordinator._resolved_execution_decision_id = (
        _guarded_resolved_execution_decision_id
    )
    PaperCampaignAdmissionCoordinator._execution_attempt = _guarded_execution_attempt
    setattr(PaperCampaignAdmissionCoordinator, _GUARD_MARKER, True)
