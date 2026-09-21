from dataclasses import FrozenInstanceError

import pytest

from autosport.strategy_family_proof import (
    NON_CLAIMS,
    REQUIREMENTS_BY_FAMILY,
    ProofRequirement,
    StrategyFamily,
    StrategyProofContract,
    StrategyProofError,
    hybrid_strategy_proof_contract,
    strategy_proof_contract,
)


def test_family_vocabulary_is_exact_and_ordered():
    assert [(item.name, item.value) for item in StrategyFamily] == [
        ("FORECAST", "forecast"),
        ("LIVE_MOVEMENT", "live_movement"),
        ("ARBITRAGE", "arbitrage"),
        ("DUTCHING", "dutching"),
        ("HEDGE", "hedge"),
        ("WAIT", "wait"),
    ]


def test_every_family_has_one_nonempty_immutable_contract():
    assert tuple(REQUIREMENTS_BY_FAMILY) == tuple(StrategyFamily)
    for family in StrategyFamily:
        contract = strategy_proof_contract(family)
        assert contract.families == (family,)
        assert contract.requirements is REQUIREMENTS_BY_FAMILY[family]
        assert isinstance(contract.requirements, frozenset)
        assert contract.requirements


def test_forecast_requires_causal_oos_baseline_and_calibration_proof():
    required = {
        ProofRequirement.CAUSAL_EVENT_TIME_ORDER,
        ProofRequirement.OUT_OF_SAMPLE_EVIDENCE,
        ProofRequirement.BASELINE_COMPARISON,
        ProofRequirement.UNCERTAINTY_CALIBRATION,
    }
    assert required <= strategy_proof_contract("forecast").requirements


def test_live_movement_requires_identity_freshness_and_causal_order():
    required = {
        ProofRequirement.MARKET_IDENTITY,
        ProofRequirement.QUOTE_FRESHNESS,
        ProofRequirement.CAUSAL_EVENT_TIME_ORDER,
    }
    assert required <= strategy_proof_contract("live_movement").requirements


def test_arbitrage_requires_complete_contemporaneous_costed_legs():
    required = {
        ProofRequirement.COMPLETE_LEG_COVERAGE,
        ProofRequirement.CONTEMPORANEOUS_QUOTES,
        ProofRequirement.COSTS_AND_FEES,
        ProofRequirement.SETTLEMENT_RULE_CONSISTENCY,
        ProofRequirement.FILL_PRICE_SENSITIVITY,
    }
    assert required <= strategy_proof_contract("arbitrage").requirements


def test_dutching_requires_outcome_coverage_and_residual_exposure_proof():
    required = {
        ProofRequirement.OUTCOME_COVERAGE,
        ProofRequirement.CONTEMPORANEOUS_QUOTES,
        ProofRequirement.RESIDUAL_EXPOSURE_VALIDATION,
    }
    assert required <= strategy_proof_contract("dutching").requirements


def test_hedge_requires_position_identity_and_residual_exposure_proof():
    required = {
        ProofRequirement.POSITION_IDENTITY,
        ProofRequirement.RESIDUAL_EXPOSURE_VALIDATION,
        ProofRequirement.FILL_PRICE_SENSITIVITY,
    }
    assert required <= strategy_proof_contract("hedge").requirements


def test_wait_requires_explicit_abstention_and_forbids_phantom_execution():
    required = {
        ProofRequirement.EXPLICIT_NO_ACTION_SEMANTICS,
        ProofRequirement.THRESHOLD_RATIONALE,
        ProofRequirement.COUNTERFACTUAL_OPPORTUNITY_COST,
        ProofRequirement.NO_PHANTOM_EXECUTION,
    }
    assert required <= strategy_proof_contract("wait").requirements


def test_hybrid_is_exact_parent_union_and_never_weaker():
    parents = [StrategyFamily.FORECAST, StrategyFamily.ARBITRAGE, StrategyFamily.WAIT]
    hybrid = hybrid_strategy_proof_contract(parents)
    expected = frozenset().union(*(REQUIREMENTS_BY_FAMILY[item] for item in parents))
    assert hybrid.requirements == expected
    for family in parents:
        assert REQUIREMENTS_BY_FAMILY[family] <= hybrid.requirements


def test_hybrid_order_is_canonical_and_duplicates_are_deduplicated():
    contract = hybrid_strategy_proof_contract(
        ["wait", StrategyFamily.ARBITRAGE, "forecast", "wait", "arbitrage"]
    )
    assert contract.families == (
        StrategyFamily.FORECAST,
        StrategyFamily.ARBITRAGE,
        StrategyFamily.WAIT,
    )


def test_empty_or_unknown_hybrid_is_rejected_fail_closed():
    with pytest.raises(StrategyProofError, match="at least one family"):
        hybrid_strategy_proof_contract([])
    with pytest.raises(StrategyProofError, match="unknown strategy family"):
        hybrid_strategy_proof_contract(["martingale"])
    with pytest.raises(StrategyProofError, match="iterable"):
        hybrid_strategy_proof_contract(None)  # type: ignore[arg-type]


def test_contract_and_requirement_registry_are_immutable():
    contract = strategy_proof_contract(StrategyFamily.FORECAST)
    with pytest.raises(FrozenInstanceError):
        contract.families = (StrategyFamily.WAIT,)  # type: ignore[misc]
    with pytest.raises(TypeError):
        REQUIREMENTS_BY_FAMILY[StrategyFamily.FORECAST] = frozenset()  # type: ignore[index]
    with pytest.raises(AttributeError):
        contract.requirements.add(ProofRequirement.NO_PHANTOM_EXECUTION)  # type: ignore[attr-defined]


def test_contract_rejects_noncanonical_manual_construction():
    with pytest.raises(StrategyProofError, match="canonical StrategyFamily order"):
        StrategyProofContract(
            families=(StrategyFamily.WAIT, StrategyFamily.FORECAST),
            requirements=frozenset({ProofRequirement.LAWFUL_PROVENANCE}),
        )


def test_nonclaims_make_requirements_only_authority_explicit_and_immutable():
    assert NON_CLAIMS == frozenset(
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
    with pytest.raises(AttributeError):
        NON_CLAIMS.add("anything")  # type: ignore[attr-defined]
