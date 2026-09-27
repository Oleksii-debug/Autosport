from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_TS = "2026-09-27T00:45:00+00:00"


def _leg() -> TicketLeg:
    return TicketLeg(
        "event-structural-load",
        "market-structural-load",
        "selection-structural-load",
        Decimal("2.5"),
        sport="soccer",
        exchange_side="back",
    )


def test_normal_path_load_is_structural_not_positive_opening_authority(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    source = PaperBook("100")
    ticket = source.open_ticket([_leg()], "10", placed_at=_TS)
    source.save(path)

    loaded = PaperBook.load(path)

    # Structural facts remain inspectable for reconciliation/recovery decisions.
    assert loaded.balance == Decimal("90")
    assert loaded.tickets[ticket.ticket_id].stake == Decimal("10")

    # But caller-editable persisted bytes cannot authorize bankroll/exposure use by
    # merely surviving the parser. Positive authority must come from an independent
    # product-owned durable source.
    with pytest.raises(
        ValueError,
        match="byte-loaded snapshot lacks product-issued opening authority",
    ):
        _ = loaded.committed_stake

    with pytest.raises(
        ValueError,
        match="byte-loaded snapshot lacks product-issued opening authority",
    ):
        loaded.open_ticket([_leg()], "1", placed_at=_TS)

    copied = tmp_path / "copied-paper-book.json"
    with pytest.raises(
        ValueError,
        match="byte-loaded snapshot lacks product-issued opening authority",
    ):
        loaded.save(copied)
    assert not copied.exists()


def test_normal_path_load_cannot_settle_without_independent_restart_authority(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    source = PaperBook("100")
    ticket = source.open_ticket([_leg()], "10", placed_at=_TS)
    source.save(path)

    loaded = PaperBook.load(path)
    loaded_ticket = loaded.tickets[ticket.ticket_id]
    with pytest.raises(
        ValueError,
        match="byte-loaded snapshot lacks product-issued opening authority",
    ):
        loaded.settle(
            ticket.ticket_id,
            {loaded_ticket.legs[0].quote_key},
            settled_at=_TS,
        )

    assert loaded.balance == Decimal("90")
    assert loaded_ticket.status.value == "open"
    assert loaded_ticket.payout == Decimal("0")
