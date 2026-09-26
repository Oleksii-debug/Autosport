from decimal import Decimal

import pytest

from autosport.domain import MarketEvent
from autosport.providers import CanonicalNormalizer, ProviderQuote
from autosport.storage import SQLiteMarketStore


_TS = "2026-09-21T20:00:00+00:00"


def _event(
    *,
    exchange_side: str | None = None,
    sport: str | None = None,
) -> MarketEvent:
    return MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id="runner-1",
        decimal_odds=Decimal("2.5"),
        observed_ts=_TS,
        source_id="book",
        sequence=9,
        ingest_ts=_TS,
        sport=sport,
        exchange_side=exchange_side,
    )


def _provider_quote(*, exchange_side: str | None = None) -> ProviderQuote:
    return ProviderQuote(
        provider_event_id="event-1",
        provider_market_id="market-1",
        provider_selection_id="runner-1",
        decimal_odds=Decimal("2.5"),
        observed_ts=_TS,
        sequence=9,
        exchange_side=exchange_side,
    )


def test_legacy_no_side_quote_and_dedupe_identity_are_unchanged() -> None:
    event = _event()

    assert event.quote_key == "event-1|market-1|runner-1"
    assert event.dedupe_key == "book|event-1|market-1|runner-1|9"
    assert "exchange_side" not in event.to_dict()


def test_existing_sport_identity_is_exactly_unchanged_without_side() -> None:
    event = _event(sport="soccer")

    assert event.quote_key == (
        "sport-v2-WyJxdW90ZSIsInNvY2NlciIsImV2ZW50LTEiLCJtYXJrZXQtMSIs"
        "InJ1bm5lci0xIl0"
    )
    assert event.dedupe_key == (
        "sport-v2-WyJkZWR1cGUiLCJib29rIiwic29jY2VyIiwiZXZlbnQtMSIsIm1h"
        "cmtldC0xIiwicnVubmVyLTEiLDld"
    )
    assert "exchange_side" not in event.to_dict()


def test_back_and_lay_use_disjoint_canonical_identities() -> None:
    back = _event(exchange_side="back")
    lay = _event(exchange_side="lay")
    legacy = _event()

    assert back.quote_key.startswith("exchange-side-v1-")
    assert lay.quote_key.startswith("exchange-side-v1-")
    assert back.quote_key != lay.quote_key
    assert back.dedupe_key != lay.dedupe_key
    assert back.quote_key != legacy.quote_key
    assert back.dedupe_key != legacy.dedupe_key
    assert back.selection_id == lay.selection_id == legacy.selection_id == "runner-1"


def test_exchange_side_round_trips_without_changing_selection_identity() -> None:
    event = _event(exchange_side="lay")

    payload = event.to_dict()
    restored = MarketEvent.from_dict(payload)

    assert payload["exchange_side"] == "lay"
    assert restored == event
    assert restored.selection_id == "runner-1"
    assert restored.exchange_side == "lay"


def test_back_and_lay_survive_durable_store_reopen_as_distinct_current_quotes(
    tmp_path,
) -> None:
    path = tmp_path / "exchange-side.db"
    back = _event(exchange_side="back")
    lay = _event(exchange_side="lay")

    store = SQLiteMarketStore(path)
    assert store.append(back) is True
    assert store.append(lay) is True
    store.close()

    reopened = SQLiteMarketStore(path)
    try:
        events = reopened.events()
        current = reopened.current_by_source()
    finally:
        reopened.close()

    assert {event.exchange_side for event in events} == {"back", "lay"}
    assert {event.quote_key for event in events} == {back.quote_key, lay.quote_key}
    assert current[("book", back.quote_key)].exchange_side == "back"
    assert current[("book", lay.quote_key)].exchange_side == "lay"


@pytest.mark.parametrize("exchange_side", ["BACK", "Lay", "back ", "", "buy"])
def test_market_event_rejects_noncanonical_exchange_side(exchange_side: str) -> None:
    with pytest.raises(ValueError, match="exchange_side"):
        _event(exchange_side=exchange_side)


def test_market_event_rejects_non_string_exchange_side() -> None:
    with pytest.raises(ValueError, match="exchange_side"):
        _event(exchange_side=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("exchange_side", ["BACK", "Lay", "back ", "", "buy"])
def test_provider_quote_rejects_noncanonical_exchange_side(exchange_side: str) -> None:
    with pytest.raises(ValueError, match="exchange_side"):
        _provider_quote(exchange_side=exchange_side)


def test_provider_quote_rejects_non_string_exchange_side() -> None:
    with pytest.raises(TypeError, match="exchange_side"):
        _provider_quote(exchange_side=1)  # type: ignore[arg-type]


def test_normalizer_preserves_exchange_side_as_identity_dimension() -> None:
    normalizer = CanonicalNormalizer()

    back = normalizer.normalize("matchbook", _provider_quote(exchange_side="back"))
    lay = normalizer.normalize("matchbook", _provider_quote(exchange_side="lay"))

    assert back.event_id == lay.event_id == "matchbook:event-1"
    assert back.market_id == lay.market_id == "matchbook:market-1"
    assert back.selection_id == lay.selection_id == "matchbook:runner-1"
    assert back.exchange_side == "back"
    assert lay.exchange_side == "lay"
    assert back.quote_key != lay.quote_key
    assert back.dedupe_key != lay.dedupe_key
