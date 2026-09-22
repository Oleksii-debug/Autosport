from decimal import Decimal

import pytest

from autosport.domain import MarketEvent, TicketLeg
from autosport.providers import CanonicalNormalizer, ProviderQuote

TS = "2026-09-22T13:00:00+00:00"


def _event(*, exchange_side=None, sport=None, selection_id="selection"):
    return MarketEvent(
        event_id="event",
        market_id="market",
        selection_id=selection_id,
        decimal_odds=Decimal("2.5"),
        observed_ts=TS,
        source_id="source",
        sequence=7,
        ingest_ts=TS,
        sport=sport,
        exchange_side=exchange_side,
    )


def _provider_quote(*, exchange_side=None, sport=None, selection_id="selection"):
    return ProviderQuote(
        provider_event_id="event",
        provider_market_id="market",
        provider_selection_id=selection_id,
        decimal_odds=Decimal("2.5"),
        observed_ts=TS,
        sequence=7,
        sport=sport,
        exchange_side=exchange_side,
    )


def test_legacy_side_less_identity_and_serialization_stay_exact():
    event = _event()
    assert event.quote_key == "event|market|selection"
    assert event.dedupe_key == "source|event|market|selection|7"
    assert "exchange_side" not in event.to_dict()

    restored = MarketEvent.from_dict(event.to_dict())
    assert restored.exchange_side is None
    assert restored.quote_key == event.quote_key
    assert restored.dedupe_key == event.dedupe_key
    assert restored.to_dict() == event.to_dict()

    leg = TicketLeg("event", "market", "selection", Decimal("2.5"))
    assert leg.quote_key == "event|market|selection"


def test_back_lay_and_legacy_quote_identities_do_not_alias():
    legacy = _event()
    back = _event(exchange_side="back")
    lay = _event(exchange_side="lay")

    assert len({legacy.quote_key, back.quote_key, lay.quote_key}) == 3
    assert len({legacy.dedupe_key, back.dedupe_key, lay.dedupe_key}) == 3
    assert back.quote_key.startswith("exchange-v1-")
    assert lay.quote_key.startswith("exchange-v1-")


def test_explicit_side_round_trips_without_strengthening_legacy_payloads():
    for side in ("back", "lay"):
        event = _event(exchange_side=side, sport="football")
        payload = event.to_dict()
        assert payload["exchange_side"] == side
        restored = MarketEvent.from_dict(payload)
        assert restored == event
        assert restored.quote_key == event.quote_key
        assert restored.dedupe_key == event.dedupe_key

    legacy_payload = _event(sport="football").to_dict()
    assert "exchange_side" not in legacy_payload
    assert MarketEvent.from_dict(legacy_payload).exchange_side is None


def test_invalid_exchange_side_fails_closed_at_provider_and_domain_boundaries():
    for invalid in ("BACK", "lay ", "", "bid", 1, True):
        with pytest.raises((TypeError, ValueError)):
            _provider_quote(exchange_side=invalid)
        with pytest.raises(ValueError):
            _event(exchange_side=invalid)


def test_normalizer_preserves_structural_side_without_synthetic_selection_id():
    normalizer = CanonicalNormalizer()
    back = normalizer.normalize("matchbook", _provider_quote(exchange_side="back"))
    lay = normalizer.normalize("matchbook", _provider_quote(exchange_side="lay"))

    assert back.exchange_side == "back"
    assert lay.exchange_side == "lay"
    assert back.selection_id == "matchbook:selection"
    assert lay.selection_id == "matchbook:selection"
    assert back.quote_key != lay.quote_key
    assert back.dedupe_key != lay.dedupe_key


def test_selection_text_that_mentions_back_cannot_alias_structural_side():
    textual = _event(selection_id="selection:back")
    structural = _event(selection_id="selection", exchange_side="back")
    assert textual.quote_key != structural.quote_key
    assert textual.dedupe_key != structural.dedupe_key
