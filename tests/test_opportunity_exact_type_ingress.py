from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.domain import MarketEvent
from autosport.forecasting import ForecastRecord
from autosport.opportunity import (
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


_HASH = "a" * 64


def _quote() -> QuoteRef:
    return QuoteRef.from_market_event(
        MarketEvent(
            event_id="event-exact-type",
            market_id="market-exact-type",
            selection_id="selection-exact-type",
            decimal_odds=Decimal("2.00"),
            observed_ts="2026-09-16T16:00:00+00:00",
            source_id="provider-exact-type",
            sequence=1,
            source_ts="2026-09-16T15:59:59+00:00",
            ingest_ts="2026-09-16T16:00:01+00:00",
        )
    )


class _ForecastRecordSubclass(ForecastRecord):
    __slots__ = ()


class _PredictiveEligibilitySubclass(PredictiveEligibilityEvidence):
    __slots__ = ()


class _ForecastRefSubclass(ForecastRef):
    __slots__ = ()


class _QuoteRefSubclass(QuoteRef):
    __slots__ = ()


class _OpportunitySubclass(Opportunity):
    __slots__ = ()


class _OpportunitySetSubclass(OpportunitySet):
    __slots__ = ()


class _PlanAllocationSubclass(PlanAllocation):
    __slots__ = ()


def test_forecast_source_and_quote_require_exact_types() -> None:
    quote = _quote()
    hostile_forecast = _ForecastRecordSubclass(
        quote_key=quote.quote_key,
        probability=Decimal("0.5"),
        model_id="model",
        model_version="1",
        strategy_version="1",
        model_training_cutoff_ts="2026-09-16T15:00:00+00:00",
        input_cutoff_ts="2026-09-16T16:00:00+00:00",
        generated_at="2026-09-16T16:00:01+00:00",
        market_snapshot_hash=_HASH,
    )
    with pytest.raises(
        OpportunityContractError,
        match="forecast source must be a ForecastRecord",
    ):
        ForecastRef.from_forecast(hostile_forecast, quote)

    exact_forecast = ForecastRecord(
        quote_key=quote.quote_key,
        probability=Decimal("0.5"),
        model_id="model",
        model_version="1",
        strategy_version="1",
        model_training_cutoff_ts="2026-09-16T15:00:00+00:00",
        input_cutoff_ts="2026-09-16T16:00:00+00:00",
        generated_at="2026-09-16T16:00:01+00:00",
        market_snapshot_hash=_HASH,
    )
    hostile_quote = _QuoteRefSubclass(
        event_id="event-hostile-quote",
        market_id="market-hostile-quote",
        selection_id="selection-hostile-quote",
        source_id="provider-hostile-quote",
        sequence=1,
        decimal_odds=Decimal("2"),
        observed_ts="2026-09-16T16:00:00+00:00",
        source_ts="2026-09-16T15:59:59+00:00",
        ingest_ts="2026-09-16T16:00:01+00:00",
        market_event_hash=_HASH,
    )
    with pytest.raises(
        OpportunityContractError,
        match="forecast quote must be a QuoteRef",
    ):
        ForecastRef.from_forecast(exact_forecast, hostile_quote)


def test_predictive_eligibility_requires_exact_type() -> None:
    eligibility = _PredictiveEligibilitySubclass(
        evaluation_id="evaluation-hostile-type",
        evaluation_sha256=_HASH,
        protocol_sha256=_HASH,
        admission_policy_sha256=_HASH,
        model_id="model",
        model_version="1",
        strategy_version="1",
        uncertainty_kind="absolute_probability_radius_v1",
        sample_size=100,
        minimum_sample_size=50,
        maximum_uncertainty=Decimal("0.1"),
        as_of="2026-09-16T16:00:00+00:00",
        valid_until="2026-09-17T16:00:00+00:00",
    )
    with pytest.raises(
        OpportunityContractError,
        match="forecast predictive_eligibility must be typed evidence",
    ):
        ForecastRef(
            forecast_id="forecast-hostile-evidence-type",
            forecast_hash=_HASH,
            quote_key=_quote().quote_key,
            probability=Decimal("0.5"),
            input_cutoff_ts="2026-09-16T16:00:00+00:00",
            market_snapshot_hash=_HASH,
            quote_market_event_hash=_HASH,
            model_id="model",
            model_version="1",
            strategy_version="1",
            uncertainty=Decimal("0.1"),
            predictive_eligibility=eligibility,
        )


def test_nested_opportunity_members_require_exact_types() -> None:
    quote = _quote()
    forecast = _ForecastRefSubclass(
        forecast_id="forecast-hostile-ref-type",
        forecast_hash=_HASH,
        quote_key=quote.quote_key,
        probability=Decimal("0.5"),
        input_cutoff_ts="2026-09-16T16:00:00+00:00",
        market_snapshot_hash=_HASH,
        quote_market_event_hash=_HASH,
    )
    with pytest.raises(
        OpportunityContractError,
        match="opportunity forecasts must be ForecastRef values",
    ):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=(quote,),
            forecasts=(forecast,),
        )

    hostile_opportunity = _OpportunitySubclass(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.WAIT,
        quotes=(quote,),
    )
    with pytest.raises(
        OpportunityContractError,
        match="opportunity set must contain only Opportunity values",
    ):
        OpportunitySet((hostile_opportunity,))


def test_portfolio_plan_members_require_exact_types() -> None:
    opportunity = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.ACTIONABLE,
        quotes=(_quote(),),
    )
    hostile_set = _OpportunitySetSubclass((opportunity,))
    with pytest.raises(
        OpportunityContractError,
        match="opportunity_set must be an OpportunitySet",
    ):
        PortfolioPlan(opportunity_set=hostile_set)

    exact_set = OpportunitySet((opportunity,))
    hostile_allocation = _PlanAllocationSubclass(
        opportunity.opportunity_id,
        Decimal("1"),
    )
    with pytest.raises(
        OpportunityContractError,
        match="allocations must contain only PlanAllocation values",
    ):
        PortfolioPlan(
            opportunity_set=exact_set,
            allocations=(hostile_allocation,),
        )

class _HostileTuple(tuple):
    def __iter__(self):
        raise AssertionError("hostile tuple iteration executed")


def test_canonical_tuple_fields_reject_subclasses_before_iteration() -> None:
    quote = _quote()

    with pytest.raises(OpportunityContractError, match="opportunity quotes must be a tuple"):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=_HostileTuple((quote,)),  # type: ignore[arg-type]
        )

    with pytest.raises(OpportunityContractError, match="opportunity forecasts must be a tuple"):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=(quote,),
            forecasts=_HostileTuple(()),  # type: ignore[arg-type]
        )

    with pytest.raises(OpportunityContractError, match="opportunity evidence_refs must be a tuple"):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=(quote,),
            evidence_refs=_HostileTuple(()),  # type: ignore[arg-type]
        )

    opportunity = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.WAIT,
        quotes=(quote,),
    )
    with pytest.raises(OpportunityContractError, match="opportunity set members must be a tuple"):
        OpportunitySet(_HostileTuple((opportunity,)))  # type: ignore[arg-type]

    opportunity_set = OpportunitySet((opportunity,))
    with pytest.raises(OpportunityContractError, match="allocations must be a tuple"):
        PortfolioPlan(
            opportunity_set=opportunity_set,
            allocations=_HostileTuple(()),  # type: ignore[arg-type]
        )

    with pytest.raises(OpportunityContractError, match="portfolio_evidence_refs must be a tuple"):
        PortfolioPlan(
            opportunity_set=opportunity_set,
            portfolio_evidence_refs=_HostileTuple(()),  # type: ignore[arg-type]
        )

