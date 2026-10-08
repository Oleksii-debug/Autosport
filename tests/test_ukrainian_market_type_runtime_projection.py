from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from autosport.domain import MarketType
from autosport.ui_model import observation_quote_lines


def _observation(market_type: MarketType):
    event = SimpleNamespace(
        sport="table_tennis",
        event_id="event-1",
        market_type=market_type,
        market_id="market-1",
        selection_id="selection-1",
        decimal_odds=Decimal("2.50"),
        source_ts="2026-09-22T07:40:00+00:00",
    )
    return SimpleNamespace(current_quotes=(event,))


@pytest.mark.parametrize("market_type", tuple(MarketType))
def test_live_quote_market_type_is_ukrainian_presentation_not_raw_domain_token(
    market_type: MarketType,
) -> None:
    line = observation_quote_lines(_observation(market_type))[0]

    fields = line.split(" | ")
    assert len(fields) >= 6
    presentation = fields[2]

    assert presentation
    assert presentation.casefold() != market_type.value.casefold()
    assert any("\u0400" <= character <= "\u04ff" for character in presentation)
