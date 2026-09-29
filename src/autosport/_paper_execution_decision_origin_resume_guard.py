from __future__ import annotations

from typing import Any

from . import _paper_execution_decision_origin as _origin
from . import _paper_execution_decision_origin_callsite_guard as _callsite_guard
from . import _paper_execution_decision_origin_instance_guard as _instance_guard
from . import _paper_value_execution_authority as _paper_value_authority
from .decision_ledger import JsonlDecisionLedger
from .paper_execution_adoption import PaperExecutionAdoptionRuntime
from .paper_execution_reality import PaperExecutionLedger, PaperExecutionStateError
from .paper_strategy import PaperValueAgent


_LOAD_SENTINEL = "_autosport_decision_origin_pristine_load_run"
_EVENTS_SENTINEL = "_autosport_decision_origin_pristine_events"
_AGENT_EVENT_SENTINEL = (
    "_autosport_decision_origin_pristine_paper_value_on_market_event"
)
_CALLSITE_EXECUTE_CODE_SENTINEL = (
    "_autosport_decision_origin_pristine_product_callsite_code"
)
_SEAL_MARKER = "autosport.paper_execution_decision_origin.resume_guard.seal.v1"


def _sealed_tuple_from_callable(candidate: object):
    closure = getattr(candidate, "__closure__", None)
    if not closure:
        return None
    for cell in closure:
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if type(value) is tuple and len(value) == 4 and value[0] == _SEAL_MARKER:
            return value
    return None


def _initial_seal():
    return (
        _SEAL_MARKER,
        _origin._ORIGINAL_LEDGER_LOAD,
        _origin._ORIGINAL_LEDGER_EVENTS,
        PaperValueAgent.on_market_event,
    )


def _build_guard(seal):
    # Capture the already-sealed origin resolver, canonical material-action
    # derivation and product execution context in this installed wrapper closure.
    # Later module/instance substitution is not an authority source for fresh or
    # durable PaperValue execution.
    verified_origin = _instance_guard._verified_decision_origin_without_instance_dispatch
    product_origin_runtime = _instance_guard._PRODUCT_ORIGIN_RUNTIME
    material_action_id = PaperValueAgent._material_action_id

    def stable_raw_events(
        self: PaperExecutionLedger,
        run_id: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        return seal[2](self, run_id)

    def events_with_stable_origin_mask(
        self: PaperExecutionLedger,
        run_id: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        events = seal[2](self, run_id)
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

    def load_run_requiring_bound_origin(
        self: PaperExecutionLedger,
        *,
        run_id: str,
        trigger_id: str,
        plan,
        config,
        started_at: str,
        observation_evidence_ids,
    ):
        stored = _origin._reservation_origin_from_events(seal[2](self, run_id))
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
            return seal[1](
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

    def reservation_decision_origin_stable(
        self: PaperExecutionLedger,
        run_id: str,
    ):
        if type(self) is not PaperExecutionLedger:
            raise _origin.PaperExecutionDecisionOriginError(
                "reservation origin requires exact PaperExecutionLedger authority"
            )
        return _origin._reservation_origin_from_events(seal[2](self, run_id))

    def paper_value_on_market_event_with_durable_origin(
        self: PaperValueAgent,
        event,
        context,
    ) -> None:
        if (
            _origin._DECISION_ORIGIN.get() is not None
            or product_origin_runtime.get() is not None
        ):
            raise _origin.PaperExecutionDecisionOriginError(
                "caller-supplied decision-origin context cannot enter paper-value execution"
            )

        runtime = getattr(context, "paper_execution", None)
        if runtime is None or event.quote_key in self._acted:
            return seal[3](self, event, context)

        ledger = getattr(context, "decision_ledger", None)
        if type(ledger) is not JsonlDecisionLedger:
            return seal[3](self, event, context)

        if type(self) is not PaperValueAgent:
            raise _origin.PaperExecutionDecisionOriginError(
                "paper-value origin requires exact PaperValueAgent authority"
            )
        namespace = getattr(self, "__dict__", None)
        if isinstance(namespace, dict) and "_material_action_id" in namespace:
            raise _origin.PaperExecutionDecisionOriginError(
                "paper-value agent shadows authority method _material_action_id"
            )

        decision_id = material_action_id(context, event)
        record = _paper_value_authority._durable_record_for_call(
            self,
            event,
            context,
            decision_id,
        )
        if record is None:
            return seal[3](self, event, context)

        verified = verified_origin(
            ledger,
            decision_id,
        )
        token = _origin._DECISION_ORIGIN.set(verified)
        try:
            return seal[3](self, event, context)
        finally:
            _origin._DECISION_ORIGIN.reset(token)

    return (
        stable_raw_events,
        events_with_stable_origin_mask,
        load_run_requiring_bound_origin,
        reservation_decision_origin_stable,
        paper_value_on_market_event_with_durable_origin,
    )


def _install() -> None:
    already_installed = bool(
        getattr(PaperExecutionLedger, "_autosport_decision_origin_resume_guard", False)
    )
    seal = _sealed_tuple_from_callable(PaperExecutionLedger.events)
    if already_installed and seal is None:
        raise RuntimeError("decision-origin resume guard executable seal is unavailable")
    if seal is None:
        seal = _initial_seal()

    if not hasattr(PaperExecutionAdoptionRuntime, _CALLSITE_EXECUTE_CODE_SENTINEL):
        if PaperExecutionAdoptionRuntime.execute is not _callsite_guard._execute_with_exact_product_callsite:
            raise RuntimeError("decision-origin callsite guard was not installed canonically")
        setattr(
            PaperExecutionAdoptionRuntime,
            _CALLSITE_EXECUTE_CODE_SENTINEL,
            _callsite_guard._execute_with_exact_product_callsite.__code__,
        )

    # Compatibility/debug mirrors only. Authority wrappers use the closure-held
    # seal and reload restores these mirrors after deliberate tampering.
    setattr(PaperExecutionLedger, _LOAD_SENTINEL, seal[1])
    setattr(PaperExecutionLedger, _EVENTS_SENTINEL, seal[2])
    setattr(PaperValueAgent, _AGENT_EVENT_SENTINEL, seal[3])

    global _STABLE_LOAD_RUN
    global _STABLE_EVENTS
    global _STABLE_PAPER_VALUE_ON_MARKET_EVENT
    global _stable_raw_events
    global _events_with_stable_origin_mask
    global _load_run_requiring_bound_origin
    global _reservation_decision_origin_stable
    global _paper_value_on_market_event_with_durable_origin
    _STABLE_LOAD_RUN = seal[1]
    _STABLE_EVENTS = seal[2]
    _STABLE_PAPER_VALUE_ON_MARKET_EVENT = seal[3]
    (
        _stable_raw_events,
        _events_with_stable_origin_mask,
        _load_run_requiring_bound_origin,
        _reservation_decision_origin_stable,
        _paper_value_on_market_event_with_durable_origin,
    ) = _build_guard(seal)

    if already_installed:
        return
    PaperExecutionLedger.events = _events_with_stable_origin_mask
    PaperExecutionLedger.load_run = _load_run_requiring_bound_origin
    PaperExecutionLedger.reservation_decision_origin = _reservation_decision_origin_stable
    PaperExecutionLedger._autosport_decision_origin_resume_guard = True
    PaperValueAgent.on_market_event = _paper_value_on_market_event_with_durable_origin
    PaperValueAgent._autosport_decision_origin_resume_guard = True


_install()


__all__ = []
