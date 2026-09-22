from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from autosport.domain import MarketEvent, MarketType
from autosport.research_strategy import (
    _validate_scenario_future_identity,
    _validate_scenario_space_binding,
)


def _event(
    *,
    selection_id: str,
    observed_ts: str,
    sport: str = "football",
) -> MarketEvent:
    return MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id=selection_id,
        decimal_odds=Decimal("2.00"),
        observed_ts=observed_ts,
        source_id="provider",
        sequence=1,
        market_type=next(iter(MarketType)),
        status="open",
        source_ts=observed_ts,
        ingest_ts=observed_ts,
        score_state=None,
        metadata={},
        sport=sport,
    )


def _group(*quote_keys: str):
    return SimpleNamespace(
        outcomes=tuple(SimpleNamespace(quote_key=quote_key) for quote_key in quote_keys)
    )


def test_future_sport_v2_market_identity_cannot_bypass_scenario_guard() -> None:
    decision_time = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    future_time = datetime(2026, 9, 22, 10, 1, tzinfo=timezone.utc)
    future_ts = future_time.isoformat()

    observed_future = _event(selection_id="observed", observed_ts=future_ts)
    alternate_future = _event(selection_id="alternate", observed_ts=future_ts)

    assert observed_future.quote_key.startswith("sport-v2-")
    assert "|" not in alternate_future.quote_key
    assert alternate_future.quote_key != observed_future.quote_key

    with pytest.raises(
        ValueError,
        match="research scenario (event|market) identity first appears after decision",
    ):
        _validate_scenario_future_identity(
            (_group(alternate_future.quote_key),),
            {observed_future.quote_key: future_time},
            {observed_future.event_id: future_time},
            {(observed_future.event_id, observed_future.market_id): future_time},
            decision_time,
        )


def test_absent_sport_v2_selection_cannot_bypass_replay_market_guard() -> None:
    observed_ts = "2026-09-22T10:00:00+00:00"
    decision_time = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)

    observed = _event(selection_id="observed", observed_ts=observed_ts)
    absent = _event(selection_id="absent", observed_ts=observed_ts)

    assert observed.quote_key.startswith("sport-v2-")
    assert "|" not in absent.quote_key
    assert absent.quote_key != observed.quote_key

    with pytest.raises(ValueError, match="research scenario outcome absent from replay state"):
        _validate_scenario_space_binding(
            (_group(observed.quote_key, absent.quote_key),),
            {observed.quote_key: observed},
            decision_time,
        )
