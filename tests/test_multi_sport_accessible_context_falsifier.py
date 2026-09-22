from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from autosport.domain import MarketEvent, MarketType, TicketLeg, TicketStatus
from autosport.ui_model import observation_quote_lines, ticket_lines


def _ticket_line(*, sport: str) -> str:
    leg = TicketLeg(
        event_id="same-event",
        market_id="same-market",
        selection_id="same-selection",
        locked_odds=Decimal("2.00"),
        sport=sport,
    )
    ticket = SimpleNamespace(
        status=TicketStatus.OPEN,
        stake=Decimal("10.00"),
        combined_odds=Decimal("2.00"),
        payout=Decimal("0"),
        legs=(leg,),
    )
    session = SimpleNamespace(
        book=SimpleNamespace(tickets={"same-ticket": ticket})
    )
    return ticket_lines(session)[0]


def _observation_quote_line(*, sport: str) -> str:
    event = MarketEvent(
        event_id="same-event",
        market_id="same-market",
        selection_id="same-selection",
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-22T00:00:00Z",
        source_id="test-source",
        sequence=1,
        market_type=MarketType.WINNER,
        source_ts="2026-09-22T00:00:00Z",
        sport=sport,
        market_semantics_id="match-winner.v1",
        provider_source_class="test-only",
    )
    return observation_quote_lines(
        SimpleNamespace(current_quotes=(event,))
    )[0]


def test_ticket_operator_text_distinguishes_sport_accessibly() -> None:
    table_tennis = _ticket_line(sport="table_tennis")
    second_sport = _ticket_line(sport="soccer")

    assert table_tennis != second_sport
    assert "спорт" in table_tennis.lower()
    assert "спорт" in second_sport.lower()
    assert _ticket_line(sport="table_tennis") == table_tennis
    assert _ticket_line(sport="soccer") == second_sport


def test_observation_operator_text_distinguishes_sport_accessibly() -> None:
    table_tennis = _observation_quote_line(sport="table_tennis")
    second_sport = _observation_quote_line(sport="soccer")

    assert table_tennis != second_sport
    assert "спорт" in table_tennis.lower()
    assert "спорт" in second_sport.lower()
    assert _observation_quote_line(sport="table_tennis") == table_tennis
    assert _observation_quote_line(sport="soccer") == second_sport
