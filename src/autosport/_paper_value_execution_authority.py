from __future__ import annotations

import inspect
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from . import _paper_execution_reality_legacy as _paper_impl
from .decision_ledger import DecisionRecord, JsonlDecisionLedger
from .paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
    PreparedPaperExecution,
)
from .paper_strategy import PaperValueAgent
from .real_execution_ledger import ExecutionPlan
from .risk import PaperRiskPolicy


@dataclass(frozen=True, slots=True)
class PaperValueExecutionDescriptor:
    """Non-authoritative description of a legacy paper-value execution plan.

    The descriptor deliberately carries the exact plan/evidence needed to make a
    durable DecisionRecord before any positive PAPER execution capability exists.
    It is audit data only: PaperExecutionAdoptionRuntime.execute() refuses it until
    the canonical PaperValueAgent call has re-resolved the exact durable decision.
    """

    execution_plan: ExecutionPlan
    exposure_bindings: tuple[PaperExposureBinding, ...]
    intent_evidence_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.execution_plan, ExecutionPlan):
            raise TypeError("execution_plan must be ExecutionPlan")
        if (
            type(self.exposure_bindings) is not tuple
            or any(
                not isinstance(item, PaperExposureBinding)
                for item in self.exposure_bindings
            )
        ):
            raise TypeError(
                "exposure_bindings must be a tuple of PaperExposureBinding values"
            )
        action_ids = tuple(action.action_id for action in self.execution_plan.actions)
        binding_ids = tuple(item.action_id for item in self.exposure_bindings)
        if action_ids != binding_ids:
            raise ValueError("exposure bindings must exactly match execution action order")
        if type(self.intent_evidence_json) is not str or not self.intent_evidence_json:
            raise ValueError("intent_evidence_json must be non-empty canonical JSON")


_ORIGINAL_PREPARE_PAPER_VALUE_ACTION = (
    PaperExecutionAdoptionRuntime.prepare_paper_value_action
)
_ORIGINAL_EXPECTED_RUN_ID = PaperExecutionAdoptionRuntime.expected_run_id
_ORIGINAL_EXECUTE = PaperExecutionAdoptionRuntime.execute
_ORIGINAL_ON_MARKET_EVENT = PaperValueAgent.on_market_event
_PAPER_VALUE_ACTION = "OPEN_PAPER_VALUE_TICKET"
_FRAME_MISSING = object()


def _describe_paper_value_action(
    self: PaperExecutionAdoptionRuntime,
    **kwargs,
) -> PaperValueExecutionDescriptor:
    """Return exact audit data without minting positive execution authority."""

    prepared = _ORIGINAL_PREPARE_PAPER_VALUE_ACTION(self, **kwargs)
    # The legacy helper used to mint authority immediately from caller-supplied
    # values. Revoke that transient mint before anything is returned publicly.
    self._prepared_authorities.pop(id(prepared), None)
    return PaperValueExecutionDescriptor(
        execution_plan=prepared.execution_plan,
        exposure_bindings=prepared.exposure_bindings,
        intent_evidence_json=prepared.intent_evidence_json,
    )


def _expected_run_id(
    self: PaperExecutionAdoptionRuntime,
    prepared,
    trigger_id: str,
) -> str:
    if isinstance(prepared, PaperValueExecutionDescriptor):
        # A deterministic run id is evidence metadata, not action authority.
        return _paper_impl._run_id(
            prepared.execution_plan,
            trigger_id,
            self.config,
        )
    return _ORIGINAL_EXPECTED_RUN_ID(self, prepared, trigger_id)


def _has_durable_execution_reservation(
    runtime: PaperExecutionAdoptionRuntime,
    decision_id: str,
) -> bool:
    """Return true only when #623 already durably reserved this exact trigger."""

    try:
        events = runtime.ledger.events()
    except Exception:
        return False
    return any(
        item.get("event_type") == "RUN_RESERVED"
        and item.get("payload", {}).get("trigger_id") == decision_id
        for item in events
    )


def _legacy_general_risk_authority(
    agent: PaperValueAgent,
    event,
    context,
    *,
    decision_id: str,
) -> str | None:
    """Prevent a recovered GENERAL record from suppressing its first risk gate.

    Fresh decisions are still gated by PaperValueAgent itself. A recovered GENERAL
    DecisionRecord is audit evidence, not historical risk authority: before the
    first durable #623 run it must pass the exact current PaperRiskPolicy for its
    recovered requested stake. Once #623 has already reserved the exact trigger,
    restart may resume that same execution without resizing against post-attempt
    exposure.
    """

    runtime = context.paper_execution
    ledger = context.decision_ledger
    if runtime is None or ledger is None:
        return None

    path = getattr(ledger, "path", None)
    if path is not None and not path.exists():
        return "fresh-decision"
    matches = tuple(
        record
        for record in ledger.verified_records()
        if record.decision_id == decision_id
    )
    if len(matches) > 1:
        raise PaperExecutionAdoptionError(
            "duplicate durable paper-value decision identity"
        )
    if not matches:
        return "fresh-decision"

    record = matches[0]
    if (
        record.replay_run_id != context.replay_run_id
        or record.agent != PaperValueAgent.name
        or record.action != _PAPER_VALUE_ACTION
        or record.observed_ts != event.observed_ts
        or record.payload.get("material_action_id") != decision_id
        or record.payload.get("quote_key") != event.quote_key
    ):
        raise PaperExecutionAdoptionError(
            "durable paper-value decision identity changed across restart"
        )
    if _has_durable_execution_reservation(runtime, decision_id):
        return "durable-execution-recovery"

    try:
        chosen_stake = Decimal(str(record.payload["requested_stake"]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise PaperExecutionAdoptionError(
            "durable paper-value decision lacks canonical requested stake"
        ) from exc
    if not chosen_stake.is_finite() or chosen_stake <= 0:
        raise PaperExecutionAdoptionError(
            "durable paper-value requested stake is invalid"
        )

    risk = agent.risk_policy.evaluate(
        context.paper_book,
        chosen_stake,
        context=None,
    )
    if not risk.allowed:
        return None
    return "fresh-risk-evaluation"


def _canonical_agent_call(
    runtime: PaperExecutionAdoptionRuntime,
    *,
    decision_id: str,
    started_at: str,
) -> tuple[JsonlDecisionLedger, object, PaperRiskPolicy, str, str]:
    """Resolve authority from the executing canonical agent code path itself.

    No module/closure/context membership container is positive authority. A caller
    can invoke the canonical agent, but then it must actually traverse that code's
    risk/decision gates. A direct public runtime call has no matching execution
    frame and therefore fails closed.
    """

    current = inspect.currentframe()
    original_frame = None
    wrapper_frame = None
    try:
        frame = current.f_back if current is not None else None
        while frame is not None:
            if original_frame is None and frame.f_code is _ORIGINAL_ON_MARKET_EVENT.__code__:
                original_frame = frame
            elif wrapper_frame is None and frame.f_code is _on_market_event.__code__:
                wrapper_frame = frame
            frame = frame.f_back

        if original_frame is None:
            raise PaperExecutionAdoptionError(
                "paper-value execution descriptor requires the canonical PaperValueAgent execution path"
            )

        local = original_frame.f_locals
        agent = local.get("self")
        context = local.get("context")
        event = local.get("event")
        if not isinstance(agent, PaperValueAgent):
            raise PaperExecutionAdoptionError(
                "paper-value execution caller is not canonical PaperValueAgent"
            )
        if context is None or context.paper_execution is not runtime:
            raise PaperExecutionAdoptionError(
                "paper-value execution runtime is not bound to canonical AgentContext"
            )
        ledger = context.decision_ledger
        if not isinstance(ledger, JsonlDecisionLedger):
            raise PaperExecutionAdoptionError(
                "paper-value execution requires canonical JsonlDecisionLedger authority"
            )
        risk_policy = agent.risk_policy
        if not isinstance(risk_policy, PaperRiskPolicy):
            raise PaperExecutionAdoptionError(
                "paper-value execution requires canonical PaperRiskPolicy authority"
            )
        if (
            event is None
            or event.observed_ts != started_at
            or local.get("material_action_id") != decision_id
            or agent._material_action_id(context, event) != decision_id
        ):
            raise PaperExecutionAdoptionError(
                "paper-value canonical call identity does not match execution descriptor"
            )

        goal = local.get("goal", _FRAME_MISSING)
        persisted = local.get("persisted", _FRAME_MISSING)
        chosen_stake = local.get("chosen_stake", _FRAME_MISSING)
        if goal is _FRAME_MISSING or goal is not risk_policy.economic_goal:
            raise PaperExecutionAdoptionError(
                "paper-value canonical call risk authority is inconsistent"
            )
        if chosen_stake is _FRAME_MISSING:
            raise PaperExecutionAdoptionError(
                "paper-value canonical call lacks chosen stake authority"
            )

        # A fresh call appends its DecisionRecord before execute(), so ``persisted``
        # is already non-None here. The local RiskDecision is the causal proof that
        # this invocation actually traversed the fresh gate before publication.
        risk = local.get("risk", _FRAME_MISSING)
        if risk is not _FRAME_MISSING and getattr(risk, "allowed", None) is True:
            risk_authority = "fresh-risk-evaluation"
        elif _has_durable_execution_reservation(runtime, decision_id):
            risk_authority = "durable-execution-recovery"
        elif goal is None:
            if persisted is _FRAME_MISSING or wrapper_frame is None:
                raise PaperExecutionAdoptionError(
                    "recovered legacy paper-value decision lacks canonical risk re-evaluation"
                )
            wrapper = wrapper_frame.f_locals
            if (
                wrapper.get("self") is not agent
                or wrapper.get("event") is not event
                or wrapper.get("context") is not context
                or wrapper.get("decision_id") != decision_id
                or wrapper.get("risk_authority") != "fresh-risk-evaluation"
            ):
                raise PaperExecutionAdoptionError(
                    "recovered legacy paper-value risk authority is not bound to this call"
                )
            risk_authority = "fresh-risk-evaluation"
        else:
            proposal_context = local.get("proposal_context", _FRAME_MISSING)
            if proposal_context is _FRAME_MISSING:
                raise PaperExecutionAdoptionError(
                    "recovered economic paper-value decision lacks proposal risk evidence"
                )
            recovered_risk = risk_policy.evaluate(
                context.paper_book,
                chosen_stake,
                context=proposal_context,
            )
            if not recovered_risk.allowed:
                raise PaperExecutionAdoptionError(
                    "recovered economic paper-value decision no longer proves its first execution risk gate"
                )
            risk_authority = "fresh-risk-evaluation"

        return (
            ledger,
            goal,
            risk_policy,
            context.replay_run_id,
            risk_authority,
        )
    finally:
        # Frame references retain local object graphs; break them deterministically.
        del current
        try:
            del frame
        except UnboundLocalError:
            pass
        del original_frame
        del wrapper_frame


def _resolve_durable_paper_value_decision(
    self: PaperExecutionAdoptionRuntime,
    *,
    descriptor: PaperValueExecutionDescriptor,
    trigger_id: str,
    started_at: str,
) -> DecisionRecord:
    decision_id = descriptor.execution_plan.decision_id
    if trigger_id != decision_id:
        raise PaperExecutionAdoptionError(
            "paper-value trigger does not match durable decision identity"
        )
    if descriptor.execution_plan.created_at != started_at:
        raise PaperExecutionAdoptionError(
            "paper-value execution time does not match durable decision observation"
        )

    ledger, goal, risk_policy, replay_run_id, risk_authority = _canonical_agent_call(
        self,
        decision_id=decision_id,
        started_at=started_at,
    )

    try:
        if goal is None:
            if risk_authority == "durable-execution-recovery" and not _has_durable_execution_reservation(
                self, decision_id
            ):
                raise PaperExecutionAdoptionError(
                    "legacy paper-value recovery lacks exact durable #623 execution authority"
                )
            matches = tuple(
                record
                for record in ledger.verified_records()
                if record.decision_id == decision_id
            )
            if len(matches) != 1:
                raise PaperExecutionAdoptionError(
                    "paper-value execution requires one exact durable decision"
                )
            record = matches[0]
        else:
            record = ledger.verified_economic_decision_for_material_action(
                decision_id,
                goal,
                risk_policy=risk_policy,
            )
            if record is None:
                raise PaperExecutionAdoptionError(
                    "paper-value execution requires exact durable economic authority"
                )
    except PaperExecutionAdoptionError:
        raise
    except Exception as exc:
        raise PaperExecutionAdoptionError(
            "paper-value durable decision authority could not be verified"
        ) from exc

    if (
        record.replay_run_id != replay_run_id
        or record.agent != PaperValueAgent.name
        or record.action != _PAPER_VALUE_ACTION
        or record.observed_ts != started_at
        or record.decision_id != decision_id
    ):
        raise PaperExecutionAdoptionError(
            "paper-value durable decision identity does not match execution descriptor"
        )

    if len(descriptor.execution_plan.actions) != 1:
        raise PaperExecutionAdoptionError(
            "legacy paper-value descriptor must contain exactly one execution action"
        )
    action = descriptor.execution_plan.actions[0]
    expected_run_id = _paper_impl._run_id(
        descriptor.execution_plan,
        trigger_id,
        self.config,
    )
    payload = record.payload
    expected = {
        "material_action_id": decision_id,
        "requested_stake": str(action.requested_stake),
        "execution_plan_id": descriptor.execution_plan.plan_id,
        "execution_plan_fingerprint": descriptor.execution_plan.fingerprint,
        "execution_run_id": expected_run_id,
        "execution_authority_json": descriptor.intent_evidence_json,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise PaperExecutionAdoptionError(
                f"paper-value durable decision conflicts with execution descriptor: {key}"
            )
    return record


def _authorize_descriptor(
    self: PaperExecutionAdoptionRuntime,
    *,
    descriptor: PaperValueExecutionDescriptor,
    trigger_id: str,
    started_at: str,
) -> PreparedPaperExecution:
    # Successful resolution is the positive proof: exact durable decision truth is
    # re-resolved while the canonical agent frame is live, and restart either
    # passes its exact risk gate now or resumes an already-reserved exact #623 run.
    _resolve_durable_paper_value_decision(
        self,
        descriptor=descriptor,
        trigger_id=trigger_id,
        started_at=started_at,
    )
    return self._mint_prepared(
        PreparedPaperExecution(
            execution_plan=descriptor.execution_plan,
            exposure_bindings=descriptor.exposure_bindings,
            intent_evidence_json=descriptor.intent_evidence_json,
        )
    )


def _execute(
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
    if not isinstance(prepared, PaperValueExecutionDescriptor):
        return _ORIGINAL_EXECUTE(
            self,
            prepared=prepared,
            trigger_id=trigger_id,
            started_at=started_at,
            materialize_exposure=materialize_exposure,
            observations=observations,
            evidence_registry=evidence_registry,
            suspended_action_ids=suspended_action_ids,
        )

    authorized = _authorize_descriptor(
        self,
        descriptor=prepared,
        trigger_id=trigger_id,
        started_at=started_at,
    )
    try:
        return _ORIGINAL_EXECUTE(
            self,
            prepared=authorized,
            trigger_id=trigger_id,
            started_at=started_at,
            materialize_exposure=materialize_exposure,
            observations=observations,
            evidence_registry=evidence_registry,
            suspended_action_ids=suspended_action_ids,
        )
    finally:
        # Execution authority is one-shot/re-resolvable from durable truth; do not
        # retain the transient PreparedPaperExecution mint after the call returns.
        self._prepared_authorities.pop(id(authorized), None)


def _on_market_event(self: PaperValueAgent, event, context) -> None:
    runtime = context.paper_execution
    if runtime is None:
        return _ORIGINAL_ON_MARKET_EVENT(self, event, context)

    ledger = context.decision_ledger
    if ledger is None:
        context.notes.append(
            "paper-value material action withheld: durable decision authority is unavailable"
        )
        return

    decision_id = self._material_action_id(context, event)
    risk_authority = "economic-decision"
    if self.risk_policy.economic_goal is None:
        risk_authority = _legacy_general_risk_authority(
            self,
            event,
            context,
            decision_id=decision_id,
        )
        if risk_authority is None:
            return

    # No mutable membership/token is installed here. The execute seam proves that
    # this exact wrapper and the original canonical agent frame are currently live.
    return _ORIGINAL_ON_MARKET_EVENT(self, event, context)


def _install() -> None:
    if getattr(
        PaperExecutionAdoptionRuntime,
        "_autosport_paper_value_authority_installed",
        False,
    ):
        return
    PaperExecutionAdoptionRuntime.prepare_paper_value_action = (
        _describe_paper_value_action
    )
    PaperExecutionAdoptionRuntime.expected_run_id = _expected_run_id
    PaperExecutionAdoptionRuntime.execute = _execute
    PaperExecutionAdoptionRuntime._autosport_paper_value_authority_installed = True
    PaperValueAgent.on_market_event = _on_market_event


_install()
