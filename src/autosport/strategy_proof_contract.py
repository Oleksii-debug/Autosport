"""Class-specific proof taxonomy for the canonical Autosport Opportunity contract.

This module is deliberately authority-light. It reuses ``StrategyClass`` and
``OpportunityDecision`` from :mod:`autosport.opportunity`; it does not mint a
second strategy/decision vocabulary. It answers only whether the evidence
*classes* required by one canonical strategy class are present. It does not
validate the evidence itself, prove economics, size stakes, mutate portfolio
state, relax EconomicGoal/RiskPolicy authority, or authorize execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from .opportunity import OpportunityDecision, StrategyClass


class StrategyProofContractError(ValueError):
    """Raised when strategy-proof inputs are non-canonical."""


class ProofRequirement(str, Enum):
    """Evidence classes a strategy may require before downstream gates."""

    CAUSAL_OPPORTUNITY_EVIDENCE = "causal_opportunity_evidence"
    MARKET_IDENTITY_SCOPE = "market_identity_scope"
    QUOTE_PROVENANCE_FRESHNESS = "quote_provenance_freshness"
    PORTFOLIO_IDENTITY = "portfolio_identity"
    DECISION_TIME = "decision_time"
    STRATEGY_MODEL_CONFIG_IDENTITY = "strategy_model_config_identity"
    TRUTH_COMPLETENESS = "truth_completeness"
    RISK_POLICY_RESULT = "risk_policy_result"

    FORECAST_PROBABILITY = "forecast_probability"
    FORECAST_CAUSAL_PROVENANCE = "forecast_causal_provenance"
    FORECAST_UNCERTAINTY_CALIBRATION = "forecast_uncertainty_calibration"

    LIVE_STATE_CONTINUITY = "live_state_continuity"
    PRICE_MOVEMENT_CAUSALITY = "price_movement_causality"

    EXECUTABLE_QUOTE_SET = "executable_quote_set"
    COMPLETE_TERMINAL_OUTCOME_SPACE = "complete_terminal_outcome_space"
    STAKE_VECTOR = "stake_vector"
    SETTLEMENT_SEMANTICS = "settlement_semantics"
    APPLICABLE_COSTS = "applicable_costs"
    PROVIDER_LIMITS = "provider_limits"
    STAKE_GRANULARITY = "stake_granularity"
    MINIMUM_TERMINAL_NET_PNL = "minimum_terminal_net_pnl"

    EXISTING_EXPOSURE = "existing_exposure"
    RESIDUAL_RISK_IMPROVEMENT = "residual_risk_improvement"

    HYBRID_COMPONENT_EVIDENCE = "hybrid_component_evidence"


_SHARED: Final[frozenset[ProofRequirement]] = frozenset(
    {
        ProofRequirement.CAUSAL_OPPORTUNITY_EVIDENCE,
        ProofRequirement.MARKET_IDENTITY_SCOPE,
        ProofRequirement.QUOTE_PROVENANCE_FRESHNESS,
        ProofRequirement.PORTFOLIO_IDENTITY,
        ProofRequirement.DECISION_TIME,
        ProofRequirement.STRATEGY_MODEL_CONFIG_IDENTITY,
        ProofRequirement.TRUTH_COMPLETENESS,
        ProofRequirement.RISK_POLICY_RESULT,
    }
)

_PREDICTIVE: Final[frozenset[ProofRequirement]] = frozenset(
    {
        ProofRequirement.FORECAST_PROBABILITY,
        ProofRequirement.FORECAST_CAUSAL_PROVENANCE,
        ProofRequirement.FORECAST_UNCERTAINTY_CALIBRATION,
    }
)

_LIVE: Final[frozenset[ProofRequirement]] = frozenset(
    {
        ProofRequirement.LIVE_STATE_CONTINUITY,
        ProofRequirement.PRICE_MOVEMENT_CAUSALITY,
    }
)

_OUTCOME_STRUCTURE: Final[frozenset[ProofRequirement]] = frozenset(
    {
        ProofRequirement.EXECUTABLE_QUOTE_SET,
        ProofRequirement.COMPLETE_TERMINAL_OUTCOME_SPACE,
        ProofRequirement.STAKE_VECTOR,
        ProofRequirement.SETTLEMENT_SEMANTICS,
        ProofRequirement.APPLICABLE_COSTS,
        ProofRequirement.PROVIDER_LIMITS,
        ProofRequirement.STAKE_GRANULARITY,
        ProofRequirement.MINIMUM_TERMINAL_NET_PNL,
    }
)

_HEDGE: Final[frozenset[ProofRequirement]] = frozenset(
    {
        ProofRequirement.EXECUTABLE_QUOTE_SET,
        ProofRequirement.STAKE_VECTOR,
        ProofRequirement.SETTLEMENT_SEMANTICS,
        ProofRequirement.APPLICABLE_COSTS,
        ProofRequirement.PROVIDER_LIMITS,
        ProofRequirement.STAKE_GRANULARITY,
        ProofRequirement.EXISTING_EXPOSURE,
        ProofRequirement.RESIDUAL_RISK_IMPROVEMENT,
    }
)

_CLASS_REQUIREMENTS: Final[dict[StrategyClass, frozenset[ProofRequirement]]] = {
    StrategyClass.PREDICTIVE_EDGE: _SHARED,
    StrategyClass.LIVE_PRICE_MOVEMENT: _SHARED | _LIVE,
    StrategyClass.ARBITRAGE: _SHARED | _OUTCOME_STRUCTURE,
    StrategyClass.DUTCHING: _SHARED | _OUTCOME_STRUCTURE,
    StrategyClass.HEDGE_REBALANCE: _SHARED | _HEDGE,
    StrategyClass.HYBRID: _SHARED
    | frozenset({ProofRequirement.HYBRID_COMPONENT_EVIDENCE}),
}


def _strategy_class(value: object) -> StrategyClass:
    if not isinstance(value, StrategyClass):
        raise StrategyProofContractError("strategy_class must be a StrategyClass")
    return value


def _probability_claim(value: object) -> bool:
    if type(value) is not bool:
        raise StrategyProofContractError("claims_probability_edge must be a bool")
    return value


def _validate_probability_claim(
    strategy_class: StrategyClass,
    claims_probability_edge: bool,
) -> None:
    if strategy_class is StrategyClass.PREDICTIVE_EDGE and not claims_probability_edge:
        raise StrategyProofContractError(
            "PREDICTIVE_EDGE must claim probability edge"
        )
    if (
        strategy_class not in {StrategyClass.PREDICTIVE_EDGE, StrategyClass.HYBRID}
        and claims_probability_edge
    ):
        raise StrategyProofContractError(
            "only PREDICTIVE_EDGE or HYBRID may claim probability edge"
        )


def _proofs(value: object) -> frozenset[ProofRequirement]:
    if not isinstance(value, frozenset):
        raise StrategyProofContractError("present_proofs must be a frozenset")
    for proof in value:
        if not isinstance(proof, ProofRequirement):
            raise StrategyProofContractError(
                "present_proofs must contain only ProofRequirement values"
            )
    return value


def _ordered(proofs: frozenset[ProofRequirement]) -> tuple[ProofRequirement, ...]:
    return tuple(sorted(proofs, key=lambda proof: proof.value))


def _required_for(
    strategy_class: StrategyClass,
    claims_probability_edge: bool,
) -> frozenset[ProofRequirement]:
    canonical_class = _strategy_class(strategy_class)
    canonical_claim = _probability_claim(claims_probability_edge)
    _validate_probability_claim(canonical_class, canonical_claim)

    required = _CLASS_REQUIREMENTS[canonical_class]
    if canonical_claim:
        required = required | _PREDICTIVE
    return required


@dataclass(frozen=True, slots=True)
class StrategyProofContract:
    """Immutable proof-class contract keyed by canonical Opportunity identity."""

    strategy_class: StrategyClass
    claims_probability_edge: bool
    required_proofs: tuple[ProofRequirement, ...]

    def __post_init__(self) -> None:
        canonical_class = _strategy_class(self.strategy_class)
        canonical_claim = _probability_claim(self.claims_probability_edge)
        expected = _ordered(_required_for(canonical_class, canonical_claim))

        if not isinstance(self.required_proofs, tuple):
            raise StrategyProofContractError("required_proofs must be a tuple")
        if any(not isinstance(item, ProofRequirement) for item in self.required_proofs):
            raise StrategyProofContractError(
                "required_proofs must contain only ProofRequirement values"
            )
        if self.required_proofs != _ordered(frozenset(self.required_proofs)):
            raise StrategyProofContractError(
                "required_proofs must be unique and use canonical lexical order"
            )
        if self.required_proofs != expected:
            raise StrategyProofContractError(
                "required_proofs do not match the canonical strategy-class contract"
            )

    @property
    def forecast_required(self) -> bool:
        return ProofRequirement.FORECAST_PROBABILITY in self.required_proofs

    @property
    def complete_terminal_state_required(self) -> bool:
        return (
            ProofRequirement.COMPLETE_TERMINAL_OUTCOME_SPACE
            in self.required_proofs
        )


@dataclass(frozen=True, slots=True)
class StrategyProofEvaluation:
    """Deterministic proof-presence evaluation, never execution authority."""

    strategy_class: StrategyClass
    claims_probability_edge: bool
    required_proofs: tuple[ProofRequirement, ...]
    present_proofs: tuple[ProofRequirement, ...]
    missing_proofs: tuple[ProofRequirement, ...]
    proof_contract_satisfied: bool
    positive_action_candidate: bool
    proof_gate_decision: OpportunityDecision
    execution_authorized: bool = False

    def __post_init__(self) -> None:
        canonical_class = _strategy_class(self.strategy_class)
        canonical_claim = _probability_claim(self.claims_probability_edge)
        expected_required = _ordered(_required_for(canonical_class, canonical_claim))

        for name, value in (
            ("required_proofs", self.required_proofs),
            ("present_proofs", self.present_proofs),
            ("missing_proofs", self.missing_proofs),
        ):
            if not isinstance(value, tuple):
                raise StrategyProofContractError(f"{name} must be a tuple")
            if any(not isinstance(item, ProofRequirement) for item in value):
                raise StrategyProofContractError(
                    f"{name} must contain only ProofRequirement values"
                )
            if value != _ordered(frozenset(value)):
                raise StrategyProofContractError(
                    f"{name} must be unique and use canonical lexical order"
                )

        if self.required_proofs != expected_required:
            raise StrategyProofContractError(
                "required_proofs do not match the canonical strategy-class contract"
            )

        required_set = frozenset(self.required_proofs)
        present_set = frozenset(self.present_proofs)
        expected_missing = required_set - present_set
        if self.missing_proofs != _ordered(expected_missing):
            raise StrategyProofContractError(
                "missing_proofs must equal canonical required-minus-present"
            )

        expected_satisfied = not expected_missing
        if self.proof_contract_satisfied is not expected_satisfied:
            raise StrategyProofContractError(
                "proof_contract_satisfied does not match missing_proofs"
            )
        if self.positive_action_candidate is not expected_satisfied:
            raise StrategyProofContractError(
                "positive_action_candidate does not match proof state"
            )

        if not isinstance(self.proof_gate_decision, OpportunityDecision):
            raise StrategyProofContractError(
                "proof_gate_decision must be an OpportunityDecision"
            )
        expected_decision = (
            OpportunityDecision.ACTIONABLE
            if expected_satisfied
            else OpportunityDecision.WAIT
        )
        if self.proof_gate_decision is not expected_decision:
            raise StrategyProofContractError(
                "proof_gate_decision must fail closed to WAIT"
            )

        if type(self.execution_authorized) is not bool:
            raise StrategyProofContractError("execution_authorized must be a bool")
        if self.execution_authorized:
            raise StrategyProofContractError(
                "strategy proof evaluation cannot authorize execution"
            )


def proof_contract_for(
    strategy_class: StrategyClass,
    *,
    claims_probability_edge: bool,
) -> StrategyProofContract:
    """Return canonical proof obligations for one Opportunity strategy identity."""

    canonical_class = _strategy_class(strategy_class)
    canonical_claim = _probability_claim(claims_probability_edge)
    required = _required_for(canonical_class, canonical_claim)
    return StrategyProofContract(
        strategy_class=canonical_class,
        claims_probability_edge=canonical_claim,
        required_proofs=_ordered(required),
    )


def evaluate_strategy_proofs(
    strategy_class: StrategyClass,
    present_proofs: frozenset[ProofRequirement],
    *,
    claims_probability_edge: bool,
) -> StrategyProofEvaluation:
    """Evaluate proof-class presence without widening downstream authority."""

    contract = proof_contract_for(
        strategy_class,
        claims_probability_edge=claims_probability_edge,
    )
    canonical_present = _proofs(present_proofs)
    required_set = frozenset(contract.required_proofs)
    missing = required_set - canonical_present
    satisfied = not missing

    return StrategyProofEvaluation(
        strategy_class=contract.strategy_class,
        claims_probability_edge=contract.claims_probability_edge,
        required_proofs=contract.required_proofs,
        present_proofs=_ordered(canonical_present),
        missing_proofs=_ordered(missing),
        proof_contract_satisfied=satisfied,
        positive_action_candidate=satisfied,
        proof_gate_decision=(
            OpportunityDecision.ACTIONABLE
            if satisfied
            else OpportunityDecision.WAIT
        ),
        execution_authorized=False,
    )
