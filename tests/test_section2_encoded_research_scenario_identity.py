from decimal import Decimal

import pytest

from autosport.domain import MarketEvent, _quote_identity, _quote_identity_components
from autosport.forecasting import parse_iso_timestamp
from autosport.research_strategy import (
    _validate_scenario_future_identity,
    _validate_scenario_space_binding,
)
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome


_DECISION_TS = "2026-10-07T00:00:00+00:00"


def _event(selection_id: str) -> MarketEvent:
    return MarketEvent(
        event_id="event|2026",
        market_id="market|spread",
        selection_id=selection_id,
        decimal_odds=Decimal("2"),
        observed_ts=_DECISION_TS,
        source_id="provider-1",
        sequence=1 if selection_id == "a" else 2,
        ingest_ts=_DECISION_TS,
    )


def test_component_boundary_quote_decoder_round_trips_structured_identity() -> None:
    quote_key = _quote_identity("event|2026", "market|spread", "player|a", None)
    assert quote_key.startswith("component-boundary-v1-")
    assert _quote_identity_components(quote_key) == (
        None,
        "event|2026",
        "market|spread",
        "player|a",
        None,
    )


def test_quote_decoder_preserves_prefix_looking_legacy_event_id() -> None:
    quote_key = "sport-v2-not-an-encoding|winner|alice"
    assert _quote_identity_components(quote_key) == (
        None,
        "sport-v2-not-an-encoding",
        "winner",
        "alice",
        None,
    )


def test_scenario_space_rejects_absent_encoded_same_market_outcome() -> None:
    first = _event("a")
    second = _event("b")
    fabricated = _quote_identity(
        first.event_id,
        first.market_id,
        "fabricated|selection",
        None,
    )
    group = ScenarioGroup(
        "encoded-market",
        (
            ScenarioOutcome(first.quote_key),
            ScenarioOutcome(second.quote_key),
            ScenarioOutcome(fabricated),
        ),
    )
    with pytest.raises(ValueError, match="outcome absent from replay state"):
        _validate_scenario_space_binding(
            (group,),
            {first.quote_key: first, second.quote_key: second},
            parse_iso_timestamp(_DECISION_TS),
        )


def test_future_market_guard_decodes_encoded_quote_identity() -> None:
    fabricated = _quote_identity(
        "event|2026",
        "market|future",
        "selection|ghost",
        None,
    )
    group = ScenarioGroup("future-market", (ScenarioOutcome(fabricated),))
    decision_time = parse_iso_timestamp(_DECISION_TS)
    with pytest.raises(ValueError, match="market identity first appears after decision"):
        _validate_scenario_future_identity(
            (group,),
            {},
            {
                (None, "event|2026"): parse_iso_timestamp(
                    "2026-10-06T23:59:00+00:00"
                )
            },
            {
                (None, "event|2026", "market|future"): parse_iso_timestamp(
                    "2026-10-07T00:01:00+00:00"
                )
            },
            decision_time,
        )


def test_future_market_guard_keeps_sport_in_event_market_identity() -> None:
    fabricated = _quote_identity(
        "shared-event",
        "shared-market",
        "ghost",
        "tennis",
    )
    group = ScenarioGroup("sport-qualified-future-market", (ScenarioOutcome(fabricated),))
    decision_time = parse_iso_timestamp(_DECISION_TS)
    earlier = parse_iso_timestamp("2026-10-06T23:59:00+00:00")
    future = parse_iso_timestamp("2026-10-07T00:01:00+00:00")

    with pytest.raises(ValueError, match="market identity first appears after decision"):
        _validate_scenario_future_identity(
            (group,),
            {},
            {
                ("soccer", "shared-event"): earlier,
                ("tennis", "shared-event"): future,
            },
            {
                ("soccer", "shared-event", "shared-market"): earlier,
                ("tennis", "shared-event", "shared-market"): future,
            },
            decision_time,
        )


def test_scenario_space_does_not_merge_same_local_market_across_sports() -> None:
    tennis = MarketEvent(
        event_id="shared-event",
        market_id="shared-market",
        selection_id="tennis-selection",
        decimal_odds=Decimal("2"),
        observed_ts=_DECISION_TS,
        source_id="provider-1",
        sequence=1,
        ingest_ts=_DECISION_TS,
        sport="tennis",
    )
    soccer = MarketEvent(
        event_id="shared-event",
        market_id="shared-market",
        selection_id="soccer-selection",
        decimal_odds=Decimal("2"),
        observed_ts=_DECISION_TS,
        source_id="provider-1",
        sequence=2,
        ingest_ts=_DECISION_TS,
        sport="soccer",
    )
    group = ScenarioGroup(
        "tennis-only",
        (ScenarioOutcome(tennis.quote_key),),
    )

    _validate_scenario_space_binding(
        (group,),
        {
            tennis.quote_key: tennis,
            soccer.quote_key: soccer,
        },
        parse_iso_timestamp(_DECISION_TS),
    )


def test_research_future_guard_revalidates_mutated_scenario_identity_before_hashing() -> None:
    class HostileQuoteKey(str):
        def __hash__(self) -> int:
            raise AssertionError("scenario quote hash dispatched before exact admission")

    first = ScenarioOutcome("event|market|a")
    group = ScenarioGroup(
        "mutated-scenario",
        (
            first,
            ScenarioOutcome("event|market|b"),
        ),
    )
    object.__setattr__(first, "quote_key", HostileQuoteKey(first.quote_key))

    with pytest.raises(
        ValueError,
        match="scenario outcome quote_key must be a non-empty trimmed string",
    ):
        _validate_scenario_future_identity(
            (group,),
            {},
            {},
            {},
            parse_iso_timestamp(_DECISION_TS),
        )
