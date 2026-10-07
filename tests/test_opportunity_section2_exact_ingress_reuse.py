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
    PredictiveEligibilityEvidence,
    QuoteRef,
    StrategyClass,
)


_HASH = "a" * 64
_TS = "2026-09-16T16:00:00+00:00"


def _quote() -> QuoteRef:
    return QuoteRef.from_market_event(
        MarketEvent(
            event_id="event-exact-type",
            market_id="market-exact-type",
            selection_id="selection-exact-type",
            decimal_odds=Decimal("2.00"),
            observed_ts=_TS,
            source_id="provider-exact-type",
            sequence=1,
            source_ts="2026-09-16T15:59:59+00:00",
            ingest_ts="2026-09-16T16:00:01+00:00",
        )
    )


class _HostileEvidenceRef(EvidenceRef):
    __slots__ = ()

    def __hash__(self) -> int:
        raise AssertionError("EvidenceRef subclass hash dispatch executed")


class _HostileMarketEvent(MarketEvent):
    __slots__ = ()

    def to_dict(self) -> dict[str, object]:
        raise AssertionError("MarketEvent subclass serialization executed")


class _HostileQuoteRef(QuoteRef):
    __slots__ = ()

    @property
    def identity_key(self) -> tuple[str, str, str, str, str, int]:
        raise AssertionError("QuoteRef subclass identity dispatch executed")


class _ForecastRecordSubclass(ForecastRecord):
    __slots__ = ()


class _PredictiveEligibilitySubclass(PredictiveEligibilityEvidence):
    __slots__ = ()


class _ForecastRefSubclass(ForecastRef):
    __slots__ = ()


class _OpportunitySubclass(Opportunity):
    __slots__ = ()


class _OpportunitySetSubclass(OpportunitySet):
    __slots__ = ()


class _PlanAllocationSubclass(PlanAllocation):
    __slots__ = ()


class _HostileTuple(tuple):
    def __iter__(self):
        raise AssertionError("hostile tuple iteration executed")


def test_evidence_ref_subclass_fails_before_hash_dispatch() -> None:
    hostile = _HostileEvidenceRef("attacker-authority", "attacker-reference")
    with pytest.raises(
        OpportunityContractError,
        match="opportunity evidence_refs must contain only EvidenceRef values",
    ):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=(_quote(),),
            evidence_refs=(hostile,),
        )


def test_quote_source_requires_exact_market_event() -> None:
    hostile = _HostileMarketEvent(
        event_id="event-hostile",
        market_id="market-hostile",
        selection_id="selection-hostile",
        decimal_odds=Decimal("2"),
        observed_ts=_TS,
        source_id="provider-hostile",
        sequence=1,
        source_ts="2026-09-16T15:59:59+00:00",
        ingest_ts="2026-09-16T16:00:01+00:00",
    )
    with pytest.raises(
        OpportunityContractError,
        match="quote source must be a MarketEvent",
    ):
        QuoteRef.from_market_event(hostile)


def test_opportunity_quote_member_requires_exact_quote_ref() -> None:
    hostile = _HostileQuoteRef(
        event_id="event-hostile-quote",
        market_id="market-hostile-quote",
        selection_id="selection-hostile-quote",
        source_id="provider-hostile-quote",
        sequence=1,
        decimal_odds=Decimal("2"),
        observed_ts=_TS,
        source_ts="2026-09-16T15:59:59+00:00",
        ingest_ts="2026-09-16T16:00:01+00:00",
        market_event_hash=_HASH,
    )
    with pytest.raises(
        OpportunityContractError,
        match="opportunity quotes must be QuoteRef values",
    ):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=(hostile,),
        )


def test_forecast_sources_require_exact_types() -> None:
    quote = _quote()
    forecast = _ForecastRecordSubclass(
        quote_key=quote.quote_key,
        probability=Decimal("0.5"),
        model_id="model",
        model_version="1",
        strategy_version="1",
        model_training_cutoff_ts="2026-09-16T15:00:00+00:00",
        input_cutoff_ts=_TS,
        generated_at="2026-09-16T16:00:01+00:00",
        market_snapshot_hash=_HASH,
    )
    with pytest.raises(
        OpportunityContractError,
        match="forecast source must be a ForecastRecord",
    ):
        ForecastRef.from_forecast(forecast, quote)


def test_predictive_eligibility_requires_exact_type() -> None:
    eligibility = _PredictiveEligibilitySubclass(
        evaluation_id="evaluation-hostile",
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
        as_of="2026-09-16T15:00:00+00:00",
        valid_until="2026-09-17T15:00:00+00:00",
    )
    with pytest.raises(
        OpportunityContractError,
        match="forecast predictive_eligibility must be typed evidence",
    ):
        ForecastRef(
            forecast_id="forecast-hostile",
            forecast_hash=_HASH,
            quote_key=_quote().quote_key,
            probability=Decimal("0.5"),
            input_cutoff_ts=_TS,
            market_snapshot_hash=_HASH,
            quote_market_event_hash=_HASH,
            model_id="model",
            model_version="1",
            strategy_version="1",
            uncertainty=Decimal("0.1"),
            predictive_eligibility=eligibility,
        )


def test_nested_opportunity_and_plan_members_require_exact_types() -> None:
    quote = _quote()
    hostile_forecast = _ForecastRefSubclass(
        forecast_id="forecast-hostile-ref",
        forecast_hash=_HASH,
        quote_key=quote.quote_key,
        probability=Decimal("0.5"),
        input_cutoff_ts=_TS,
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
            forecasts=(hostile_forecast,),
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

    exact_opportunity = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.ACTIONABLE,
        quotes=(quote,),
    )
    hostile_set = _OpportunitySetSubclass((exact_opportunity,))
    with pytest.raises(
        OpportunityContractError,
        match="opportunity_set must be an OpportunitySet",
    ):
        PortfolioPlan(opportunity_set=hostile_set)

    exact_set = OpportunitySet((exact_opportunity,))
    hostile_allocation = _PlanAllocationSubclass(
        exact_opportunity.opportunity_id,
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


def test_tuple_subclasses_fail_before_iteration() -> None:
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


@pytest.mark.parametrize("value", ("authority\nline", "authority\tline", "authority\rline", "authority\x7fline"))
def test_evidence_identity_rejects_non_nul_control_characters(value: str) -> None:
    with pytest.raises(OpportunityContractError, match="control characters"):
        EvidenceRef(value, "reference-1")


@pytest.mark.parametrize("value", ("event\nline", "event\tline", "event\rline", "event\x7fline"))
def test_quote_identity_rejects_non_nul_control_characters(value: str) -> None:
    with pytest.raises(OpportunityContractError, match="control characters"):
        QuoteRef(
            event_id=value,
            market_id="market-1",
            selection_id="selection-1",
            source_id="provider-1",
            sequence=1,
            decimal_odds=Decimal("2.0"),
            observed_ts=_TS,
            source_ts=None,
            ingest_ts=_TS,
            market_event_hash=_HASH,
        )
