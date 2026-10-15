"""Plan 2 / Section 3: nested empirical dependency evidence cannot be mutated into authority.

All effects are PROPOSED_PAPER_ONLY; no provider receipt, fill, or real balance.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.paper import PaperBook
from autosport.portfolio_delta import PortfolioDelta
from autosport.portfolio_plan import (
    EvidenceTruth,
    PortfolioAction,
    PortfolioDependencyEvidence,
    PortfolioDependencyGraph,
    PortfolioPlan,
)
from autosport.risk import PaperRiskPolicy


def _canonical_proposal():
    book = PaperBook(Decimal("100.00"))
    policy = PaperRiskPolicy(
        economic_goal=EconomicGoalContract(
            goal_id="dependency-owner",
            revision=1,
            bankroll_id="paper-dependency-bank",
            currency="EUR",
        ),
    )
    source_sha = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    assert source_sha is not None
    graph = PortfolioDependencyGraph(
        portfolio_sha256=source_sha,
        intent_sha256s=("a" * 64,),
        candidate_sha256s=("b" * 64,),
    )
    evidence = PortfolioDependencyEvidence(
        evidence_id="empirical-paper-evidence",
        portfolio_sha256=source_sha,
        intent_sha256s=graph.intent_sha256s,
        candidate_sha256s=graph.candidate_sha256s,
        population_id="offline-replay-sample",
        method="deterministic-causal-observation",
        sample_size=30,
        causal_cutoff="2026-10-08T11:00:00+00:00",
        as_of="2026-10-08T11:30:00+00:00",
        valid_until="2026-10-08T13:00:00+00:00",
        reproducibility_sha256="c" * 64,
        pairwise_dependency_upper_bounds=(),
        uncertainty_fraction=Decimal("0.1"),
        fee_fraction=Decimal("0.02"),
        partial_fill_stress_fraction=Decimal("0.03"),
    )
    plan = PortfolioPlan(
        decision_ts="2026-10-08T12:00:00+00:00",
        action=PortfolioAction.STAKE_VECTOR,
        stakes=(Decimal("4.25"),),
        intent_ids=("paper-intent",),
        intent_sha256s=graph.intent_sha256s,
        opportunity_classes=("live_price_movement",),
        portfolio_sha256=source_sha,
        dependency_graph=graph,
        terminal_economics=None,
        economic_goal_contract_sha256=provenance_for(policy.economic_goal).contract_sha256,
        risk_policy_sha256=policy.provenance_sha256,
        portfolio_truth=EvidenceTruth.EXACT,
        reason="read-only PAPER proposal",
        dependency_evidence=evidence,
        robust_proposal=None,
    )
    return book, policy, plan, evidence


@pytest.mark.parametrize(
    ("field", "corrupt"),
    (
        ("uncertainty_fraction", Decimal("-0.01")),
        ("fee_fraction", Decimal("1.01")),
        ("partial_fill_stress_fraction", Decimal("NaN")),
        ("sample_size", True),
        ("valid_until", "2026-10-08T10:00:00+00:00"),
        ("pairwise_dependency_upper_bounds", ("not-a-pair",)),
    ),
)
def test_postconstruction_nested_evidence_mutation_fails_closed(field, corrupt):
    book, policy, plan, evidence = _canonical_proposal()
    assert PortfolioDelta.derive(book, plan, policy).to_dict()["effect_state"] == "PROPOSED_PAPER_ONLY"
    object.__setattr__(evidence, field, corrupt)
    with pytest.raises((ValueError, TypeError)):
        PortfolioDelta.derive(book, plan, policy)
    assert book.balance == Decimal("100.00")
    assert book.committed_stake == Decimal("0")
    assert not book.tickets


def test_hostile_decimal_subclass_is_rejected_before_virtual_numeric_call():
    class HostileDecimal(Decimal):
        def is_finite(self):
            raise AssertionError("hostile Decimal method must not run")

    book, policy, plan, evidence = _canonical_proposal()
    object.__setattr__(evidence, "fee_fraction", HostileDecimal("0.01"))
    with pytest.raises(ValueError, match="exact Decimals"):
        PortfolioDelta.derive(book, plan, policy)
    assert book.balance == Decimal("100.00") and not book.tickets


def test_valid_dependency_evidence_survives_unicode_restart_without_debit(tmp_path):
    book, policy, plan, _ = _canonical_proposal()
    payload = PortfolioDelta.derive(book, plan, policy).to_dict()
    path = tmp_path / "Паперовий банк доказів з пробілами.json"
    book.save(path)
    recovered = PaperBook.load(path)
    delta = PortfolioDelta.readback(payload, book=recovered, plan=plan, risk_policy=policy)
    assert delta.proposed_stake_total == Decimal("4.25")
    assert delta.hypothetical_cash_after == Decimal("95.75")
    assert delta.currency == "EUR"
    assert recovered.balance == Decimal("100.00")
    assert not recovered.tickets
