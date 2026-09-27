from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.domain import TicketLeg, TicketStatus
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy


_TS = "2026-09-27T20:00:00+00:00"


def _leg(selection_id: str) -> TicketLeg:
    return TicketLeg(
        "risk-private-event",
        "risk-private-market",
        selection_id,
        Decimal("2"),
        sport="soccer",
        exchange_side="back",
    )


def _assert_risk_rejected(book: PaperBook) -> None:
    assert PaperRiskPolicy._book_state(book) is None
    assert PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book) is None
    assert PaperRiskPolicy._historical_risk_metrics(book) is None
    assert PaperRiskPolicy._shadow_book_for_allocation(book) is None
    decision = PaperRiskPolicy().evaluate(book, Decimal("1"))
    assert decision.allowed is False
    assert decision.reason == "virtual bankroll private economic authority is invalid"


def test_risk_rejects_structurally_coherent_opening_economic_rewrite() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg("opening")], "10", placed_at=_TS)

    # Rewrite the visible opening economics into another structurally coherent history.
    # Structural replay alone sees a valid 1-unit debit from 100 to 99, but the private
    # product-issued opening commitment still binds the original 10-unit stake.
    ticket.stake = Decimal("1")
    book.balance = Decimal("99")
    PaperBook._validate_loaded_state(book)

    with pytest.raises(ValueError, match="opening economic identity changed"):
        _ = book.committed_stake

    _assert_risk_rejected(book)


def test_risk_rejects_structurally_coherent_causal_settlement_rewrite() -> None:
    book = PaperBook("100")
    leg = _leg("causal")
    ticket = book.open_ticket([leg], "10", placed_at=_TS)
    settled_at = "2026-09-27T20:01:00+00:00"
    book.settle(ticket.ticket_id, {leg.quote_key}, settled_at=settled_at)

    # Rewrite every caller-visible settlement fact from WIN to LOSS. The resulting
    # lifecycle and balance are internally coherent but conflict with the private
    # product-issued causal history captured by settle().
    ticket.status = TicketStatus.LOST
    ticket.payout = Decimal("0")
    ticket.settled_at = settled_at
    book.balance = Decimal("90")
    book._lifecycle[-1] = ("settle", ticket.ticket_id, (), ())
    PaperBook._validate_loaded_state(book)

    with pytest.raises(ValueError, match="causal history changed"):
        _ = book.committed_stake

    _assert_risk_rejected(book)
