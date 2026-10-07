from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.domain import MarketEvent, TicketLeg


_TS = "2026-10-07T00:00:00+00:00"


class _ExplosiveString(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("str subclass strip() must not execute")

    def encode(self, *args: object, **kwargs: object) -> bytes:
        raise AssertionError("str subclass encode() must not execute")


def _event(**overrides: object) -> MarketEvent:
    payload: dict[str, object] = {
        "event_id": "event-1",
        "market_id": "market-1",
        "selection_id": "selection-1",
        "decimal_odds": Decimal("2.5"),
        "observed_ts": _TS,
        "source_id": "provider-1",
        "sequence": 1,
        "ingest_ts": _TS,
    }
    payload.update(overrides)
    return MarketEvent(**payload)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("event_id", "event-1"),
        ("market_id", "market-1"),
        ("selection_id", "selection-1"),
        ("source_id", "provider-1"),
    ),
)
def test_market_event_direct_constructor_rejects_string_subclass_before_dispatch(
    field_name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError):
        _event(**{field_name: _ExplosiveString(value)})


@pytest.mark.parametrize(
    "field_name",
    ("event_id", "market_id", "selection_id", "source_id"),
)
@pytest.mark.parametrize("value", ("", " padded", "padded ", "\x00inside", "line\nbreak", "del\x7finside", "\ud800"))
def test_market_event_direct_constructor_rejects_noncanonical_identity(
    field_name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError):
        _event(**{field_name: value})


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("event_id", "event-1"),
        ("market_id", "market-1"),
        ("selection_id", "selection-1"),
    ),
)
def test_ticket_leg_direct_constructor_rejects_string_subclass_before_dispatch(
    field_name: str,
    value: str,
) -> None:
    payload = {
        "event_id": "event-1",
        "market_id": "market-1",
        "selection_id": "selection-1",
        "locked_odds": Decimal("2.5"),
    }
    payload[field_name] = _ExplosiveString(value)
    with pytest.raises(ValueError):
        TicketLeg(**payload)  # type: ignore[arg-type]


@pytest.mark.parametrize("field_name", ("event_id", "market_id", "selection_id"))
@pytest.mark.parametrize("value", ("", " padded", "padded ", "\x00inside", "line\nbreak", "del\x7finside", "\ud800"))
def test_ticket_leg_direct_constructor_rejects_noncanonical_identity(
    field_name: str,
    value: str,
) -> None:
    payload = {
        "event_id": "event-1",
        "market_id": "market-1",
        "selection_id": "selection-1",
        "locked_odds": Decimal("2.5"),
    }
    payload[field_name] = value
    with pytest.raises(ValueError):
        TicketLeg(**payload)  # type: ignore[arg-type]


def test_valid_delimiter_bearing_domain_identity_is_not_normalized_or_rewritten() -> None:
    event = _event(
        event_id="event|2026",
        market_id="market|spread",
        selection_id="player|a",
    )
    leg = TicketLeg(
        event_id="event|2026",
        market_id="market|spread",
        selection_id="player|a",
        locked_odds=Decimal("2.5"),
    )

    assert event.event_id == "event|2026"
    assert event.market_id == "market|spread"
    assert event.selection_id == "player|a"
    assert event.quote_key == leg.quote_key
