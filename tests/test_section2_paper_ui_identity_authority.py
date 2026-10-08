from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.ui_model import ticket_lines


PLACED_AT = "2026-10-07T12:00:00+00:00"


def _book_with_ticket() -> tuple[PaperBook, TicketLeg]:
    book = PaperBook("100")
    leg = TicketLeg(
        event_id="event-a",
        market_id="winner",
        selection_id="home",
        locked_odds=Decimal("2.00"),
        sport="table_tennis",
    )
    book.open_ticket((leg,), "10", placed_at=PLACED_AT)
    return book, leg


def test_opening_authority_detects_in_place_ticket_leg_identity_mutation() -> None:
    book, caller_owned_leg = _book_with_ticket()

    # Frozen DTOs are still mutable through object.__setattr__. The opening
    # authority must retain scalar identity facts rather than this object reference.
    object.__setattr__(caller_owned_leg, "event_id", "event-b")

    with pytest.raises(
        ValueError,
        match="ticket opening economic identity changed after admission",
    ):
        _ = book.committed_stake


def test_ticket_lines_fail_closed_before_rendering_mutated_leg_identity() -> None:
    book, caller_owned_leg = _book_with_ticket()
    object.__setattr__(caller_owned_leg, "selection_id", "away")

    with pytest.raises(
        ValueError,
        match="ticket opening economic identity changed after admission",
    ):
        ticket_lines(SimpleNamespace(book=book))


def test_ticket_lines_preserve_valid_canonical_ticket_identity() -> None:
    book, _leg = _book_with_ticket()

    lines = ticket_lines(SimpleNamespace(book=book))

    assert len(lines) == 1
    assert "event-a/winner/home@2.00" in lines[0]
