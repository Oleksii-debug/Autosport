"""Strategy-family proof taxonomy for the generic Autosport opportunity contract.

This module is deliberately authority-light.  It answers only whether the
evidence *classes* required by one strategy family are present.  It does not
validate the underlying evidence, size stakes, mutate portfolio state, relax
EconomicGoal/RiskPolicy authority, or authorize execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class StrategyProofContractError(ValueError):
    """Raised when strategy-proof inputs are non-canonical."""


class StrategyFamily(str, Enum):
    """Strategy families admitted by the generic opportunity language."""

    PREDICTIVE_EDGE = "predictive_edge"
    LIVE_PRICE_MOVEMENT = "live_price_movement"
    ARBITRAGE = "arbitrage"
    DUTCHING = "dutching"
    HEDGE_REBALANCE = "hedge_rebalance"
    HYBRID = "hybrid"
    WAIT = "wait"


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

_FAMILY_REQUIREMENTS: Final[dict[StrategyFamily, frozenset[ProofRequirement]]] = {
    StrategyFamily.PREDICTIVE_EDGE: _SHARED | _PREDICTIVE,
    StrategyFamily.LIVE_PRICE_MOVEMENT: _SHARED | _LIVE,
    StrategyFamily.ARBITRAGE: _SHARED | _OUTCOME_STRUCTURE,
    StrategyFamily.DUTCHING: _SHARED | _OUTCOME_STRUCTURE,
    StrategyFamily.HEDGE_REBALANCE: _SHARED | _HEDGE,
    StrategyFamily.HYBRID: _SHARED
    | _PREDICTIVE
    | frozenset({ProofRequirement.HYBRID_COMPONENT_EVIDENCE}),
    StrategyFamily.WAIT: _SHARED,
}


def _family(value: object) -> StrategyFamily:
    if not isinstance(value, StrategyFamily):
        raise StrategyProofContractError("family must be a StrategyFamily")
    return value


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


@dataclass(frozen=True, slots=True)
class StrategyProofContract:
    """Immutable proof-class contract for one strategy family."""

    family: StrategyFamily
    required_proofs: tuple[ProofRequirement, ...]

    def __post_init__(self) -> None:
        _family(self.family)
        if not isinstance(self.required_proofs, tuple):
            raise StrategyProofContractError("required_proofs must be a tuple")
        if any(not isinstance(item, ProofRequirement) for item in self.required_proofs):
            raise StrategyProofContractError(
                "required_proofs must contain only ProofRequirement values"
            )
        if tuple(sorted(self.required_proofs, key=lambda item: item.value)) != (
            self.required_proofs
        ):
            raise StrategyProofContractError(
                "required_proofs must use canonical lexical order"
            )
        if len(set(self.required_proofs)) != len(self.required_proofs):
            raise StrategyProofContractError("required_proofs must be unique")

        expected = _ordered(_FAMILY_REQUIREMENTS[self.family])
        if self.required_proofs != expected:
            raise StrategyProofContractError(
                "required_proofs do not match the canonical strategy-family contract"
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

    @property
    def positive_action_family(self) -> bool:
        return self.family is not StrategyFamily.WAIT


@dataclass(frozen=True, slots=True)
class StrategyProofEvaluation:
    """Deterministic proof-presence evaluation.

    ``proof_contract_satisfied`` means only that the enumerated proof classes are
    present.  It is never execution authorization: each evidence object still has
    to pass its canonical validator and the existing portfolio/risk/execution
    authorities remain independently binding.
    """

    family: StrategyFamily
    required_proofs: tuple[ProofRequirement, ...]
    present_proofs: tuple[ProofRequirement, ...]
    missing_proofs: tuple[ProofRequirement, ...]
    proof_contract_satisfied: bool
    positive_action_candidate: bool
    fallback_family: StrategyFamily
    execution_authorized: bool = False

    def __post_init__(self) -> None:
        _family(self.family)
        _family(self.fallback_family)
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
            if tuple(sorted(value, key=lambda item: item.value)) != value:
                raise StrategyProofContractError(
                    f"{name} must use canonical lexical order"
                )
            if len(set(value)) != len(value):
                raise StrategyProofContractError(f"{name} must be unique")

        expected_required = _ordered(_FAMILY_REQUIREMENTS[self.family])
        if self.required_proofs != expected_required:
            raise StrategyProofContractError(
                "required_proofs do not match the canonical strategy-family contract"
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

        expected_positive = (
            self.family is not StrategyFamily.WAIT and expected_satisfied
        )
        if self.positive_action_candidate is not expected_positive:
            raise StrategyProofContractError(
                "positive_action_candidate does not match family/proof state"
            )

        expected_fallback = (
            self.family if expected_positive else StrategyFamily.WAIT
        )
        if self.fallback_family is not expected_fallback:
            raise StrategyProofContractError(
                "fallback_family must fail closed to WAIT"
            )

        if not isinstance(self.execution_authorized, bool):
            raise StrategyProofContractError("execution_authorized must be a bool")
        if self.execution_authorized:
            raise StrategyProofContractError(
                "strategy proof evaluation cannot authorize execution"
            )


def proof_contract_for(family: StrategyFamily) -> StrategyProofContract:
    """Return the canonical immutable proof contract for ``family``."""

    canonical_family = _family(family)
    return StrategyProofContract(
        family=canonical_family,
        required_proofs=_ordered(_FAMILY_REQUIREMENTS[canonical_family]),
    )


def evaluate_strategy_proofs(
    family: StrategyFamily,
    present_proofs: frozenset[ProofRequirement],
) -> StrategyProofEvaluation:
    """Evaluate proof-class presence without widening downstream authority."""

    contract = proof_contract_for(family)
    canonical_present = _proofs(present_proofs)
    required_set = frozenset(contract.required_proofs)
    missing = required_set - canonical_present
    satisfied = not missing
    positive_action_candidate = (
        contract.positive_action_family and satisfied
    )
    fallback_family = (
        contract.family if positive_action_candidate else StrategyFamily.WAIT
    )

    return StrategyProofEvaluation(
        family=contract.family,
        required_proofs=contract.required_proofs,
        present_proofs=_ordered(canonical_present),
        missing_proofs=_ordered(missing),
        proof_contract_satisfied=satisfied,
        positive_action_candidate=positive_action_candidate,
        fallback_family=fallback_family,
        execution_authorized=False,
    )
