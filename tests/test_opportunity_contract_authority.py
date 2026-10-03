from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.domain import MarketEvent
from autosport.opportunity import (
    EvidenceRef,
    ForecastRef,
    Opportunity,
    OpportunityContractError,
    OpportunityDecision,
    OpportunitySet,
    PlanAllocation,
    PortfolioPlan,
    PredictiveEligibilityEvidence,
    QuoteRef,
    StrategyClass,
)
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy


_HASH = "a" * 64


def _quote() -> QuoteRef:
    return QuoteRef.from_market_event(
        MarketEvent(
            event_id="event-authority",
            market_id="market-authority",
            selection_id="selection-authority",
            decimal_odds=Decimal("2.00"),
            observed_ts="2026-09-16T16:00:00+00:00",
            source_id="provider-authority",
            sequence=1,
            source_ts="2026-09-16T15:59:59+00:00",
            ingest_ts="2026-09-16T16:00:01+00:00",
        )
    )


def _plan() -> PortfolioPlan:
    opportunity = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.ACTIONABLE,
        quotes=(_quote(),),
    )
    return PortfolioPlan(
        opportunity_set=OpportunitySet((opportunity,)),
        allocations=(
            PlanAllocation(opportunity.opportunity_id, Decimal("1.00")),
        ),
        portfolio_evidence_refs=(
            EvidenceRef("PortfolioEngine", "scenario-report:authority-test"),
        ),
        risk_evidence_refs=(
            EvidenceRef("PaperRiskPolicy", "risk-decision:authority-test"),
        ),
        ledger_state_refs=(
            EvidenceRef("PaperBook", "ledger-state:authority-test"),
        ),
    )


def test_contract_construction_does_not_mutate_paperbook_or_risk_state() -> None:
    book = PaperBook(Decimal("1000"))
    policy = PaperRiskPolicy()
    state_before = (
        book.initial_bankroll,
        book.balance,
        book.committed_stake,
        tuple(book.tickets.items()),
    )
    risk_before = policy.evaluate(book, Decimal("1"))

    plan = _plan()

    state_after = (
        book.initial_bankroll,
        book.balance,
        book.committed_stake,
        tuple(book.tickets.items()),
    )
    risk_after = policy.evaluate(book, Decimal("1"))
    assert plan.allocations[0].stake == Decimal("1.00")
    assert state_after == state_before
    assert risk_after == risk_before
    assert book.tickets == {}


def test_nonfinite_decimal_contract_inputs_fail_closed() -> None:
    with pytest.raises(
        OpportunityContractError,
        match="allocation stake must be an exact finite Decimal",
    ):
        PlanAllocation(_HASH, Decimal("NaN"))

    with pytest.raises(
        OpportunityContractError,
        match="quote decimal_odds must be an exact finite Decimal",
    ):
        QuoteRef(
            event_id="event",
            market_id="market",
            selection_id="selection",
            source_id="provider",
            sequence=1,
            decimal_odds=Decimal("Infinity"),
            observed_ts="2026-09-16T16:00:00+00:00",
            source_ts="2026-09-16T15:59:59+00:00",
            ingest_ts="2026-09-16T16:00:01+00:00",
            market_event_hash=_HASH,
        )

    with pytest.raises(
        OpportunityContractError,
        match="forecast probability must be an exact finite Decimal",
    ):
        ForecastRef(
            forecast_id="forecast",
            forecast_hash=_HASH,
            quote_key="event|market|selection",
            probability=Decimal("NaN"),
            input_cutoff_ts="2026-09-16T16:00:00+00:00",
            market_snapshot_hash=_HASH,
            quote_market_event_hash=_HASH,
        )


def test_opportunity_set_rejects_conflicting_decisions_for_same_facts() -> None:
    quote = _quote()
    waiting = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.WAIT,
        quotes=(quote,),
    )
    actionable = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.ACTIONABLE,
        quotes=(quote,),
    )

    assert waiting.opportunity_id != actionable.opportunity_id
    assert waiting.conflict_key == actionable.conflict_key
    with pytest.raises(
        OpportunityContractError,
        match="conflicting decisions for the same canonical opportunity",
    ):
        OpportunitySet((waiting, actionable))


class _HostileDecimal(Decimal):
    def is_finite(self) -> bool:
        raise AssertionError("Decimal subclass virtual dispatch must not execute")


def test_decimal_subclasses_fail_closed_before_virtual_dispatch() -> None:
    with pytest.raises(
        OpportunityContractError,
        match="allocation stake must be an exact finite Decimal",
    ):
        PlanAllocation(_HASH, _HostileDecimal("1"))

    with pytest.raises(
        OpportunityContractError,
        match="quote decimal_odds must be an exact finite Decimal",
    ):
        QuoteRef(
            event_id="event-hostile-decimal",
            market_id="market-hostile-decimal",
            selection_id="selection-hostile-decimal",
            source_id="provider-hostile-decimal",
            sequence=1,
            decimal_odds=_HostileDecimal("2"),
            observed_ts="2026-09-16T16:00:00+00:00",
            source_ts="2026-09-16T15:59:59+00:00",
            ingest_ts="2026-09-16T16:00:01+00:00",
            market_event_hash=_HASH,
        )

    with pytest.raises(
        OpportunityContractError,
        match="forecast probability must be an exact finite Decimal",
    ):
        ForecastRef(
            forecast_id="forecast-hostile-probability",
            forecast_hash=_HASH,
            quote_key="event|market|selection",
            probability=_HostileDecimal("0.5"),
            input_cutoff_ts="2026-09-16T16:00:00+00:00",
            market_snapshot_hash=_HASH,
            quote_market_event_hash=_HASH,
        )

    with pytest.raises(
        OpportunityContractError,
        match="forecast uncertainty must be an exact finite Decimal",
    ):
        ForecastRef(
            forecast_id="forecast-hostile-uncertainty",
            forecast_hash=_HASH,
            quote_key="event|market|selection",
            probability=Decimal("0.5"),
            input_cutoff_ts="2026-09-16T16:00:00+00:00",
            market_snapshot_hash=_HASH,
            quote_market_event_hash=_HASH,
            model_id="model",
            model_version="1",
            strategy_version="1",
            uncertainty=_HostileDecimal("0.1"),
        )

    with pytest.raises(
        OpportunityContractError,
        match="predictive maximum_uncertainty must be an exact finite Decimal",
    ):
        PredictiveEligibilityEvidence(
            evaluation_id="evaluation-hostile-decimal",
            evaluation_sha256=_HASH,
            protocol_sha256=_HASH,
            admission_policy_sha256=_HASH,
            model_id="model",
            model_version="1",
            strategy_version="1",
            uncertainty_kind="absolute_probability_radius_v1",
            sample_size=100,
            minimum_sample_size=50,
            maximum_uncertainty=_HostileDecimal("0.1"),
            as_of="2026-09-16T16:00:00+00:00",
            valid_until="2026-09-17T16:00:00+00:00",
        )

