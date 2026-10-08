"""Plan 2 Section 3: non-executing portfolio contract and restart readback.

These are provider-free financial-boundary checks, not acceptance evidence for a
real bookmaker, an executed ticket, or the whole product.
"""

from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal

import pytest

from autosport.opportunity import (
    EvidenceRef,
    Opportunity,
    OpportunityContractError,
    OpportunityDecision,
    OpportunitySet,
    PlanAllocation,
    PortfolioPlan,
    QuoteRef,
    StrategyClass,
)
from autosport.paper import PaperBook


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _quote(index: int) -> QuoteRef:
    return QuoteRef(
        event_id=f"event-{index}",
        market_id=f"market-{index}",
        selection_id=f"selection-{index}",
        source_id="recorded-fixture",
        sequence=index,
        decimal_odds=Decimal("2.50"),
        observed_ts="2026-10-08T09:00:00+00:00",
        source_ts="2026-10-08T08:59:59+00:00",
        ingest_ts="2026-10-08T09:00:01+00:00",
        market_event_hash=_digest(f"event-{index}"),
        market_snapshot_hash=_digest(f"snapshot-{index}"),
        sport="football",
    )


def _opportunity(
    decision: OpportunityDecision = OpportunityDecision.ACTIONABLE,
) -> Opportunity:
    return Opportunity(
        strategy_class=StrategyClass.PARLAY,
        decision=decision,
        quotes=(_quote(2), _quote(1)),
    )


def _positive_plan() -> PortfolioPlan:
    opportunity = _opportunity()
    return PortfolioPlan(
        opportunity_set=OpportunitySet((opportunity,)),
        allocations=(
            PlanAllocation(opportunity.opportunity_id, Decimal("7.25")),
        ),
        portfolio_evidence_refs=(EvidenceRef("PortfolioEngine", "portfolio:1"),),
        risk_evidence_refs=(EvidenceRef("PaperRiskPolicy", "risk:1"),),
        ledger_state_refs=(EvidenceRef("PaperBook", "paper-snapshot:1"),),
    )


def test_proposal_roundtrip_is_stable_and_never_debits_paper_bank() -> None:
    book = PaperBook("1000")
    before = (book.initial_bankroll, book.balance, dict(book.tickets))
    first = _positive_plan()

    # Persisted/reloaded proposal identity is not a ticket execution.
    restored = PortfolioPlan.from_dict(
        json.loads(json.dumps(first.to_dict(), ensure_ascii=False))
    )
    assert restored == first
    assert restored.plan_id == first.plan_id
    assert restored.allocations[0].stake == Decimal("7.25")
    assert (book.initial_bankroll, book.balance, book.tickets) == before
    assert book.committed_stake == Decimal("0")


@pytest.mark.parametrize("decision", [OpportunityDecision.WAIT, OpportunityDecision.ZERO])
def test_wait_zero_are_unchanged_on_restart_and_never_admit_positive_stake(
    decision: OpportunityDecision,
) -> None:
    item = _opportunity(decision)
    zero_plan = PortfolioPlan(
        opportunity_set=OpportunitySet((item,)),
        allocations=(PlanAllocation(item.opportunity_id, Decimal("0")),),
    )
    assert PortfolioPlan.from_dict(zero_plan.to_dict()) == zero_plan
    with pytest.raises(OpportunityContractError, match="WAIT/ZERO"):
        PortfolioPlan(
            opportunity_set=OpportunitySet((item,)),
            allocations=(PlanAllocation(item.opportunity_id, Decimal("0.01")),),
            portfolio_evidence_refs=(EvidenceRef("PortfolioEngine", "portfolio:1"),),
            risk_evidence_refs=(EvidenceRef("PaperRiskPolicy", "risk:1"),),
            ledger_state_refs=(EvidenceRef("PaperBook", "paper-snapshot:1"),),
        )


def test_conflicting_decisions_for_identical_evidence_are_rejected() -> None:
    actionable = _opportunity(OpportunityDecision.ACTIONABLE)
    waiting = _opportunity(OpportunityDecision.WAIT)
    assert actionable.conflict_key == waiting.conflict_key
    assert actionable.opportunity_id != waiting.opportunity_id
    with pytest.raises(OpportunityContractError, match="conflicting decisions"):
        OpportunitySet((actionable, waiting))


def test_mutated_serialized_proposal_fails_closed() -> None:
    raw = _positive_plan().to_dict()
    for mutate in (
        lambda payload: payload.update(plan_id="0" * 64),
        lambda payload: payload["allocations"][0].update(
            opportunity_id="f" * 64
        ),
        lambda payload: payload["allocations"][0].update(stake="NaN"),
        lambda payload: payload.update(provider_receipt="false authority"),
        lambda payload: payload["opportunity_set"].update(
            opportunity_set_id="0" * 64
        ),
    ):
        corrupted = copy.deepcopy(raw)
        mutate(corrupted)
        with pytest.raises((OpportunityContractError, ValueError)):
            PortfolioPlan.from_dict(corrupted)


def test_positive_proposal_requires_all_three_upstream_evidence_classes() -> None:
    item = _opportunity()
    for omitted in ("portfolio_evidence_refs", "risk_evidence_refs", "ledger_state_refs"):
        fields = {
            "portfolio_evidence_refs": (EvidenceRef("PortfolioEngine", "p:1"),),
            "risk_evidence_refs": (EvidenceRef("PaperRiskPolicy", "r:1"),),
            "ledger_state_refs": (EvidenceRef("PaperBook", "s:1"),),
        }
        fields[omitted] = ()
        with pytest.raises(OpportunityContractError, match="portfolio, risk, and ledger"):
            PortfolioPlan(
                opportunity_set=OpportunitySet((item,)),
                allocations=(PlanAllocation(item.opportunity_id, Decimal("1")),),
                **fields,
            )


def test_invalid_money_values_fail_without_rounding_or_float_promotion() -> None:
    for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), 1.25, "1.25"):
        with pytest.raises(OpportunityContractError):
            PlanAllocation("a" * 64, value)
    exact = PlanAllocation("a" * 64, Decimal("0.001"))
    assert PlanAllocation.from_dict(exact.to_dict()) == exact
