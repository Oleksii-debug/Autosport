from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.domain import MarketEvent, MarketType
from autosport.research_strategy import _candidate_leg_from_dict


def _event(*, sport: str = "football") -> MarketEvent:
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


def _raw_leg(event: MarketEvent) -> dict[str, str]:
    return {
        "quote_key": event.quote_key,
        "event_id": event.event_id,
        "market_id": event.market_id,
        "selection_id": event.selection_id,
        "sport": event.sport,
        "decimal_odds": str(event.decimal_odds),
        "probability": "0.55",
    }


def test_research_candidate_parser_preserves_sport_qualified_identity() -> None:
    event = _event()

    leg = _candidate_leg_from_dict(_raw_leg(event))

    assert leg.quote_key == event.quote_key
    assert leg.ticket_identity() == (event.event_id, event.market_id, event.selection_id)
    assert leg.sport == event.sport


def test_research_candidate_parser_rejects_sport_quote_key_mismatch() -> None:
    event = _event(sport="football")
    raw = _raw_leg(event)
    raw["sport"] = "tennis"

    with pytest.raises(
        ValueError,
        match="structured event/market/selection identity does not match quote_key",
    ):
        _candidate_leg_from_dict(raw)


def test_research_candidate_parser_requires_structured_ids_for_sport() -> None:
    event = _event()
    raw = _raw_leg(event)
    for field_name in ("event_id", "market_id", "selection_id"):
        raw.pop(field_name)

    with pytest.raises(
        ValueError,
        match="sport-qualified identity requires structured event_id, market_id, and selection_id",
    ):
        _candidate_leg_from_dict(raw)


def test_research_candidate_parser_preserves_legacy_no_sport_identity() -> None:
    leg = _candidate_leg_from_dict(
        {
            "quote_key": "legacy-event|legacy-market|legacy-selection",
            "event_id": "legacy-event",
            "market_id": "legacy-market",
            "selection_id": "legacy-selection",
            "decimal_odds": "2.00",
            "probability": "0.55",
        }
    )

    assert leg.quote_key == "legacy-event|legacy-market|legacy-selection"
    assert leg.ticket_identity() == (
        "legacy-event",
        "legacy-market",
        "legacy-selection",
    )
    assert leg.sport is None
