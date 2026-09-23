from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.domain import MarketEvent, MarketType
from autosport.research_strategy import (
    market_event_evidence_hash,
    research_market_snapshot_hash,
)


def _event(*, sport: str) -> MarketEvent:
    timestamp = "2026-09-22T10:00:00+00:00"
    return MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        decimal_odds=Decimal("2.00"),
        observed_ts=timestamp,
        source_id="provider",
        sequence=1,
        market_type=next(iter(MarketType)),
        status="open",
        source_ts=timestamp,
        ingest_ts=timestamp,
        score_state=None,
        metadata={},
        sport=sport,
    )


def test_research_event_evidence_hash_binds_sport_identity() -> None:
    football = _event(sport="football")
    tennis = replace(football, sport="tennis")

    assert (
        football.event_id,
        football.market_id,
        football.selection_id,
        football.decimal_odds,
    ) == (
        tennis.event_id,
        tennis.market_id,
        tennis.selection_id,
        tennis.decimal_odds,
    )
    assert football.sport != tennis.sport
    assert football.quote_key != tennis.quote_key
    assert market_event_evidence_hash(football) != market_event_evidence_hash(tennis)


def test_research_market_snapshot_hash_binds_sport_inside_projection() -> None:
    football = _event(sport="football")
    tennis = replace(football, sport="tennis")
    lookup_key = "fixed-research-slot"

    # Canonical quote identity already binds sport. Hold the caller lookup key
    # constant here to prove the research event projection independently does too.
    assert football.quote_key != tennis.quote_key
    assert research_market_snapshot_hash(
        {lookup_key: football},
        (lookup_key,),
    ) != research_market_snapshot_hash(
        {lookup_key: tennis},
        (lookup_key,),
    )

@pytest.mark.parametrize(
    ("field_name", "first_value", "second_value"),
    [
        ("competition_id", "league:a", "league:b"),
        ("market_semantics_id", "soccer:h2h:v1", "soccer:h2h:v2"),
        ("provider_source_class", "exchange", "sportsbook"),
        ("exchange_side", "back", "lay"),
    ],
)
def test_research_hashes_bind_concrete_optional_market_identity(
    field_name: str,
    first_value: str,
    second_value: str,
) -> None:
    baseline = _event(sport="football")
    first = replace(baseline, **{field_name: first_value})
    second = replace(baseline, **{field_name: second_value})
    lookup_key = "fixed-research-slot"

    assert first.quote_key == second.quote_key or field_name == "exchange_side"
    assert market_event_evidence_hash(first) != market_event_evidence_hash(second)
    assert research_market_snapshot_hash(
        {lookup_key: first},
        (lookup_key,),
    ) != research_market_snapshot_hash(
        {lookup_key: second},
        (lookup_key,),
    )

