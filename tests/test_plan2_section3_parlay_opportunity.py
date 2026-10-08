"""Plan 2 Section 3: explicit parlay identity remains a non-executing proposal."""

from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.domain import MarketEvent
from autosport.forecasting import ForecastRecord
from autosport.opportunity import (
    EvidenceRef,
    ForecastRef,
    Opportunity,
    OpportunityContractError,
    OpportunityDecision,
    OpportunitySet,
    PlanAllocation,
    PortfolioPlan,
    QuoteRef,
    StrategyClass,
)


def _quote(index: int) -> QuoteRef:
    event = MarketEvent(
        event_id=f"event-{index}",
        market_id=f"market-{index}",
        selection_id=f"selection-{index}",
        decimal_odds=Decimal("2.10"),
        observed_ts="2026-10-08T10:00:00+00:00",
        source_id="recorded-provider",
        sequence=index,
        source_ts="2026-10-08T09:59:59+00:00",
        ingest_ts="2026-10-08T10:00:01+00:00",
        metadata={"sport": "football"},
    )
    return QuoteRef.from_market_event(
        event, market_snapshot_hash="b" * 64,
    )


def _forecast(quote: QuoteRef, index: int) -> ForecastRef:
    forecast = ForecastRecord(
        quote_key=quote.quote_key,
        probability=Decimal("0.55"),
        model_id="recorded-model",
        model_version="1",
        strategy_version="1",
        model_training_cutoff_ts="2026-10-01T00:00:00+00:00",
        input_cutoff_ts="2026-10-08T09:59:58+00:00",
        generated_at="2026-10-08T09:59:59+00:00",
        evidence_hashes=("a" * 64,),
        market_snapshot_hash="b" * 64,
        provenance={"split": "frozen"},
        forecast_id=f"forecast-{index}",
    )
    return ForecastRef.from_forecast(forecast, quote)


def test_parlay_is_first_class_structural_opportunity_and_roundtrips() -> None:
    quotes = (_quote(2), _quote(1))
    opportunity = Opportunity(
        strategy_class=StrategyClass.PARLAY,
        decision=OpportunityDecision.WAIT,
        quotes=quotes,
    )
    assert [q.event_id for q in opportunity.quotes] == ["event-1", "event-2"]
    assert Opportunity.from_dict(opportunity.to_dict()) == opportunity
    assert opportunity.forecasts == ()


def test_parlay_probability_claim_requires_all_leg_forecasts() -> None:
    quotes = (_quote(1), _quote(2))
    forecasts = tuple(_forecast(quote, i) for i, quote in enumerate(quotes, start=1))
    candidate = Opportunity(
        strategy_class=StrategyClass.PARLAY,
        decision=OpportunityDecision.ACTIONABLE,
        quotes=quotes,
        forecasts=forecasts,
        claims_probability_edge=True,
    )
    assert Opportunity.from_dict(candidate.to_dict()) == candidate
    with pytest.raises(OpportunityContractError, match="every quote"):
        Opportunity(
            strategy_class=StrategyClass.PARLAY,
            decision=OpportunityDecision.ACTIONABLE,
            quotes=quotes,
            forecasts=forecasts[:1],
            claims_probability_edge=True,
        )


def test_parlay_portfolio_allocation_is_proposal_not_accepted_ticket() -> None:
    opportunity = Opportunity(
        strategy_class=StrategyClass.PARLAY,
        decision=OpportunityDecision.ACTIONABLE,
        quotes=(_quote(1), _quote(2)),
    )
    members = OpportunitySet((opportunity,))
    with pytest.raises(OpportunityContractError, match="portfolio, risk, and ledger"):
        PortfolioPlan(
            opportunity_set=members,
            allocations=(PlanAllocation(opportunity.opportunity_id, Decimal("4.25")),),
        )
    plan = PortfolioPlan(
        opportunity_set=members,
        allocations=(PlanAllocation(opportunity.opportunity_id, Decimal("4.25")),),
        portfolio_evidence_refs=(EvidenceRef("PortfolioEngine", "portfolio:1"),),
        risk_evidence_refs=(EvidenceRef("PaperRiskPolicy", "risk:1"),),
        ledger_state_refs=(EvidenceRef("PaperBook", "state:1"),),
    )
    assert PortfolioPlan.from_dict(plan.to_dict()) == plan
    assert plan.allocations[0].stake == Decimal("4.25")
    assert not hasattr(plan, "accepted_ticket")
    assert not hasattr(plan, "provider_receipt")


def test_wait_or_zero_parlay_never_allocates_positive_stake() -> None:
    for decision in (OpportunityDecision.WAIT, OpportunityDecision.ZERO):
        item = Opportunity(
            strategy_class=StrategyClass.PARLAY,
            decision=decision,
            quotes=(_quote(1), _quote(2)),
        )
        with pytest.raises(OpportunityContractError, match="WAIT/ZERO"):
            PortfolioPlan(
                opportunity_set=OpportunitySet((item,)),
                allocations=(PlanAllocation(item.opportunity_id, Decimal("0.01")),),
                portfolio_evidence_refs=(EvidenceRef("PortfolioEngine", "portfolio:1"),),
                risk_evidence_refs=(EvidenceRef("PaperRiskPolicy", "risk:1"),),
                ledger_state_refs=(EvidenceRef("PaperBook", "state:1"),),
            )
