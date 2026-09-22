from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from autosport.domain import MarketEvent, MarketType, PaperTicket, TicketLeg, TicketStatus
from autosport.ui_model import observation_quote_lines, ticket_lines


def _event(sport: str) -> MarketEvent:
    return MarketEvent(
        event_id="same-event",
        market_id="same-market",
        selection_id="same-selection",
        decimal_odds=Decimal("2.50"),
        observed_ts="2026-09-22T05:30:00+00:00",
        source_id="test:multisport",
        sequence=1,
        market_type=MarketType.WINNER,
        source_ts="2026-09-22T05:29:59+00:00",
        ingest_ts="2026-09-22T05:30:00+00:00",
        sport=sport,
        market_semantics_id="match-winner",
        provider_source_class="test-only-engineering-conformance",
    )


def _ticket(sport: str) -> PaperTicket:
    return PaperTicket(
        ticket_id="same-ticket",
        stake=Decimal("10"),
        legs=(
            TicketLeg(
                event_id="same-event",
                market_id="same-market",
                selection_id="same-selection",
                locked_odds=Decimal("2.50"),
                sport=sport,
            ),
        ),
        placed_at="2026-09-22T05:31:00+00:00",
        status=TicketStatus.OPEN,
    )


def test_live_quote_text_distinguishes_otherwise_identical_sports() -> None:
    """The keyboard/NVDA live list must not collapse cross-sport lookalikes."""

    table_tennis_result = SimpleNamespace(current_quotes=(_event("table_tennis"),))
    soccer_result = SimpleNamespace(current_quotes=(_event("soccer"),))

    table_tennis_line = observation_quote_lines(table_tennis_result)[0]
    soccer_line = observation_quote_lines(soccer_result)[0]

    assert table_tennis_line != soccer_line
    assert table_tennis_line == observation_quote_lines(table_tennis_result)[0]
    assert soccer_line == observation_quote_lines(soccer_result)[0]


def test_ticket_text_distinguishes_otherwise_identical_sports() -> None:
    """The keyboard/NVDA ticket list must preserve canonical sport context."""

    table_tennis_session = SimpleNamespace(
        book=SimpleNamespace(tickets={"same": _ticket("table_tennis")})
    )
    soccer_session = SimpleNamespace(
        book=SimpleNamespace(tickets={"same": _ticket("soccer")})
    )

    table_tennis_line = ticket_lines(table_tennis_session)[0]
    soccer_line = ticket_lines(soccer_session)[0]

    assert table_tennis_line != soccer_line
    assert table_tennis_line == ticket_lines(table_tennis_session)[0]
    assert soccer_line == ticket_lines(soccer_session)[0]
