from __future__ import annotations

from typing import Any

from . import _paper_execution_decision_origin as _origin
from .paper_execution_reality import PaperExecutionLedger, PaperExecutionStateError


_LOAD_SENTINEL = "_autosport_decision_origin_pristine_load_run"
_EVENTS_SENTINEL = "_autosport_decision_origin_pristine_events"

# Persist the pre-origin composed implementations once. The origin module may be
# reloaded later; these references must never become already-installed wrappers.
if not hasattr(PaperExecutionLedger, _LOAD_SENTINEL):
    setattr(PaperExecutionLedger, _LOAD_SENTINEL, _origin._ORIGINAL_LEDGER_LOAD)
if not hasattr(PaperExecutionLedger, _EVENTS_SENTINEL):
    setattr(PaperExecutionLedger, _EVENTS_SENTINEL, _origin._ORIGINAL_LEDGER_EVENTS)

_STABLE_LOAD_RUN = getattr(PaperExecutionLedger, _LOAD_SENTINEL)
_STABLE_EVENTS = getattr(PaperExecutionLedger, _EVENTS_SENTINEL)


def _stable_raw_events(
    self: PaperExecutionLedger,
    run_id: str | None = None,
) -> tuple[dict[str, Any], ...]:
    return _STABLE_EVENTS(self, run_id)


def _events_with_stable_origin_mask(
    self: PaperExecutionLedger,
    run_id: str | None = None,
) -> tuple[dict[str, Any], ...]:
    events = _stable_raw_events(self, run_id)
    if not _origin._MASK_ORIGIN_FOR_LEGACY_LOAD.get():
        return events

    masked: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != "RUN_RESERVED":
            masked.append(event)
            continue
        payload = event.get("payload")
        if type(payload) is not dict or "decision_origin" not in payload:
            masked.append(event)
            continue
        copy = dict(event)
        copy_payload = dict(payload)
        copy_payload.pop("decision_origin", None)
        copy["payload"] = copy_payload
        masked.append(copy)
    return tuple(masked)


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
    """Reject retry unless the exact reservation origin is re-resolved now."""

    stored = _origin._reservation_origin_from_events(_stable_raw_events(self, run_id))
    expected = _origin._DECISION_ORIGIN.get()
    if stored is not None and (
        stored.decision_id != trigger_id or stored.decision_id != plan.decision_id
    ):
        raise _origin.PaperExecutionDecisionOriginError(
            "durable decision origin conflicts with execution reservation identity"
        )
    if stored is not None and expected is None:
        raise PaperExecutionStateError(
            "origin-bound execution reservation requires verified decision origin on resume"
        )
    if expected is not None:
        if stored is None:
            raise PaperExecutionStateError(
                "existing execution reservation lacks pre-execution decision origin"
            )
        if stored != expected:
            raise PaperExecutionStateError(
                "execution decision origin changed across retry/restart"
            )

    token = _origin._MASK_ORIGIN_FOR_LEGACY_LOAD.set(True)
    try:
        return _STABLE_LOAD_RUN(
            self,
            run_id=run_id,
            trigger_id=trigger_id,
            plan=plan,
            config=config,
            started_at=started_at,
            observation_evidence_ids=observation_evidence_ids,
        )
    finally:
        _origin._MASK_ORIGIN_FOR_LEGACY_LOAD.reset(token)


def _reservation_decision_origin_stable(
    self: PaperExecutionLedger,
    run_id: str,
):
    if type(self) is not PaperExecutionLedger:
        raise _origin.PaperExecutionDecisionOriginError(
            "reservation origin requires exact PaperExecutionLedger authority"
        )
    return _origin._reservation_origin_from_events(_stable_raw_events(self, run_id))


def _install() -> None:
    if getattr(PaperExecutionLedger, "_autosport_decision_origin_resume_guard", False):
        return
    PaperExecutionLedger.events = _events_with_stable_origin_mask
    PaperExecutionLedger.load_run = _load_run_requiring_bound_origin
    PaperExecutionLedger.reservation_decision_origin = _reservation_decision_origin_stable
    PaperExecutionLedger._autosport_decision_origin_resume_guard = True


_install()


__all__ = []
