from __future__ import annotations

from contextvars import ContextVar
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


@dataclass(frozen=True, slots=True)
class _PaperValueCallAuthority:
    """Call-scoped identity for one canonical PaperValueAgent invocation.

    Positive durable authority remains the DecisionLedger/#623 evidence. This
    object only prevents the public runtime from treating caller-assignable
    attributes as proof that the canonical agent/risk path is active.
    """

    runtime: PaperExecutionAdoptionRuntime
    ledger: JsonlDecisionLedger
    goal: object
    risk_policy: PaperRiskPolicy
    replay_run_id: str
    decision_id: str
    started_at: str
    risk_authority: str


_ORIGINAL_PREPARE_PAPER_VALUE_ACTION = (
    PaperExecutionAdoptionRuntime.prepare_paper_value_action
)
_ORIGINAL_EXPECTED_RUN_ID = PaperExecutionAdoptionRuntime.expected_run_id
_ORIGINAL_EXECUTE = PaperExecutionAdoptionRuntime.execute
_ORIGINAL_ON_MARKET_EVENT = PaperValueAgent.on_market_event
_PAPER_VALUE_ACTION = "OPEN_PAPER_VALUE_TICKET"
_ACTIVE_PAPER_VALUE_CALL: ContextVar[_PaperValueCallAuthority | None] = ContextVar(
    "autosport_active_paper_value_call",
    default=None,
)


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
    """Prove a goal-less legacy stake cannot bypass the canonical risk gate.

    A recovered GENERAL DecisionRecord is audit evidence, not a historical risk
    capability. Before the first durable #623 run, evaluate the exact recovered
    requested stake against the current canonical PaperBook. Once #623 has already
    reserved the exact trigger, recovery may resume that same durable execution
    without resizing against post-attempt exposure.
    """

    runtime = context.paper_execution
    ledger = context.decision_ledger
    if runtime is None or ledger is None:
        return None
    if _has_durable_execution_reservation(runtime, decision_id):
        return "durable-execution-recovery"

    chosen_stake = agent.stake
    path = getattr(ledger, "path", None)
    if path is None or path.exists():
        matches = tuple(
            record
            for record in ledger.verified_records()
            if record.decision_id == decision_id
        )
        if len(matches) > 1:
            raise PaperExecutionAdoptionError(
                "duplicate durable paper-value decision identity"
            )
        if matches:
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


def _resolve_durable_paper_value_decision(
    self: PaperExecutionAdoptionRuntime,
    *,
    descriptor: PaperValueExecutionDescriptor,
    trigger_id: str,
    started_at: str,
) -> DecisionRecord:
    authority = _ACTIVE_PAPER_VALUE_CALL.get()
    if authority is None or authority.runtime is not self:
        raise PaperExecutionAdoptionError(
            "paper-value execution descriptor requires an active canonical PaperValueAgent call"
        )

    ledger = authority.ledger
    goal = authority.goal
    risk_policy = authority.risk_policy
    replay_run_id = authority.replay_run_id
    if not isinstance(ledger, JsonlDecisionLedger):
        raise PaperExecutionAdoptionError(
            "paper-value execution requires canonical JsonlDecisionLedger authority"
        )
    if not isinstance(risk_policy, PaperRiskPolicy):
        raise PaperExecutionAdoptionError(
            "paper-value execution requires canonical PaperRiskPolicy authority"
        )

    decision_id = descriptor.execution_plan.decision_id
    if (
        authority.decision_id != decision_id
        or authority.started_at != started_at
        or trigger_id != decision_id
    ):
        raise PaperExecutionAdoptionError(
            "paper-value call authority does not match execution descriptor identity"
        )
    if descriptor.execution_plan.created_at != started_at:
        raise PaperExecutionAdoptionError(
            "paper-value execution time does not match durable decision observation"
        )

    try:
        if goal is None:
            if authority.risk_authority not in {
                "fresh-risk-evaluation",
                "durable-execution-recovery",
            }:
                raise PaperExecutionAdoptionError(
                    "legacy paper-value execution lacks canonical risk authority"
                )
            if (
                authority.risk_authority == "durable-execution-recovery"
                and not _has_durable_execution_reservation(self, decision_id)
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
    # Successful resolution is the positive proof: the exact durable decision is
    # re-resolved inside the canonical call, and goal-less execution additionally
    # requires a fresh risk pass or an already-reserved exact #623 recovery run.
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
    if _ACTIVE_PAPER_VALUE_CALL.get() is not None:
        raise PaperExecutionAdoptionError(
            "nested paper-value durable decision authority is not permitted"
        )

    decision_id = self._material_action_id(context, event)
    goal = self.risk_policy.economic_goal
    if goal is None:
        risk_authority = _legacy_general_risk_authority(
            self,
            event,
            context,
            decision_id=decision_id,
        )
        if risk_authority is None:
            return
    else:
        # Fresh goal-active decisions are risk-evaluated by the original canonical
        # path; recovered ones must pass verified_economic_decision below before
        # any descriptor can be authorized.
        risk_authority = "economic-decision"

    token = _ACTIVE_PAPER_VALUE_CALL.set(
        _PaperValueCallAuthority(
            runtime=runtime,
            ledger=ledger,
            goal=goal,
            risk_policy=self.risk_policy,
            replay_run_id=context.replay_run_id,
            decision_id=decision_id,
            started_at=event.observed_ts,
            risk_authority=risk_authority,
        )
    )
    try:
        _ORIGINAL_ON_MARKET_EVENT(self, event, context)
    finally:
        _ACTIVE_PAPER_VALUE_CALL.reset(token)


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
