from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.domain import TicketLeg, TicketStatus
from autosport.paper import PaperBook


_PLACED_AT = "2026-09-23T01:00:00+00:00"
_SETTLED_AT = "2026-09-23T01:01:00+00:00"


def _leg(
    side: str | None,
    *,
    selection_id: str = "selection-1",
    odds: str = "2.5",
) -> TicketLeg:
    return TicketLeg(
        "event-1",
        "market-1",
        selection_id,
        Decimal(odds),
        sport="soccer",
        exchange_side=side,
    )


def _assert_open_state_unchanged(
    book: PaperBook,
    ticket_id: str,
    *,
    balance: Decimal,
    lifecycle: tuple[tuple[object, ...], ...],
) -> None:
    ticket = book.tickets[ticket_id]
    assert book.balance == balance
    assert ticket.status is TicketStatus.OPEN
    assert ticket.payout == Decimal("0")
    assert ticket.settled_at is None
    assert tuple(book._lifecycle) == lifecycle
    assert ticket_id not in book._settlement_times


def test_settle_rejects_post_open_stake_rewrite_before_payout() -> None:
    book = PaperBook("100")
    original = _leg("back", odds="2")
    ticket = book.open_ticket([original], "10", placed_at=_PLACED_AT)
    balance_before = book.balance
    lifecycle_before = tuple(book._lifecycle)

    ticket.stake = Decimal("20")

    with pytest.raises(ValueError, match="opening economic identity changed"):
        book.settle(
            ticket.ticket_id,
            {original.quote_key},
            settled_at=_SETTLED_AT,
        )

    _assert_open_state_unchanged(
        book,
        ticket.ticket_id,
        balance=balance_before,
        lifecycle=lifecycle_before,
    )


def test_settle_rejects_post_open_back_odds_replacement_before_payout() -> None:
    book = PaperBook("100")
    original = _leg("back", odds="2")
    ticket = book.open_ticket([original], "10", placed_at=_PLACED_AT)
    balance_before = book.balance
    lifecycle_before = tuple(book._lifecycle)

    inflated = _leg("back", odds="100")
    assert inflated.quote_key == original.quote_key
    ticket.legs = (inflated,)

    with pytest.raises(ValueError, match="opening economic identity changed"):
        book.settle(
            ticket.ticket_id,
            {inflated.quote_key},
            settled_at=_SETTLED_AT,
        )

    _assert_open_state_unchanged(
        book,
        ticket.ticket_id,
        balance=balance_before,
        lifecycle=lifecycle_before,
    )


def test_save_rejects_post_open_valid_back_odds_replacement(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    ticket = book.open_ticket([_leg("back", odds="2")], "10", placed_at=_PLACED_AT)
    ticket.legs = (_leg("back", odds="100"),)

    with pytest.raises(ValueError, match="opening economic identity changed"):
        book.save(path)

    assert not path.exists()


def test_settle_revalidates_mutated_ticket_side_before_economic_mutation() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket(
        [_leg("back")],
        "10",
        placed_at=_PLACED_AT,
    )
    balance_before = book.balance
    lifecycle_before = tuple(book._lifecycle)

    lay = _leg("lay")
    ticket.legs = (lay,)

    with pytest.raises(ValueError, match="LAY materialization is unsupported"):
        book.settle(
            ticket.ticket_id,
            {lay.quote_key},
            settled_at=_SETTLED_AT,
        )

    _assert_open_state_unchanged(
        book,
        ticket.ticket_id,
        balance=balance_before,
        lifecycle=lifecycle_before,
    )


def test_settle_rejects_lay_in_mutated_multi_leg_ticket_before_payout() -> None:
    book = PaperBook("100")
    first = _leg("back", selection_id="selection-1", odds="2")
    second = _leg("back", selection_id="selection-2", odds="3")
    ticket = book.open_ticket(
        [first, second],
        "10",
        placed_at=_PLACED_AT,
    )
    balance_before = book.balance
    lifecycle_before = tuple(book._lifecycle)

    lay_second = _leg("lay", selection_id="selection-2", odds="3")
    ticket.legs = (first, lay_second)

    with pytest.raises(ValueError, match="LAY materialization is unsupported"):
        book.settle(
            ticket.ticket_id,
            {first.quote_key, lay_second.quote_key},
            settled_at=_SETTLED_AT,
        )

    _assert_open_state_unchanged(
        book,
        ticket.ticket_id,
        balance=balance_before,
        lifecycle=lifecycle_before,
    )
