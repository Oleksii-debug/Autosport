from dataclasses import FrozenInstanceError

import pytest

from autosport.opportunity import OpportunityDecision, StrategyClass
from autosport.strategy_proof_contract import (
    ProofRequirement,
    StrategyProofContract,
    StrategyProofContractError,
    StrategyProofEvaluation,
    evaluate_strategy_proofs,
    proof_contract_for,
)


def _claims_probability_edge(strategy_class: StrategyClass) -> bool:
    return strategy_class is StrategyClass.PREDICTIVE_EDGE


def _required(
    strategy_class: StrategyClass,
    *,
    claims_probability_edge: bool | None = None,
) -> frozenset[ProofRequirement]:
    claim = (
        _claims_probability_edge(strategy_class)
        if claims_probability_edge is None
        else claims_probability_edge
    )
    return frozenset(
        proof_contract_for(
            strategy_class,
            claims_probability_edge=claim,
        ).required_proofs
    )


def test_every_canonical_strategy_class_has_a_deterministic_contract() -> None:
    contracts = {
        strategy_class: proof_contract_for(
            strategy_class,
            claims_probability_edge=_claims_probability_edge(strategy_class),
        )
        for strategy_class in StrategyClass
    }

    assert set(contracts) == set(StrategyClass)
    assert "wait" not in {strategy_class.value for strategy_class in StrategyClass}
    assert OpportunityDecision.WAIT.value == "wait"
    for strategy_class, contract in contracts.items():
        assert contract.strategy_class is strategy_class
        assert contract.required_proofs == tuple(
            sorted(contract.required_proofs, key=lambda proof: proof.value)
        )
        assert len(contract.required_proofs) == len(set(contract.required_proofs))


def test_predictive_edge_requires_probability_claim_and_forecast_proof() -> None:
    contract = proof_contract_for(
        StrategyClass.PREDICTIVE_EDGE,
        claims_probability_edge=True,
    )

    assert contract.forecast_required is True
    assert ProofRequirement.FORECAST_PROBABILITY in contract.required_proofs

    with pytest.raises(StrategyProofContractError, match="must claim probability"):
        proof_contract_for(
            StrategyClass.PREDICTIVE_EDGE,
            claims_probability_edge=False,
        )


@pytest.mark.parametrize(
    "strategy_class",
    [
        StrategyClass.LIVE_PRICE_MOVEMENT,
        StrategyClass.ARBITRAGE,
        StrategyClass.DUTCHING,
        StrategyClass.HEDGE_REBALANCE,
    ],
)
def test_nonpredictive_classes_cannot_be_relabelled_as_probability_edge(
    strategy_class: StrategyClass,
) -> None:
    with pytest.raises(StrategyProofContractError, match="only PREDICTIVE_EDGE or HYBRID"):
        proof_contract_for(strategy_class, claims_probability_edge=True)


@pytest.mark.parametrize("strategy_class", [StrategyClass.ARBITRAGE, StrategyClass.DUTCHING])
def test_outcome_independent_classes_require_complete_terminal_state_proof(
    strategy_class: StrategyClass,
) -> None:
    required = _required(strategy_class)

    assert ProofRequirement.COMPLETE_TERMINAL_OUTCOME_SPACE in required
    assert ProofRequirement.MINIMUM_TERMINAL_NET_PNL in required
    assert ProofRequirement.SETTLEMENT_SEMANTICS in required
    assert ProofRequirement.APPLICABLE_COSTS in required
    assert ProofRequirement.PROVIDER_LIMITS in required
    assert ProofRequirement.STAKE_GRANULARITY in required
    assert ProofRequirement.FORECAST_PROBABILITY not in required


def test_hedge_requires_existing_exposure_and_risk_improvement_not_forecast() -> None:
    required = _required(StrategyClass.HEDGE_REBALANCE)

    assert ProofRequirement.EXISTING_EXPOSURE in required
    assert ProofRequirement.RESIDUAL_RISK_IMPROVEMENT in required
    assert ProofRequirement.FORECAST_PROBABILITY not in required


def test_live_price_movement_requires_live_causality_not_forecast() -> None:
    required = _required(StrategyClass.LIVE_PRICE_MOVEMENT)

    assert ProofRequirement.LIVE_STATE_CONTINUITY in required
    assert ProofRequirement.PRICE_MOVEMENT_CAUSALITY in required
    assert ProofRequirement.FORECAST_PROBABILITY not in required


def test_hybrid_forecast_requirement_tracks_canonical_probability_claim() -> None:
    nonpredictive = _required(
        StrategyClass.HYBRID,
        claims_probability_edge=False,
    )
    predictive = _required(
        StrategyClass.HYBRID,
        claims_probability_edge=True,
    )

    assert ProofRequirement.HYBRID_COMPONENT_EVIDENCE in nonpredictive
    assert ProofRequirement.FORECAST_PROBABILITY not in nonpredictive
    assert ProofRequirement.HYBRID_COMPONENT_EVIDENCE in predictive
    assert ProofRequirement.FORECAST_PROBABILITY in predictive
    assert ProofRequirement.FORECAST_CAUSAL_PROVENANCE in predictive


def test_missing_class_specific_proof_fails_closed_to_wait_decision() -> None:
    required = _required(StrategyClass.ARBITRAGE)
    present = required - frozenset(
        {ProofRequirement.COMPLETE_TERMINAL_OUTCOME_SPACE}
    )

    evaluation = evaluate_strategy_proofs(
        StrategyClass.ARBITRAGE,
        present,
        claims_probability_edge=False,
    )

    assert evaluation.proof_contract_satisfied is False
    assert evaluation.positive_action_candidate is False
    assert evaluation.proof_gate_decision is OpportunityDecision.WAIT
    assert evaluation.missing_proofs == (
        ProofRequirement.COMPLETE_TERMINAL_OUTCOME_SPACE,
    )
    assert evaluation.execution_authorized is False


def test_relabelling_predictive_evidence_cannot_satisfy_arbitrage_contract() -> None:
    predictive = _required(
        StrategyClass.PREDICTIVE_EDGE,
        claims_probability_edge=True,
    )

    evaluation = evaluate_strategy_proofs(
        StrategyClass.ARBITRAGE,
        predictive,
        claims_probability_edge=False,
    )

    assert evaluation.proof_contract_satisfied is False
    assert ProofRequirement.COMPLETE_TERMINAL_OUTCOME_SPACE in evaluation.missing_proofs
    assert ProofRequirement.MINIMUM_TERMINAL_NET_PNL in evaluation.missing_proofs
    assert evaluation.proof_gate_decision is OpportunityDecision.WAIT


def test_complete_proof_is_only_a_proof_gate_candidate_not_execution_authority() -> None:
    required = _required(StrategyClass.ARBITRAGE)

    evaluation = evaluate_strategy_proofs(
        StrategyClass.ARBITRAGE,
        required,
        claims_probability_edge=False,
    )

    assert evaluation.proof_contract_satisfied is True
    assert evaluation.positive_action_candidate is True
    assert evaluation.proof_gate_decision is OpportunityDecision.ACTIONABLE
    assert evaluation.missing_proofs == ()
    assert evaluation.execution_authorized is False


def test_extra_wrong_class_proofs_do_not_replace_required_proofs() -> None:
    required = _required(
        StrategyClass.PREDICTIVE_EDGE,
        claims_probability_edge=True,
    )
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

    evaluation = evaluate_strategy_proofs(
        StrategyClass.PREDICTIVE_EDGE,
        present,
        claims_probability_edge=True,
    )

    assert evaluation.missing_proofs == (ProofRequirement.FORECAST_PROBABILITY,)
    assert evaluation.positive_action_candidate is False
    assert evaluation.proof_gate_decision is OpportunityDecision.WAIT


def test_contracts_and_evaluations_are_frozen() -> None:
    contract = proof_contract_for(
        StrategyClass.PREDICTIVE_EDGE,
        claims_probability_edge=True,
    )
    evaluation = evaluate_strategy_proofs(
        StrategyClass.PREDICTIVE_EDGE,
        _required(StrategyClass.PREDICTIVE_EDGE),
        claims_probability_edge=True,
    )

    with pytest.raises(FrozenInstanceError):
        contract.strategy_class = StrategyClass.HYBRID  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        evaluation.execution_authorized = True  # type: ignore[misc]


def test_noncanonical_inputs_are_rejected() -> None:
    with pytest.raises(StrategyProofContractError, match="StrategyClass"):
        proof_contract_for(  # type: ignore[arg-type]
            "arbitrage",
            claims_probability_edge=False,
        )

    with pytest.raises(StrategyProofContractError, match="bool"):
        proof_contract_for(  # type: ignore[arg-type]
            StrategyClass.ARBITRAGE,
            claims_probability_edge=0,
        )

    with pytest.raises(StrategyProofContractError, match="frozenset"):
        evaluate_strategy_proofs(  # type: ignore[arg-type]
            StrategyClass.ARBITRAGE,
            set(),
            claims_probability_edge=False,
        )

    with pytest.raises(StrategyProofContractError, match="ProofRequirement"):
        evaluate_strategy_proofs(
            StrategyClass.ARBITRAGE,
            frozenset({"complete_terminal_outcome_space"}),  # type: ignore[arg-type]
            claims_probability_edge=False,
        )


def test_callers_cannot_mint_noncanonical_contract_or_execution_authority() -> None:
    canonical = proof_contract_for(
        StrategyClass.ARBITRAGE,
        claims_probability_edge=False,
    )
    shortened = tuple(
        proof
        for proof in canonical.required_proofs
        if proof is not ProofRequirement.COMPLETE_TERMINAL_OUTCOME_SPACE
    )

    with pytest.raises(StrategyProofContractError, match="canonical"):
        StrategyProofContract(
            strategy_class=StrategyClass.ARBITRAGE,
            claims_probability_edge=False,
            required_proofs=shortened,
        )

    with pytest.raises(StrategyProofContractError, match="cannot authorize"):
        StrategyProofEvaluation(
            strategy_class=StrategyClass.ARBITRAGE,
            claims_probability_edge=False,
            required_proofs=canonical.required_proofs,
            present_proofs=canonical.required_proofs,
            missing_proofs=(),
            proof_contract_satisfied=True,
            positive_action_candidate=True,
            proof_gate_decision=OpportunityDecision.ACTIONABLE,
            execution_authorized=True,
        )
