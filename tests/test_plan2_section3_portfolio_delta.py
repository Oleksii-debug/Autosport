"""Section 3 non-executing portfolio delta: exact PAPER, no ACK/fill authority."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, localcontext

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


def _context() -> tuple[PaperBook, PaperRiskPolicy]:
    book = PaperBook(Decimal("100.00"))
    policy = PaperRiskPolicy(
        economic_goal=EconomicGoalContract(
            goal_id="owner-risk",
            revision=1,
            bankroll_id="paper-bank",
            currency="EUR",
        ),
    )
    return book, policy


def _plan(
    book: PaperBook,
    policy: PaperRiskPolicy,
    *,
    stake: Decimal = Decimal("0"),
) -> PortfolioPlan:
    book_hash = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    assert book_hash is not None
    graph = (
        None if stake == 0 else PortfolioDependencyGraph(
            portfolio_sha256=book_hash,
            intent_sha256s=("a" * 64,),
            candidate_sha256s=("b" * 64,),
        )
    )
    return PortfolioPlan(
        decision_ts="2026-10-08T12:00:00+00:00",
        action=PortfolioAction.ZERO if stake == 0 else PortfolioAction.STAKE_VECTOR,
        stakes=(stake,),
        intent_ids=("intent-a",),
        intent_sha256s=("a" * 64,),
        opportunity_classes=("live_price_movement",),
        portfolio_sha256=book_hash,
        dependency_graph=graph,
        terminal_economics=None,
        economic_goal_contract_sha256=provenance_for(policy.economic_goal).contract_sha256,
        risk_policy_sha256=policy.provenance_sha256,
        portfolio_truth=EvidenceTruth.EXACT,
        reason="proposal only",
    )


def test_zero_preserves_full_intent_vector_and_book_is_unchanged() -> None:
    book, policy = _context()
    plan = _plan(book, policy)
    before = book.balance
    delta = PortfolioDelta.derive(book, plan, policy)
    assert delta.currency == "EUR"
    assert delta.bankroll_id == "paper-bank"
    assert delta.intent_ids == ("intent-a",)
    assert delta.stake_vector == (Decimal("0"),)
    assert delta.proposed_stake_total == Decimal("0")
    assert delta.hypothetical_cash_after == before
    assert delta.hypothetical_committed_stake_after == Decimal("0")
    assert delta.to_dict()["effect_state"] == "PROPOSED_PAPER_ONLY"
    assert book.balance == before and not book.tickets
    assert PortfolioDelta.readback(delta.to_dict(), book=book, plan=plan, risk_policy=policy) == delta


def test_positive_is_hypothetical_stake_cash_not_real_fill() -> None:
    book, policy = _context()
    plan = _plan(book, policy, stake=Decimal("10.25"))
    projected = PortfolioDelta.derive(book, plan, policy)
    assert projected.proposed_stake_total == Decimal("10.25")
    assert projected.available_cash_before == Decimal("100.00")
    assert projected.hypothetical_cash_after == Decimal("89.75")
    assert projected.hypothetical_committed_stake_after == Decimal("10.25")
    assert projected.terminal_economics_proof_sha256 is None
    assert projected.to_dict()["effect_state"] == "PROPOSED_PAPER_ONLY"
    assert book.balance == Decimal("100.00") and not book.tickets
    assert not hasattr(projected, "accepted_ticket")
    assert not hasattr(projected, "provider_receipt")


def test_crash_restart_rederives_same_proposal_without_double_debit(tmp_path) -> None:
    book, policy = _context()
    plan = _plan(book, policy, stake=Decimal("9.0001"))
    payload = PortfolioDelta.derive(book, plan, policy).to_dict()
    path = tmp_path / "Каса фінанси з пробілами.json"
    book.save(path)
    reloaded = PaperBook.load(path)
    recovered = PortfolioDelta.readback(payload, book=reloaded, plan=plan, risk_policy=policy)
    assert recovered.hypothetical_cash_after == Decimal("90.9999")
    assert reloaded.balance == Decimal("100.00") and not reloaded.tickets
    assert PortfolioDelta.readback(payload, book=reloaded, plan=plan, risk_policy=policy) == recovered


def test_wrong_owner_risk_or_old_source_hash_rejected() -> None:
    book, policy = _context()
    plan = _plan(book, policy, stake=Decimal("8"))
    payload = PortfolioDelta.derive(book, plan, policy).to_dict()
    goal = replace(policy.economic_goal, revision=2, max_stake_fraction=Decimal("0.01"))
    changed_policy = replace(policy, economic_goal=goal)
    with pytest.raises(ValueError, match="EconomicGoal"):
        PortfolioDelta.readback(payload, book=book, plan=plan, risk_policy=changed_policy)
    with pytest.raises(ValueError, match="RiskPolicy"):
        PortfolioDelta.derive(book, replace(plan, risk_policy_sha256="f" * 64), policy)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("proposed_stake_total", 8.0),
        ("currency", "USD"),
        ("effect_state", "FILLED"),
        ("stake_vector", ["8.0"]),
        ("intent_sha256s", []),
        ("hypothetical_cash_after", "99999"),
        ("delta_sha256", "0" * 64),
    ],
)
def test_serialized_corruption_or_ack_as_fill_is_not_authority(field, value) -> None:
    book, policy = _context()
    plan = _plan(book, policy, stake=Decimal("8"))
    payload = PortfolioDelta.derive(book, plan, policy).to_dict()
    tampered = {**payload, field: value}
    with pytest.raises(ValueError, match="stale, tampered or noncanonical"):
        PortfolioDelta.readback(tampered, book=book, plan=plan, risk_policy=policy)


def test_no_context_precision_loss_or_over_budget_projection() -> None:
    book, policy = _context()
    with localcontext() as ctx:
        ctx.prec = 3
        plan = _plan(book, policy, stake=Decimal("0.123456789123456789"))
        result = PortfolioDelta.derive(book, plan, policy)
    assert result.hypothetical_cash_after == Decimal("99.876543210876543211")
    over_budget = _plan(book, policy, stake=Decimal("101"))
    with pytest.raises(ValueError, match="insufficient virtual bankroll"):
        PortfolioDelta.derive(book, over_budget, policy)
