from __future__ import annotations

from . import _paper_execution_decision_origin as _origin
from .paper_execution_reality import PaperExecutionLedger, PaperExecutionStateError


_ORIGIN_LOAD_RUN = PaperExecutionLedger.load_run


def _load_run_requiring_bound_origin(
    self: PaperExecutionLedger,
    *,
    run_id: str,
    trigger_id: str,
    plan,
    config,
    started_at: str,
    observation_evidence_ids,
):
    """Reject retry of an origin-bound run without the exact verified origin."""

    stored = _origin._reservation_origin_from_events(_origin._raw_events(self, run_id))
    if stored is not None and _origin._DECISION_ORIGIN.get() is None:
        raise PaperExecutionStateError(
            "origin-bound execution reservation requires verified decision origin on resume"
        )
    return _ORIGIN_LOAD_RUN(
        self,
        run_id=run_id,
        trigger_id=trigger_id,
        plan=plan,
        config=config,
        started_at=started_at,
        observation_evidence_ids=observation_evidence_ids,
    )


def _install() -> None:
    if getattr(PaperExecutionLedger, "_autosport_decision_origin_resume_guard", False):
        return
    PaperExecutionLedger.load_run = _load_run_requiring_bound_origin
    PaperExecutionLedger._autosport_decision_origin_resume_guard = True


_install()


__all__ = []
