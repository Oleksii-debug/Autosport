from dataclasses import FrozenInstanceError

import pytest

from autosport.strategy_proof_contract import (
    ProofRequirement,
    StrategyFamily,
    StrategyProofContract,
    StrategyProofContractError,
    StrategyProofEvaluation,
    evaluate_strategy_proofs,
    proof_contract_for,
)


def _required(family: StrategyFamily) -> frozenset[ProofRequirement]:
    return frozenset(proof_contract_for(family).required_proofs)


def test_every_strategy_family_has_a_deterministic_contract() -> None:
    contracts = {family: proof_contract_for(family) for family in StrategyFamily}

    assert set(contracts) == set(StrategyFamily)
    for family, contract in contracts.items():
        assert contract.family is family
        assert contract.required_proofs == tuple(
            sorted(contract.required_proofs, key=lambda proof: proof.value)
        )
        assert len(contract.required_proofs) == len(set(contract.required_proofs))


def test_forecast_is_required_only_for_predictive_and_hybrid_families() -> None:
    forecast_families = {
        family
        for family in StrategyFamily
        if proof_contract_for(family).forecast_required
    }

    assert forecast_families == {
        StrategyFamily.PREDICTIVE_EDGE,
        StrategyFamily.HYBRID,
    }


@pytest.mark.parametrize("family", [StrategyFamily.ARBITRAGE, StrategyFamily.DUTCHING])
def test_outcome_independent_families_require_complete_terminal_state_proof(
    family: StrategyFamily,
) -> None:
    required = _required(family)

    assert ProofRequirement.COMPLETE_TERMINAL_OUTCOME_SPACE in required
    assert ProofRequirement.MINIMUM_TERMINAL_NET_PNL in required
    assert ProofRequirement.SETTLEMENT_SEMANTICS in required
    assert ProofRequirement.APPLICABLE_COSTS in required
    assert ProofRequirement.PROVIDER_LIMITS in required
    assert ProofRequirement.STAKE_GRANULARITY in required


def test_hedge_requires_existing_exposure_and_risk_improvement_not_forecast() -> None:
    required = _required(StrategyFamily.HEDGE_REBALANCE)

    assert ProofRequirement.EXISTING_EXPOSURE in required
    assert ProofRequirement.RESIDUAL_RISK_IMPROVEMENT in required
    assert ProofRequirement.FORECAST_PROBABILITY not in required


def test_live_price_movement_requires_live_causality_not_forecast() -> None:
    required = _required(StrategyFamily.LIVE_PRICE_MOVEMENT)

    assert ProofRequirement.LIVE_STATE_CONTINUITY in required
    assert ProofRequirement.PRICE_MOVEMENT_CAUSALITY in required
    assert ProofRequirement.FORECAST_PROBABILITY not in required


def test_missing_family_specific_proof_fails_closed_to_wait() -> None:
    required = _required(StrategyFamily.ARBITRAGE)
    present = required - frozenset(
        {ProofRequirement.COMPLETE_TERMINAL_OUTCOME_SPACE}
    )

    evaluation = evaluate_strategy_proofs(StrategyFamily.ARBITRAGE, present)

    assert evaluation.proof_contract_satisfied is False
    assert evaluation.positive_action_candidate is False
    assert evaluation.fallback_family is StrategyFamily.WAIT
    assert evaluation.missing_proofs == (
        ProofRequirement.COMPLETE_TERMINAL_OUTCOME_SPACE,
    )
    assert evaluation.execution_authorized is False


def test_relabelling_predictive_evidence_cannot_satisfy_arbitrage_contract() -> None:
    predictive = _required(StrategyFamily.PREDICTIVE_EDGE)

    evaluation = evaluate_strategy_proofs(StrategyFamily.ARBITRAGE, predictive)

    assert evaluation.proof_contract_satisfied is False
    assert ProofRequirement.COMPLETE_TERMINAL_OUTCOME_SPACE in evaluation.missing_proofs
    assert ProofRequirement.MINIMUM_TERMINAL_NET_PNL in evaluation.missing_proofs
    assert evaluation.fallback_family is StrategyFamily.WAIT


def test_complete_arbitrage_proof_only_becomes_positive_action_candidate() -> None:
    required = _required(StrategyFamily.ARBITRAGE)

    evaluation = evaluate_strategy_proofs(StrategyFamily.ARBITRAGE, required)

    assert evaluation.proof_contract_satisfied is True
    assert evaluation.positive_action_candidate is True
    assert evaluation.fallback_family is StrategyFamily.ARBITRAGE
    assert evaluation.missing_proofs == ()
    assert evaluation.execution_authorized is False


def test_wait_never_becomes_positive_action_or_execution_authority() -> None:
    required = _required(StrategyFamily.WAIT)

    evaluation = evaluate_strategy_proofs(StrategyFamily.WAIT, required)

    assert evaluation.proof_contract_satisfied is True
    assert evaluation.positive_action_candidate is False
    assert evaluation.fallback_family is StrategyFamily.WAIT
    assert evaluation.execution_authorized is False


def test_wait_with_missing_shared_context_stays_wait() -> None:
    evaluation = evaluate_strategy_proofs(StrategyFamily.WAIT, frozenset())

    assert evaluation.proof_contract_satisfied is False
    assert evaluation.positive_action_candidate is False
    assert evaluation.fallback_family is StrategyFamily.WAIT
    assert evaluation.execution_authorized is False


def test_extra_wrong_family_proofs_do_not_replace_required_proofs() -> None:
    required = _required(StrategyFamily.PREDICTIVE_EDGE)
    present = (
        required
        - frozenset({ProofRequirement.FORECAST_PROBABILITY})
        | frozenset(
            {
                ProofRequirement.COMPLETE_TERMINAL_OUTCOME_SPACE,
                ProofRequirement.MINIMUM_TERMINAL_NET_PNL,
            }
        )
    )

    evaluation = evaluate_strategy_proofs(StrategyFamily.PREDICTIVE_EDGE, present)

    assert evaluation.missing_proofs == (ProofRequirement.FORECAST_PROBABILITY,)
    assert evaluation.positive_action_candidate is False
    assert evaluation.fallback_family is StrategyFamily.WAIT


def test_contracts_and_evaluations_are_frozen() -> None:
    contract = proof_contract_for(StrategyFamily.PREDICTIVE_EDGE)
    evaluation = evaluate_strategy_proofs(
        StrategyFamily.PREDICTIVE_EDGE,
        _required(StrategyFamily.PREDICTIVE_EDGE),
    )

    with pytest.raises(FrozenInstanceError):
        contract.family = StrategyFamily.WAIT  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        evaluation.execution_authorized = True  # type: ignore[misc]


def test_noncanonical_inputs_are_rejected() -> None:
    with pytest.raises(StrategyProofContractError, match="StrategyFamily"):
        proof_contract_for("arbitrage")  # type: ignore[arg-type]

    with pytest.raises(StrategyProofContractError, match="frozenset"):
        evaluate_strategy_proofs(  # type: ignore[arg-type]
            StrategyFamily.ARBITRAGE,
            set(),
        )

    with pytest.raises(StrategyProofContractError, match="ProofRequirement"):
        evaluate_strategy_proofs(
            StrategyFamily.ARBITRAGE,
            frozenset({"complete_terminal_outcome_space"}),  # type: ignore[arg-type]
        )


def test_callers_cannot_mint_noncanonical_contract_or_execution_authority() -> None:
    canonical = proof_contract_for(StrategyFamily.ARBITRAGE)
    shortened = tuple(
        proof
        for proof in canonical.required_proofs
        if proof is not ProofRequirement.COMPLETE_TERMINAL_OUTCOME_SPACE
    )

    with pytest.raises(StrategyProofContractError, match="canonical"):
        StrategyProofContract(
            family=StrategyFamily.ARBITRAGE,
            required_proofs=shortened,
        )

    with pytest.raises(StrategyProofContractError, match="cannot authorize"):
        StrategyProofEvaluation(
            family=StrategyFamily.ARBITRAGE,
            required_proofs=canonical.required_proofs,
            present_proofs=canonical.required_proofs,
            missing_proofs=(),
            proof_contract_satisfied=True,
            positive_action_candidate=True,
            fallback_family=StrategyFamily.ARBITRAGE,
            execution_authorized=True,
        )
