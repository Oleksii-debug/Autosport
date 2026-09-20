from __future__ import annotations

from dataclasses import dataclass

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
_MISSING = object()
_PAPER_VALUE_ACTION = "OPEN_PAPER_VALUE_TICKET"


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


def _resolve_durable_paper_value_decision(
    self: PaperExecutionAdoptionRuntime,
    *,
    descriptor: PaperValueExecutionDescriptor,
    trigger_id: str,
    started_at: str,
) -> DecisionRecord:
    authority = getattr(self, "_paper_value_decision_context", None)
    if authority is None:
        raise PaperExecutionAdoptionError(
            "paper-value execution descriptor lacks active durable decision authority"
        )
    if type(authority) is not tuple or len(authority) != 4:
        raise PaperExecutionAdoptionError(
            "paper-value durable decision authority context is invalid"
        )
    ledger, goal, risk_policy, replay_run_id = authority
    if not isinstance(ledger, JsonlDecisionLedger):
        raise PaperExecutionAdoptionError(
            "paper-value execution requires canonical JsonlDecisionLedger authority"
        )
    if not isinstance(risk_policy, PaperRiskPolicy):
        raise PaperExecutionAdoptionError(
            "paper-value execution requires canonical PaperRiskPolicy authority"
        )

    decision_id = descriptor.execution_plan.decision_id
    if trigger_id != decision_id:
        raise PaperExecutionAdoptionError(
            "paper-value trigger does not match durable decision identity"
        )
    if descriptor.execution_plan.created_at != started_at:
        raise PaperExecutionAdoptionError(
            "paper-value execution time does not match durable decision observation"
        )

    try:
        if goal is None:
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
    # The return value itself is intentionally unused: successful resolution is
    # the authority proof, including exact economic-goal/risk provenance when an
    # EconomicGoalContract is active.
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
        # Execution authority is one-shot/re-resolvable from the durable decision;
        # do not retain an ambient capability after the canonical call returns.
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

    previous = getattr(runtime, "_paper_value_decision_context", _MISSING)
    if previous is not _MISSING:
        raise PaperExecutionAdoptionError(
            "nested paper-value durable decision authority is not permitted"
        )
    runtime._paper_value_decision_context = (
        ledger,
        self.risk_policy.economic_goal,
        self.risk_policy,
        context.replay_run_id,
    )
    try:
        _ORIGINAL_ON_MARKET_EVENT(self, event, context)
    finally:
        del runtime._paper_value_decision_context


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
