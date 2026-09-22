from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

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

    assert football.quote_key == tennis.quote_key
    assert football.sport != tennis.sport
    assert market_event_evidence_hash(football) != market_event_evidence_hash(tennis)


def test_research_market_snapshot_hash_binds_sport_identity() -> None:
    football = _event(sport="football")
    tennis = replace(football, sport="tennis")
    quote_key = football.quote_key

    assert research_market_snapshot_hash(
        {quote_key: football},
        (quote_key,),
    ) != research_market_snapshot_hash(
        {quote_key: tennis},
        (quote_key,),
    )
