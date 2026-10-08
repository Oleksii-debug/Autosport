"""Exact PortfolioPlan numeric-ingress guard; PAPER proposal never authorizes a fill."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.paper import PaperBook
from autosport.portfolio_delta import PortfolioDelta
from autosport.portfolio_plan import (
    EvidenceTruth,
    PortfolioAction,
    PortfolioDependencyGraph,
    PortfolioPlan,
)
from autosport.risk import PaperRiskPolicy


class HostileDecimal(Decimal):
    """A nominal Decimal with caller-controlled virtual numeric methods."""

    def is_finite(self):
        raise AssertionError("virtual Decimal method invoked")

    def __str__(self):
        raise AssertionError("virtual Decimal serialization invoked")


def _valid_proposal():
    book = PaperBook(Decimal("100.00"))
    policy = PaperRiskPolicy(
        economic_goal=EconomicGoalContract(
            goal_id="plan2-stake-ingress",
            revision=1,
            bankroll_id="paper-only",
            currency="EUR",
        ),
    )
    portfolio = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    assert portfolio is not None
    plan = PortfolioPlan(
        decision_ts="2026-10-08T12:00:00+00:00",
        action=PortfolioAction.STAKE_VECTOR,
        stakes=(Decimal("2.00"),),
        intent_ids=("intent-one",),
        intent_sha256s=("a" * 64,),
        opportunity_classes=("live_price_movement",),
        portfolio_sha256=portfolio,
        dependency_graph=PortfolioDependencyGraph(
            portfolio_sha256=portfolio,
            intent_sha256s=("a" * 64,),
            candidate_sha256s=("b" * 64,),
        ),
        terminal_economics=None,
        economic_goal_contract_sha256=provenance_for(
            policy.economic_goal
        ).contract_sha256,
        risk_policy_sha256=policy.provenance_sha256,
        portfolio_truth=EvidenceTruth.EXACT,
        reason="proposal only",
    )
    return book, plan, policy


def test_plan_constructor_rejects_subclass_before_any_virtual_method():
    book, plan, policy = _valid_proposal()
    with pytest.raises(ValueError, match="stakes must contain"):
        replace(plan, stakes=(HostileDecimal("2.00"),))
    assert book.balance == Decimal("100.00") and not book.tickets
    assert PortfolioDelta.derive(book, plan, policy).proposed_stake_total == Decimal("2.00")


def test_mutated_stake_rejected_at_use_boundary_without_paper_effect():
    book, plan, policy = _valid_proposal()
    object.__setattr__(plan, "stakes", (HostileDecimal("2.00"),))
    with pytest.raises(ValueError, match="proposal stakes must be exact canonical Decimals"):
        PortfolioDelta.derive(book, plan, policy)
    assert book.balance == Decimal("100.00") and not book.tickets


def test_normal_decimal_roundtrip_and_wait_zero_are_stable():
    book, plan, policy = _valid_proposal()
    delta = PortfolioDelta.derive(book, plan, policy)
    assert PortfolioDelta.readback(
        delta.to_dict(), book=book, plan=plan, risk_policy=policy
    ) == delta
    zero = replace(plan, action=PortfolioAction.ZERO, stakes=(Decimal("0"),))
    wait = replace(plan, action=PortfolioAction.WAIT, stakes=(Decimal("0"),))
    for candidate in (zero, wait):
        projected = PortfolioDelta.derive(book, candidate, policy)
        assert projected.proposed_stake_total == Decimal("0")
        assert projected.hypothetical_cash_after == Decimal("100.00")
    assert book.balance == Decimal("100.00") and not book.tickets


@pytest.mark.parametrize("invalid_version", (4.0, Decimal("4")))
def test_portfolio_plan_rejects_noninteger_schema_version(invalid_version):
    """Schema identity is exact; float/Decimal 4 must not decode as integer 4."""
    book, plan, _ = _valid_proposal()
    serialized = plan.to_dict()
    assert serialized["schema_version"] == 4
    serialized["schema_version"] = invalid_version
    # The canonical plan digest must not make a noncanonical wire type valid.
    with pytest.raises(ValueError, match="schema_version"):
        PortfolioPlan.from_dict(serialized)
    assert PortfolioPlan.from_dict(plan.to_dict()).plan_sha256 == plan.plan_sha256
    assert book.balance == Decimal("100.00") and not book.tickets
