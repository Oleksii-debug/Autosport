"""Fail closed on exact-instance authority method shadowing during PAPER admission.

Exact concrete ledger types prevent subclass-based authority forgery, but their
non-data-descriptor methods can still be replaced per instance through ``__dict__``.
Campaign admission must reject those objects before any authority-bearing read so
attacker callables cannot virtualize durable execution or decision history.
"""

from __future__ import annotations

from .decision_ledger import JsonlDecisionLedger
from .paper_campaign_admission import (
    PaperCampaignAdmissionCoordinator,
    PaperCampaignAdmissionError,
)
from .paper_execution_reality import PaperExecutionLedger

_GUARD_MARKER = "__autosport_exact_admission_authority_guard_v1__"
_ORIGINAL_INIT = PaperCampaignAdmissionCoordinator.__init__


def _has_instance_shadow(value: object, method_name: str) -> bool:
    namespace = getattr(value, "__dict__", None)
    return isinstance(namespace, dict) and method_name in namespace


def _guarded_init(
    self: PaperCampaignAdmissionCoordinator,
    state_path,
    *,
    paper_book_path,
    decision_ledger: JsonlDecisionLedger,
    runtime,
    execution_ledger: PaperExecutionLedger,
) -> None:
    if type(decision_ledger) is JsonlDecisionLedger and _has_instance_shadow(
        decision_ledger, "verified_records"
    ):
        raise PaperCampaignAdmissionError(
            "Decision Ledger authority method is shadowed on the exact instance"
        )
    if type(execution_ledger) is PaperExecutionLedger and _has_instance_shadow(
        execution_ledger, "events"
    ):
        raise PaperCampaignAdmissionError(
            "PAPER execution authority method is shadowed on the exact instance"
        )
    _ORIGINAL_INIT(
        self,
        state_path,
        paper_book_path=paper_book_path,
        decision_ledger=decision_ledger,
        runtime=runtime,
        execution_ledger=execution_ledger,
    )


if not getattr(PaperCampaignAdmissionCoordinator, _GUARD_MARKER, False):
    PaperCampaignAdmissionCoordinator.__init__ = _guarded_init
    setattr(PaperCampaignAdmissionCoordinator, _GUARD_MARKER, True)
