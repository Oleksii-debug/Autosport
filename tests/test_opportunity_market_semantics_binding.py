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
    QuoteRef,
    StrategyClass,
)


def _event(*, semantics: str | None) -> MarketEvent:
    return MarketEvent(
        event_id="event-semantics",
        market_id="market-semantics",
        selection_id="selection-semantics",
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-10-06T08:00:00+00:00",
        source_id="provider-semantics",
        sequence=7,
        source_ts="2026-10-06T07:59:59+00:00",
        ingest_ts="2026-10-06T08:00:01+00:00",
        sport="football",
        market_semantics_id=semantics,
    )


def test_quote_ref_preserves_canonical_market_semantics_round_trip() -> None:
    quote = QuoteRef.from_market_event(
        _event(semantics="football:match_odds:v1"),
        market_snapshot_hash="a" * 64,
    )

    assert quote.market_semantics_id == "football:match_odds:v1"
    payload = quote.to_dict()
    assert payload["market_semantics_id"] == "football:match_odds:v1"
    assert QuoteRef.from_dict(payload) == quote


def test_legacy_quote_ref_without_market_semantics_remains_compatible() -> None:
    quote = QuoteRef.from_market_event(_event(semantics=None))

    assert quote.market_semantics_id is None
    payload = quote.to_dict()
    assert "market_semantics_id" not in payload
    assert QuoteRef.from_dict(payload) == quote


def test_quote_semantics_are_not_inferred_from_quote_key() -> None:
    first = QuoteRef.from_market_event(
        _event(semantics="football:match_odds:v1")
    )
    second = QuoteRef.from_market_event(
        _event(semantics="football:match_odds:v2")
    )

    assert first.quote_key == second.quote_key
    assert first.market_semantics_id != second.market_semantics_id
    assert first.market_event_hash != second.market_event_hash


@pytest.mark.parametrize(
    "invalid",
    (
        "Football:match_odds:v1",
        "football match odds",
        "unknown",
        "mixed",
    ),
)
def test_quote_ref_rejects_noncanonical_market_semantics(invalid: str) -> None:
    payload = QuoteRef.from_market_event(
        _event(semantics="football:match_odds:v1")
    ).to_dict()
    payload["market_semantics_id"] = invalid

    with pytest.raises(
        OpportunityContractError,
        match="market_semantics_id",
    ):
        QuoteRef.from_dict(payload)


def _forecast(
    quote: QuoteRef,
    *,
    semantics: str | None,
) -> ForecastRecord:
    return ForecastRecord(
        quote_key=quote.quote_key,
        probability=Decimal("0.55"),
        model_id="model-semantics",
        model_version="1",
        strategy_version="1",
        model_training_cutoff_ts="2026-10-06T07:00:00+00:00",
        input_cutoff_ts="2026-10-06T08:00:00+00:00",
        generated_at="2026-10-06T08:00:02+00:00",
        market_snapshot_hash=quote.market_snapshot_hash,
        market_semantics_id=semantics,
    )


def test_forecast_ref_rejects_same_quote_with_different_market_semantics() -> None:
    quote = QuoteRef.from_market_event(
        _event(semantics="football:match_odds:v1"),
        market_snapshot_hash="a" * 64,
    )
    forecast = _forecast(
        quote,
        semantics="football:match_odds:v2",
    )

    with pytest.raises(
        OpportunityContractError,
        match="market semantics do not match bound QuoteRef",
    ):
        ForecastRef.from_forecast(forecast, quote)


def test_concrete_forecast_semantics_round_trip_as_schema_v3() -> None:
    quote = QuoteRef.from_market_event(
        _event(semantics="football:match_odds:v1"),
        market_snapshot_hash="a" * 64,
    )
    forecast_ref = ForecastRef.from_forecast(
        _forecast(
            quote,
            semantics="football:match_odds:v1",
        ),
        quote,
    )

    payload = forecast_ref.to_dict()
    assert payload["schema"] == "autosport.forecast_ref"
    assert payload["schema_version"] == 3
    assert payload["market_semantics_id"] == "football:match_odds:v1"
    assert ForecastRef.from_dict(payload) == forecast_ref


def test_legacy_forecast_none_semantics_preserves_schema_v2() -> None:
    quote = QuoteRef.from_market_event(
        _event(semantics=None),
        market_snapshot_hash="a" * 64,
    )
    forecast_ref = ForecastRef.from_forecast(
        _forecast(quote, semantics=None),
        quote,
    )

    payload = forecast_ref.to_dict()
    assert payload["schema_version"] == 2
    assert "market_semantics_id" not in payload
    assert ForecastRef.from_dict(payload) == forecast_ref


def test_opportunity_rejects_tampered_forecast_semantics() -> None:
    quote = QuoteRef.from_market_event(
        _event(semantics="football:match_odds:v1"),
        market_snapshot_hash="a" * 64,
    )
    forecast_ref = ForecastRef.from_forecast(
        _forecast(
            quote,
            semantics="football:match_odds:v1",
        ),
        quote,
    )
    payload = forecast_ref.to_dict()
    payload["market_semantics_id"] = "football:match_odds:v2"
    tampered = ForecastRef.from_dict(payload)

    with pytest.raises(
        OpportunityContractError,
        match="forecast evidence does not bind the exact opportunity quote snapshot",
    ):
        Opportunity(
            strategy_class=StrategyClass.PREDICTIVE_EDGE,
            decision=OpportunityDecision.WAIT,
            quotes=(quote,),
            claims_probability_edge=True,
            forecasts=(tampered,),
        )
