from __future__ import annotations

from typing import Any

from . import _paper_execution_decision_origin as _origin
from . import _paper_execution_decision_origin_instance_guard as _instance_guard
from . import _paper_value_execution_authority as _paper_value_authority
from .decision_ledger import JsonlDecisionLedger
from .paper_execution_reality import PaperExecutionLedger, PaperExecutionStateError
from .paper_strategy import PaperValueAgent


_LOAD_SENTINEL = "_autosport_decision_origin_pristine_load_run"
_EVENTS_SENTINEL = "_autosport_decision_origin_pristine_events"
_AGENT_EVENT_SENTINEL = (
    "_autosport_decision_origin_pristine_paper_value_on_market_event"
)

# Persist the pre-origin composed implementations once. The origin module may be
# reloaded later; these references must never become already-installed wrappers.
if not hasattr(PaperExecutionLedger, _LOAD_SENTINEL):
    setattr(PaperExecutionLedger, _LOAD_SENTINEL, _origin._ORIGINAL_LEDGER_LOAD)
if not hasattr(PaperExecutionLedger, _EVENTS_SENTINEL):
    setattr(PaperExecutionLedger, _EVENTS_SENTINEL, _origin._ORIGINAL_LEDGER_EVENTS)
if not hasattr(PaperValueAgent, _AGENT_EVENT_SENTINEL):
    # _paper_value_execution_authority is imported before this guard by the package
    # facade, so this is the fully-composed product producer rather than the legacy
    # strategy method. Keep that exact producer stable across guard reloads.
    setattr(PaperValueAgent, _AGENT_EVENT_SENTINEL, PaperValueAgent.on_market_event)

_STABLE_LOAD_RUN = getattr(PaperExecutionLedger, _LOAD_SENTINEL)
_STABLE_EVENTS = getattr(PaperExecutionLedger, _EVENTS_SENTINEL)
_STABLE_PAPER_VALUE_ON_MARKET_EVENT = getattr(PaperValueAgent, _AGENT_EVENT_SENTINEL)


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


def _paper_value_on_market_event_with_durable_origin(
    self: PaperValueAgent,
    event,
    context,
) -> None:
    """Bind an already-durable decision origin across the whole recovery read path."""

    runtime = getattr(context, "paper_execution", None)
    if runtime is None or event.quote_key in self._acted:
        return _STABLE_PAPER_VALUE_ON_MARKET_EVENT(self, event, context)

    ledger = getattr(context, "decision_ledger", None)
    if type(ledger) is not JsonlDecisionLedger:
        return _STABLE_PAPER_VALUE_ON_MARKET_EVENT(self, event, context)

    decision_id = self._material_action_id(context, event)
    record = _paper_value_authority._durable_record_for_call(
        self,
        event,
        context,
        decision_id,
    )
    if record is None:
        return _STABLE_PAPER_VALUE_ON_MARKET_EVENT(self, event, context)

    verified_origin = _instance_guard._verified_decision_origin_without_instance_dispatch(
        ledger,
        decision_id,
    )
    with _origin.bound_decision_origin(verified_origin):
        return _STABLE_PAPER_VALUE_ON_MARKET_EVENT(self, event, context)


def _install() -> None:
    # Re-assert the composed bindings on every reload. Other guard modules are
    # intentionally reload-tested and can reinstall an earlier layer; a persistent
    # boolean marker alone would otherwise leave stale product methods behind.
    PaperExecutionLedger.events = _events_with_stable_origin_mask
    PaperExecutionLedger.load_run = _load_run_requiring_bound_origin
    PaperExecutionLedger.reservation_decision_origin = _reservation_decision_origin_stable
    PaperExecutionLedger._autosport_decision_origin_resume_guard = True
    PaperValueAgent.on_market_event = _paper_value_on_market_event_with_durable_origin
    PaperValueAgent._autosport_decision_origin_resume_guard = True


_install()


__all__ = []
