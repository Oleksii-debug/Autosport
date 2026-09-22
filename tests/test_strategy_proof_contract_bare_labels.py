"""Regression for WP-S08: requirement labels are not resolved proof authority.

This dependent test intentionally targets PR #787.  A caller-controlled set of
ProofRequirement enum values may describe which proof classes are present in an
input envelope, but those labels do not themselves validate the underlying
forecast, quote, portfolio, risk, settlement, provider, or execution evidence.

The requirements-only repair is free to remove the decision-like result fields.
If they remain, they must fail closed instead of minting ACTIONABLE truth.
"""

from autosport.opportunity import OpportunityDecision, StrategyClass
from autosport.strategy_proof_contract import (
    evaluate_strategy_proofs,
    proof_contract_for,
)


def _assert_bare_labels_do_not_mint_actionability(
    strategy_class: StrategyClass,
    *,
    claims_probability_edge: bool,
) -> None:
    contract = proof_contract_for(
        strategy_class,
        claims_probability_edge=claims_probability_edge,
    )

    # This is deliberately the strongest caller-controlled label set: every
    # requirement name is supplied, but no canonical evidence authority is
    # resolved here.
    result = evaluate_strategy_proofs(
        strategy_class,
        frozenset(contract.required_proofs),
        claims_probability_edge=claims_probability_edge,
    )

    assert tuple(result.required_proofs) == tuple(contract.required_proofs)
    assert tuple(result.missing_proofs) == ()

    # A requirements-only taxonomy may remove these fields entirely.  If it
    # retains them for compatibility, bare enum labels must never make them
    # positive.
    assert getattr(result, "positive_action_candidate", False) is False
    assert (
        getattr(result, "proof_gate_decision", OpportunityDecision.WAIT)
        is not OpportunityDecision.ACTIONABLE
    )


def test_all_arbitrage_requirement_labels_cannot_mint_actionable_truth() -> None:
    _assert_bare_labels_do_not_mint_actionability(
        StrategyClass.ARBITRAGE,
        claims_probability_edge=False,
    )


def test_all_predictive_requirement_labels_cannot_mint_actionable_truth() -> None:
    _assert_bare_labels_do_not_mint_actionability(
        StrategyClass.PREDICTIVE_EDGE,
        claims_probability_edge=True,
    )
