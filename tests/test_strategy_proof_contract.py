from dataclasses import FrozenInstanceError

import pytest

import autosport.strategy_proof_contract as strategy_proof_module

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

    assert evaluation.required_labels_present is False
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

    assert evaluation.required_labels_present is False
    assert ProofRequirement.COMPLETE_TERMINAL_OUTCOME_SPACE in evaluation.missing_proofs
    assert ProofRequirement.MINIMUM_TERMINAL_NET_PNL in evaluation.missing_proofs


def test_complete_requirement_labels_remain_requirements_only() -> None:
    required = _required(StrategyClass.ARBITRAGE)

    evaluation = evaluate_strategy_proofs(
        StrategyClass.ARBITRAGE,
        required,
        claims_probability_edge=False,
    )

    assert evaluation.required_labels_present is True
    assert evaluation.missing_proofs == ()
    assert evaluation.execution_authorized is False
    assert not hasattr(evaluation, "positive_action_candidate")
    assert not hasattr(evaluation, "proof_gate_decision")


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
            required_labels_present=True,
            execution_authorized=True,
        )


@pytest.mark.parametrize(
    ("strategy_class", "claims_probability_edge"),
    [
        (StrategyClass.ARBITRAGE, False),
        (StrategyClass.PREDICTIVE_EDGE, True),
    ],
)
def test_all_required_bare_labels_cannot_mint_actionable_truth(
    strategy_class: StrategyClass,
    claims_probability_edge: bool,
) -> None:
    contract = proof_contract_for(
        strategy_class,
        claims_probability_edge=claims_probability_edge,
    )

    evaluation = evaluate_strategy_proofs(
        strategy_class,
        frozenset(contract.required_proofs),
        claims_probability_edge=claims_probability_edge,
    )

    assert evaluation.required_proofs == contract.required_proofs
    assert evaluation.missing_proofs == ()
    assert evaluation.required_labels_present is True
    assert evaluation.execution_authorized is False
    assert not hasattr(evaluation, "positive_action_candidate")
    assert not hasattr(evaluation, "proof_gate_decision")


def test_execution_authorized_requires_exact_bool_contract_value() -> None:
    contract = proof_contract_for(
        StrategyClass.ARBITRAGE,
        claims_probability_edge=False,
    )

    with pytest.raises(
        StrategyProofContractError,
        match="execution_authorized must be a bool",
    ):
        StrategyProofEvaluation(
            strategy_class=StrategyClass.ARBITRAGE,
            claims_probability_edge=False,
            required_proofs=contract.required_proofs,
            present_proofs=contract.required_proofs,
            missing_proofs=(),
            required_labels_present=True,
            execution_authorized=0,  # type: ignore[arg-type]
        )


class _HostileProofSet(frozenset):
    def __iter__(self):
        raise AssertionError("hostile frozenset iteration executed")


class _HostileProofTuple(tuple):
    def __iter__(self):
        raise AssertionError("hostile tuple iteration executed")


def test_hostile_proof_container_subclasses_fail_before_virtual_dispatch() -> None:
    hostile_set = _HostileProofSet(
        {ProofRequirement.CAUSAL_OPPORTUNITY_EVIDENCE}
    )

    with pytest.raises(StrategyProofContractError, match="frozenset"):
        evaluate_strategy_proofs(
            StrategyClass.ARBITRAGE,
            hostile_set,  # type: ignore[arg-type]
            claims_probability_edge=False,
        )

    contract = proof_contract_for(
        StrategyClass.ARBITRAGE,
        claims_probability_edge=False,
    )
    hostile_required = _HostileProofTuple(contract.required_proofs)
    with pytest.raises(StrategyProofContractError, match="required_proofs must be a tuple"):
        StrategyProofContract(
            strategy_class=StrategyClass.ARBITRAGE,
            claims_probability_edge=False,
            required_proofs=hostile_required,  # type: ignore[arg-type]
        )

    evaluation = evaluate_strategy_proofs(
        StrategyClass.ARBITRAGE,
        frozenset(contract.required_proofs),
        claims_probability_edge=False,
    )
    hostile_present = _HostileProofTuple(evaluation.present_proofs)
    with pytest.raises(StrategyProofContractError, match="present_proofs must be a tuple"):
        StrategyProofEvaluation(
            strategy_class=evaluation.strategy_class,
            claims_probability_edge=evaluation.claims_probability_edge,
            required_proofs=evaluation.required_proofs,
            present_proofs=hostile_present,  # type: ignore[arg-type]
            missing_proofs=evaluation.missing_proofs,
            required_labels_present=evaluation.required_labels_present,
            execution_authorized=False,
        )




class _ForgedStrategy:
    pass


class _ForgedProof:
    def __init__(self, value: str) -> None:
        self.value = value

    def __hash__(self) -> int:
        return hash(self.value)

    def __eq__(self, other: object) -> bool:
        return getattr(other, "value", None) == self.value


def test_coordinated_module_global_rebind_cannot_replace_taxonomy_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_required = _required(StrategyClass.ARBITRAGE)

    fake_arbitrage = _ForgedStrategy()
    _ForgedStrategy.PREDICTIVE_EDGE = _ForgedStrategy()
    _ForgedStrategy.HYBRID = _ForgedStrategy()
    fake_proof = _ForgedProof("caller-defined-proof")

    def poison(*_args, **_kwargs):
        raise AssertionError("rebound taxonomy authority executed")

    class _PoisonContract:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("rebound StrategyProofContract executed")

    class _PoisonEvaluation:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("rebound StrategyProofEvaluation executed")

    monkeypatch.setattr(strategy_proof_module, "StrategyClass", _ForgedStrategy)
    monkeypatch.setattr(strategy_proof_module, "ProofRequirement", _ForgedProof)
    monkeypatch.setattr(
        strategy_proof_module,
        "_CLASS_REQUIREMENTS",
        {fake_arbitrage: frozenset({fake_proof})},
    )
    monkeypatch.setattr(strategy_proof_module, "_PREDICTIVE", frozenset())
    monkeypatch.setattr(strategy_proof_module, "_strategy_class", poison)
    monkeypatch.setattr(strategy_proof_module, "_required_for", poison)
    monkeypatch.setattr(strategy_proof_module, "_proofs", poison)
    monkeypatch.setattr(
        strategy_proof_module,
        "StrategyProofContract",
        _PoisonContract,
    )
    monkeypatch.setattr(
        strategy_proof_module,
        "StrategyProofEvaluation",
        _PoisonEvaluation,
    )
    for name in ("tuple", "frozenset", "sorted", "type", "any", "bool"):
        monkeypatch.setattr(
            strategy_proof_module,
            name,
            poison,
            raising=False,
        )

    contract = proof_contract_for(
        StrategyClass.ARBITRAGE,
        claims_probability_edge=False,
    )
    assert type(contract) is StrategyProofContract
    assert frozenset(contract.required_proofs) == canonical_required

    evaluation = evaluate_strategy_proofs(
        StrategyClass.ARBITRAGE,
        canonical_required,
        claims_probability_edge=False,
    )
    assert type(evaluation) is StrategyProofEvaluation
    assert evaluation.required_labels_present is True
    assert evaluation.execution_authorized is False

    with pytest.raises(
        StrategyProofContractError,
        match="strategy_class must be a StrategyClass",
    ):
        proof_contract_for(
            fake_arbitrage,  # type: ignore[arg-type]
            claims_probability_edge=False,
        )

    with pytest.raises(
        StrategyProofContractError,
        match="required_proofs must contain only ProofRequirement values",
    ):
        StrategyProofContract(
            strategy_class=StrategyClass.ARBITRAGE,
            claims_probability_edge=False,
            required_proofs=(fake_proof,),  # type: ignore[arg-type]
        )
