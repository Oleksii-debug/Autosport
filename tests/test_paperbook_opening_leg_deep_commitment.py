from decimal import Decimal

import pytest

from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_TS = "2026-10-05T00:00:00+00:00"


def _leg(*, odds: str = "2.00", sport: str = "soccer") -> TicketLeg:
    return TicketLeg(
        "event-1",
        "market-1",
        "selection-1",
        Decimal(odds),
        sport=sport,
        exchange_side="back",
    )


def test_in_place_locked_odds_mutation_cannot_move_opening_authority() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)
    leg = ticket.legs[0]
    original_quote_key = leg.quote_key
    object.__setattr__(leg, "locked_odds", Decimal("100"))

    balance_before = book.balance
    lifecycle_before = tuple(book._lifecycle)
    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        book.settle(ticket.ticket_id, {original_quote_key}, settled_at=_TS)

    assert book.balance == balance_before
    assert tuple(book._lifecycle) == lifecycle_before
    assert ticket.status.value == "open"
    assert ticket.payout == Decimal("0")


def test_in_place_leg_mutation_cannot_replace_durable_snapshot(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)
    book.save(path)
    durable_before = path.read_bytes()

    object.__setattr__(ticket.legs[0], "locked_odds", Decimal("100"))

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        book.save(path)

    assert path.read_bytes() == durable_before


def test_trusted_restart_installs_detached_leg_opening_authority(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    book.save(path)

    restored = PaperBook.load(path)
    ticket = next(iter(restored.tickets.values()))
    object.__setattr__(ticket.legs[0], "sport", "tennis")

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        _ = restored.committed_stake


def test_unchanged_leg_remains_authorized_after_trusted_restart(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    book.save(path)

    restored = PaperBook.load(path)

    assert restored.committed_stake == Decimal("10")


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("event_id", "event-2"),
        ("market_id", "market-2"),
        ("selection_id", "selection-2"),
        ("locked_odds", Decimal("3.00")),
        ("sport", "tennis"),
        ("exchange_side", None),
        ("market_semantics_id", "soccer:h2h:v2"),
    ],
)
def test_each_canonical_leg_identity_mutation_is_rejected(
    field: str,
    replacement: object,
) -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)
    object.__setattr__(ticket.legs[0], field, replacement)

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        _ = book.committed_stake


def test_leg_tuple_reordering_cannot_move_opening_authority() -> None:
    book = PaperBook("100")
    first = _leg()
    second = TicketLeg(
        "event-2",
        "market-2",
        "selection-2",
        Decimal("2.50"),
        sport="tennis",
        exchange_side="back",
    )
    ticket = book.open_ticket([first, second], "10", placed_at=_TS)
    ticket.legs = tuple(reversed(ticket.legs))

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        _ = book.committed_stake


def test_leg_tuple_replacement_with_equal_quote_but_changed_odds_is_rejected() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)
    original = ticket.legs[0]
    ticket.legs = (
        TicketLeg(
            original.event_id,
            original.market_id,
            original.selection_id,
            Decimal("2.25"),
            sport=original.sport,
            exchange_side=original.exchange_side,
        ),
    )

    assert ticket.legs[0].quote_key == original.quote_key
    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        _ = book.committed_stake
