from __future__ import annotations

from decimal import Decimal

import pytest

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


_HASH = "a" * 64
_TS = "2026-10-07T00:00:00+00:00"


def _quote() -> QuoteRef:
    return QuoteRef(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        source_id="provider-1",
        sequence=1,
        decimal_odds=Decimal("2.5"),
        observed_ts=_TS,
        source_ts=None,
        ingest_ts=_TS,
        market_event_hash=_HASH,
        market_snapshot_hash=_HASH,
    )


def _opportunity(*, decision: OpportunityDecision = OpportunityDecision.WAIT) -> Opportunity:
    return Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=decision,
        quotes=(_quote(),),
    )


def test_opportunity_revalidates_same_type_quote_identity_after_construction() -> None:
    quote = _quote()
    object.__setattr__(quote, "event_id", " event-1")
    with pytest.raises(OpportunityContractError, match="quote event_id"):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=(quote,),
        )


def test_opportunity_revalidates_same_type_forecast_identity_after_construction() -> None:
    quote = _quote()
    forecast = ForecastRef(
        forecast_id="forecast-1",
        forecast_hash=_HASH,
        quote_key=quote.quote_key,
        probability=Decimal("0.5"),
        input_cutoff_ts=_TS,
        market_snapshot_hash=_HASH,
        quote_market_event_hash=_HASH,
    )
    object.__setattr__(forecast, "forecast_id", " forecast-1")
    with pytest.raises(OpportunityContractError, match="forecast_id"):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=(quote,),
            forecasts=(forecast,),
        )


def test_opportunity_revalidates_same_type_evidence_identity_after_construction() -> None:
    evidence = EvidenceRef("authority-1", "reference-1")
    object.__setattr__(evidence, "authority", " authority-1")
    with pytest.raises(OpportunityContractError, match="evidence authority"):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=(_quote(),),
            evidence_refs=(evidence,),
        )


def test_opportunity_set_rejects_post_init_nested_identity_tamper() -> None:
    opportunity = _opportunity()
    quote = opportunity.quotes[0]
    object.__setattr__(quote, "selection_id", " selection-1")
    with pytest.raises(
        OpportunityContractError,
        match="non-canonical Opportunity",
    ):
        OpportunitySet((opportunity,))


def test_portfolio_plan_revalidates_allocation_identity_after_construction() -> None:
    opportunity = _opportunity(decision=OpportunityDecision.ACTIONABLE)
    opportunity_set = OpportunitySet((opportunity,))
    allocation = PlanAllocation(opportunity.opportunity_id, Decimal("1"))
    object.__setattr__(allocation, "opportunity_id", "A" * 64)
    with pytest.raises(
        OpportunityContractError,
        match="canonical lowercase SHA-256",
    ):
        PortfolioPlan(
            opportunity_set=opportunity_set,
            allocations=(allocation,),
        )


def test_portfolio_plan_revalidates_evidence_identity_after_construction() -> None:
    opportunity_set = OpportunitySet((_opportunity(),))
    evidence = EvidenceRef("risk-authority", "risk-reference")
    object.__setattr__(evidence, "reference", " risk-reference")
    with pytest.raises(OpportunityContractError, match="evidence reference"):
        PortfolioPlan(
            opportunity_set=opportunity_set,
            risk_evidence_refs=(evidence,),
        )


def test_valid_opportunity_and_plan_round_trip_remains_stable() -> None:
    opportunity = _opportunity(decision=OpportunityDecision.ACTIONABLE)
    opportunity_set = OpportunitySet((opportunity,))
    refs = (EvidenceRef("authority", "reference"),)
    plan = PortfolioPlan(
        opportunity_set=opportunity_set,
        allocations=(PlanAllocation(opportunity.opportunity_id, Decimal("1")),),
        portfolio_evidence_refs=refs,
        risk_evidence_refs=refs,
        ledger_state_refs=refs,
    )

    assert Opportunity.from_dict(opportunity.to_dict()) == opportunity
    assert OpportunitySet.from_dict(opportunity_set.to_dict()) == opportunity_set
    assert PortfolioPlan.from_dict(plan.to_dict()) == plan
