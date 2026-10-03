"""Class-specific proof taxonomy for the canonical Autosport Opportunity contract.

This module is deliberately authority-light. It reuses ``StrategyClass`` and
``OpportunityDecision`` from :mod:`autosport.opportunity`; it does not mint a
second strategy/decision vocabulary. It enumerates the proof classes required
by one canonical strategy class and can report only whether caller-supplied
requirement labels cover those names. Label coverage is not evidence presence
or validity. This module does not prove economics, size stakes, mutate portfolio
state, relax EconomicGoal/RiskPolicy authority, or authorize execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final

from .opportunity import StrategyClass


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

_CLASS_REQUIREMENTS: Final = MappingProxyType({
    StrategyClass.PREDICTIVE_EDGE: _SHARED,
    StrategyClass.LIVE_PRICE_MOVEMENT: _SHARED | _LIVE,
    StrategyClass.ARBITRAGE: _SHARED | _OUTCOME_STRUCTURE,
    StrategyClass.DUTCHING: _SHARED | _OUTCOME_STRUCTURE,
    StrategyClass.HEDGE_REBALANCE: _SHARED | _HEDGE,
    StrategyClass.HYBRID: _SHARED
    | frozenset({ProofRequirement.HYBRID_COMPONENT_EVIDENCE}),
})


def _strategy_class(
    value: object,
    *,
    _strategy_type: type[StrategyClass] = StrategyClass,
    _error_type: type[StrategyProofContractError] = StrategyProofContractError,
) -> StrategyClass:
    if type(value) is not _strategy_type:
        raise _error_type("strategy_class must be a StrategyClass")
    return value


def _probability_claim(
    value: object,
    *,
    _error_type: type[StrategyProofContractError] = StrategyProofContractError,
) -> bool:
    if type(value) is not bool:
        raise _error_type("claims_probability_edge must be a bool")
    return value


def _validate_probability_claim(
    strategy_class: StrategyClass,
    claims_probability_edge: bool,
    *,
    _predictive: StrategyClass = StrategyClass.PREDICTIVE_EDGE,
    _hybrid: StrategyClass = StrategyClass.HYBRID,
    _error_type: type[StrategyProofContractError] = StrategyProofContractError,
) -> None:
    if strategy_class is _predictive and not claims_probability_edge:
        raise _error_type("PREDICTIVE_EDGE must claim probability edge")
    if strategy_class not in {_predictive, _hybrid} and claims_probability_edge:
        raise _error_type(
            "only PREDICTIVE_EDGE or HYBRID may claim probability edge"
        )


def _proofs(
    value: object,
    *,
    _proof_type: type[ProofRequirement] = ProofRequirement,
    _error_type: type[StrategyProofContractError] = StrategyProofContractError,
) -> frozenset[ProofRequirement]:
    if type(value) is not frozenset:
        raise _error_type("present_proofs must be a frozenset")
    for proof in value:
        if type(proof) is not _proof_type:
            raise _error_type(
                "present_proofs must contain only ProofRequirement values"
            )
    return value


def _ordered(proofs: frozenset[ProofRequirement]) -> tuple[ProofRequirement, ...]:
    return tuple(sorted(proofs, key=lambda proof: proof.value))


def _required_for(
    strategy_class: StrategyClass,
    claims_probability_edge: bool,
    *,
    _strategy_validator=_strategy_class,
    _claim_validator=_probability_claim,
    _claim_rule=_validate_probability_claim,
    _class_requirements=_CLASS_REQUIREMENTS,
    _predictive_requirements: frozenset[ProofRequirement] = _PREDICTIVE,
) -> frozenset[ProofRequirement]:
    canonical_class = _strategy_validator(strategy_class)
    canonical_claim = _claim_validator(claims_probability_edge)
    _claim_rule(canonical_class, canonical_claim)

    required = _class_requirements[canonical_class]
    if canonical_claim:
        required = required | _predictive_requirements
    return required


@dataclass(frozen=True, slots=True)
class StrategyProofContract:
    """Immutable proof-class contract keyed by canonical Opportunity identity."""

    strategy_class: StrategyClass
    claims_probability_edge: bool
    required_proofs: tuple[ProofRequirement, ...]

    def __post_init__(
        self,
        _strategy_validator=_strategy_class,
        _claim_validator=_probability_claim,
        _required_for_impl=_required_for,
        _ordered_impl=_ordered,
        _proof_type: type[ProofRequirement] = ProofRequirement,
        _error_type: type[StrategyProofContractError] = StrategyProofContractError,
    ) -> None:
        canonical_class = _strategy_validator(self.strategy_class)
        canonical_claim = _claim_validator(self.claims_probability_edge)
        expected = _ordered_impl(
            _required_for_impl(canonical_class, canonical_claim)
        )

        if type(self.required_proofs) is not tuple:
            raise _error_type("required_proofs must be a tuple")
        if any(type(item) is not _proof_type for item in self.required_proofs):
            raise _error_type(
                "required_proofs must contain only ProofRequirement values"
            )
        if self.required_proofs != _ordered_impl(frozenset(self.required_proofs)):
            raise _error_type(
                "required_proofs must be unique and use canonical lexical order"
            )
        if self.required_proofs != expected:
            raise _error_type(
                "required_proofs do not match the canonical strategy-class contract"
            )

    @property
    def forecast_required(
        self,
        _forecast_requirement: ProofRequirement = ProofRequirement.FORECAST_PROBABILITY,
    ) -> bool:
        return _forecast_requirement in self.required_proofs

    @property
    def complete_terminal_state_required(
        self,
        _terminal_requirement: ProofRequirement = (
            ProofRequirement.COMPLETE_TERMINAL_OUTCOME_SPACE
        ),
    ) -> bool:
        return _terminal_requirement in self.required_proofs


@dataclass(frozen=True, slots=True)
class StrategyProofEvaluation:
    """Deterministic requirement-label coverage, never evidence/action authority."""

    strategy_class: StrategyClass
    claims_probability_edge: bool
    required_proofs: tuple[ProofRequirement, ...]
    present_proofs: tuple[ProofRequirement, ...]
    missing_proofs: tuple[ProofRequirement, ...]
    required_labels_present: bool
    execution_authorized: bool = False

    def __post_init__(
        self,
        _strategy_validator=_strategy_class,
        _claim_validator=_probability_claim,
        _required_for_impl=_required_for,
        _ordered_impl=_ordered,
        _proof_type: type[ProofRequirement] = ProofRequirement,
        _error_type: type[StrategyProofContractError] = StrategyProofContractError,
    ) -> None:
        canonical_class = _strategy_validator(self.strategy_class)
        canonical_claim = _claim_validator(self.claims_probability_edge)
        expected_required = _ordered_impl(
            _required_for_impl(canonical_class, canonical_claim)
        )

        for name, value in (
            ("required_proofs", self.required_proofs),
            ("present_proofs", self.present_proofs),
            ("missing_proofs", self.missing_proofs),
        ):
            if type(value) is not tuple:
                raise _error_type(f"{name} must be a tuple")
            if any(type(item) is not _proof_type for item in value):
                raise _error_type(
                    f"{name} must contain only ProofRequirement values"
                )
            if value != _ordered_impl(frozenset(value)):
                raise _error_type(
                    f"{name} must be unique and use canonical lexical order"
                )

        if self.required_proofs != expected_required:
            raise _error_type(
                "required_proofs do not match the canonical strategy-class contract"
            )

        required_set = frozenset(self.required_proofs)
        present_set = frozenset(self.present_proofs)
        expected_missing = required_set - present_set
        if self.missing_proofs != _ordered_impl(expected_missing):
            raise _error_type(
                "missing_proofs must equal canonical required-minus-present"
            )

        expected_satisfied = not expected_missing
        if self.required_labels_present is not expected_satisfied:
            raise _error_type(
                "required_labels_present does not match missing_proofs"
            )

        if type(self.execution_authorized) is not bool:
            raise _error_type("execution_authorized must be a bool")
        if self.execution_authorized:
            raise _error_type(
                "strategy proof evaluation cannot authorize execution"
            )


def _build_public_strategy_proof_api(
    *,
    _strategy_validator=_strategy_class,
    _claim_validator=_probability_claim,
    _required_for_impl=_required_for,
    _ordered_impl=_ordered,
    _proofs_impl=_proofs,
    _contract_type: type[StrategyProofContract] = StrategyProofContract,
    _evaluation_type: type[StrategyProofEvaluation] = StrategyProofEvaluation,
):
    """Compose public taxonomy functions from canonical immutable roots."""

    def proof_contract_for(
        strategy_class: StrategyClass,
        *,
        claims_probability_edge: bool,
    ) -> StrategyProofContract:
        canonical_class = _strategy_validator(strategy_class)
        canonical_claim = _claim_validator(claims_probability_edge)
        required = _required_for_impl(canonical_class, canonical_claim)
        return _contract_type(
            strategy_class=canonical_class,
            claims_probability_edge=canonical_claim,
            required_proofs=_ordered_impl(required),
        )

    def evaluate_strategy_proofs(
        strategy_class: StrategyClass,
        present_proofs: frozenset[ProofRequirement],
        *,
        claims_probability_edge: bool,
    ) -> StrategyProofEvaluation:
        contract = proof_contract_for(
            strategy_class,
            claims_probability_edge=claims_probability_edge,
        )
        canonical_present = _proofs_impl(present_proofs)
        required_set = frozenset(contract.required_proofs)
        missing = required_set - canonical_present
        satisfied = not missing

        return _evaluation_type(
            strategy_class=contract.strategy_class,
            claims_probability_edge=contract.claims_probability_edge,
            required_proofs=contract.required_proofs,
            present_proofs=_ordered_impl(canonical_present),
            missing_proofs=_ordered_impl(missing),
            required_labels_present=satisfied,
            execution_authorized=False,
        )

    return proof_contract_for, evaluate_strategy_proofs


proof_contract_for, evaluate_strategy_proofs = _build_public_strategy_proof_api()
