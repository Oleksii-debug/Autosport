"""Requirements-only scientific proof taxonomy for Autosport strategy families.

This module describes *which evidence classes are required* before evidence about a
strategy family can be considered scientifically interpretable.  It deliberately
owns no profitability decision, promotion gate, execution state, provider write,
portfolio mutation, or real-money claim.

Hybrid strategies are monotone: their contract is the exact union of every parent
family's requirements.  Combining families therefore cannot silently weaken a
parent proof obligation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Mapping


class StrategyProofError(ValueError):
    """Raised when a strategy-family proof contract cannot be resolved canonically."""


class StrategyFamily(str, Enum):
    """Canonical strategy families covered by this proof-requirements authority."""

    FORECAST = "forecast"
    LIVE_MOVEMENT = "live_movement"
    ARBITRAGE = "arbitrage"
    DUTCHING = "dutching"
    HEDGE = "hedge"
    WAIT = "wait"


class ProofRequirement(str, Enum):
    """Atomic evidence obligations used to compose family proof contracts."""

    LAWFUL_PROVENANCE = "lawful_provenance"
    CAUSAL_EVENT_TIME_ORDER = "causal_event_time_order"
    OUT_OF_SAMPLE_EVIDENCE = "out_of_sample_evidence"
    BASELINE_COMPARISON = "baseline_comparison"
    UNCERTAINTY_CALIBRATION = "uncertainty_calibration"
    MARKET_IDENTITY = "market_identity"
    QUOTE_FRESHNESS = "quote_freshness"
    COMPLETE_LEG_COVERAGE = "complete_leg_coverage"
    CONTEMPORANEOUS_QUOTES = "contemporaneous_quotes"
    COSTS_AND_FEES = "costs_and_fees"
    SETTLEMENT_RULE_CONSISTENCY = "settlement_rule_consistency"
    FILL_PRICE_SENSITIVITY = "fill_price_sensitivity"
    OUTCOME_COVERAGE = "outcome_coverage"
    RESIDUAL_EXPOSURE_VALIDATION = "residual_exposure_validation"
    POSITION_IDENTITY = "position_identity"
    EXPLICIT_NO_ACTION_SEMANTICS = "explicit_no_action_semantics"
    THRESHOLD_RATIONALE = "threshold_rationale"
    COUNTERFACTUAL_OPPORTUNITY_COST = "counterfactual_opportunity_cost"
    NO_PHANTOM_EXECUTION = "no_phantom_execution"


@dataclass(frozen=True, slots=True)
class StrategyProofContract:
    """Immutable evidence obligations for one or more canonical strategy families."""

    families: tuple[StrategyFamily, ...]
    requirements: frozenset[ProofRequirement]

    def __post_init__(self) -> None:
        if not self.families:
            raise StrategyProofError("proof contract must contain at least one family")
        if any(not isinstance(item, StrategyFamily) for item in self.families):
            raise StrategyProofError("families must contain StrategyFamily values")
        if len(set(self.families)) != len(self.families):
            raise StrategyProofError("families must be unique")
        canonical = tuple(family for family in StrategyFamily if family in self.families)
        if self.families != canonical:
            raise StrategyProofError("families must use canonical StrategyFamily order")
        if not isinstance(self.requirements, frozenset) or not self.requirements:
            raise StrategyProofError("requirements must be a non-empty frozenset")
        if any(not isinstance(item, ProofRequirement) for item in self.requirements):
            raise StrategyProofError("requirements must contain ProofRequirement values")


_REQUIREMENTS_BY_FAMILY = MappingProxyType(
    {
        StrategyFamily.FORECAST: frozenset(
            {
                ProofRequirement.LAWFUL_PROVENANCE,
                ProofRequirement.CAUSAL_EVENT_TIME_ORDER,
                ProofRequirement.OUT_OF_SAMPLE_EVIDENCE,
                ProofRequirement.BASELINE_COMPARISON,
                ProofRequirement.UNCERTAINTY_CALIBRATION,
            }
        ),
        StrategyFamily.LIVE_MOVEMENT: frozenset(
            {
                ProofRequirement.LAWFUL_PROVENANCE,
                ProofRequirement.MARKET_IDENTITY,
                ProofRequirement.QUOTE_FRESHNESS,
                ProofRequirement.CAUSAL_EVENT_TIME_ORDER,
                ProofRequirement.OUT_OF_SAMPLE_EVIDENCE,
                ProofRequirement.BASELINE_COMPARISON,
            }
        ),
        StrategyFamily.ARBITRAGE: frozenset(
            {
                ProofRequirement.LAWFUL_PROVENANCE,
                ProofRequirement.COMPLETE_LEG_COVERAGE,
                ProofRequirement.CONTEMPORANEOUS_QUOTES,
                ProofRequirement.COSTS_AND_FEES,
                ProofRequirement.SETTLEMENT_RULE_CONSISTENCY,
                ProofRequirement.FILL_PRICE_SENSITIVITY,
            }
        ),
        StrategyFamily.DUTCHING: frozenset(
            {
                ProofRequirement.LAWFUL_PROVENANCE,
                ProofRequirement.OUTCOME_COVERAGE,
                ProofRequirement.CONTEMPORANEOUS_QUOTES,
                ProofRequirement.COSTS_AND_FEES,
                ProofRequirement.SETTLEMENT_RULE_CONSISTENCY,
                ProofRequirement.RESIDUAL_EXPOSURE_VALIDATION,
            }
        ),
        StrategyFamily.HEDGE: frozenset(
            {
                ProofRequirement.LAWFUL_PROVENANCE,
                ProofRequirement.POSITION_IDENTITY,
                ProofRequirement.RESIDUAL_EXPOSURE_VALIDATION,
                ProofRequirement.COSTS_AND_FEES,
                ProofRequirement.SETTLEMENT_RULE_CONSISTENCY,
                ProofRequirement.FILL_PRICE_SENSITIVITY,
            }
        ),
        StrategyFamily.WAIT: frozenset(
            {
                ProofRequirement.LAWFUL_PROVENANCE,
                ProofRequirement.BASELINE_COMPARISON,
                ProofRequirement.EXPLICIT_NO_ACTION_SEMANTICS,
                ProofRequirement.THRESHOLD_RATIONALE,
                ProofRequirement.COUNTERFACTUAL_OPPORTUNITY_COST,
                ProofRequirement.NO_PHANTOM_EXECUTION,
            }
        ),
    }
)

# Public immutable view.  Values are frozensets, so neither the map nor its values
# can be mutated by callers.
REQUIREMENTS_BY_FAMILY: Mapping[StrategyFamily, frozenset[ProofRequirement]] = (
    _REQUIREMENTS_BY_FAMILY
)

# Claims explicitly outside this module's authority.  Keeping this machine-readable
# makes boundary tests possible and prevents a requirements artifact from being
# mistaken for outcome evidence.
NON_CLAIMS = frozenset(
    {
        "profitability",
        "promotion_eligibility",
        "production_readiness",
        "execution_completion",
        "provider_write_success",
        "real_money_execution",
        "scientific_truth_completion",
    }
)


def _normalize_family(value: StrategyFamily | str) -> StrategyFamily:
    if isinstance(value, StrategyFamily):
        return value
    if type(value) is not str:
        raise StrategyProofError("strategy family must be StrategyFamily or canonical string")
    try:
        return StrategyFamily(value)
    except ValueError as exc:
        raise StrategyProofError(f"unknown strategy family: {value!r}") from exc


def strategy_proof_contract(family: StrategyFamily | str) -> StrategyProofContract:
    """Return the immutable proof-requirements contract for one family."""

    normalized = _normalize_family(family)
    return StrategyProofContract(
        families=(normalized,),
        requirements=REQUIREMENTS_BY_FAMILY[normalized],
    )


def hybrid_strategy_proof_contract(
    families: Iterable[StrategyFamily | str],
) -> StrategyProofContract:
    """Compose a deterministic hybrid contract as the exact parent requirement union.

    Input order and duplicates do not affect the result.  The returned ``families``
    tuple follows ``StrategyFamily`` declaration order, and an empty iterable is
    rejected instead of producing a vacuous proof contract.
    """

    try:
        normalized = {_normalize_family(family) for family in families}
    except TypeError as exc:
        raise StrategyProofError("families must be an iterable of strategy families") from exc
    if not normalized:
        raise StrategyProofError("hybrid proof contract requires at least one family")

    canonical_families = tuple(
        family for family in StrategyFamily if family in normalized
    )
    requirements = frozenset(
        requirement
        for family in canonical_families
        for requirement in REQUIREMENTS_BY_FAMILY[family]
    )
    return StrategyProofContract(
        families=canonical_families,
        requirements=requirements,
    )
