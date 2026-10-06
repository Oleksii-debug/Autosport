from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.domain import MarketEvent
from autosport.opportunity import OpportunityContractError, QuoteRef


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
