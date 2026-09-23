from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_BASE_TS = "2026-09-23T01:00:00+00:00"


def _leg(selection_id: str, odds: str = "2") -> TicketLeg:
    return TicketLeg(
        "event-1",
        "market-1",
        selection_id,
        Decimal(odds),
        sport="soccer",
        exchange_side="back",
    )


def _bind_authority_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(tmp_path.parent / f"{tmp_path.name}-generation-cas-authority"),
    )


def test_stale_bound_book_cannot_overwrite_newer_committed_generation(
    tmp_path,
    monkeypatch,
) -> None:
    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"

    initial = PaperBook("100")
    initial.save(path)

    stale = PaperBook.load(path)
    current = PaperBook.load(path)

    stale.open_ticket(
        [_leg("stale-selection")],
        "7",
        placed_at=_BASE_TS,
    )
    current_ticket = current.open_ticket(
        [_leg("current-selection", "3")],
        "10",
        placed_at="2026-09-23T01:00:01+00:00",
    )
    current.save(path)
    durable_after_current = path.read_bytes()

    with pytest.raises(
        ValueError,
        match="snapshot authority is stale; reload current durable snapshot",
    ):
        stale.save(path)

    assert path.read_bytes() == durable_after_current
    reloaded = PaperBook.load(path)
    assert reloaded.balance == Decimal("90")
    assert tuple(reloaded.tickets) == (current_ticket.ticket_id,)


def test_stale_bound_book_cannot_mutate_after_newer_generation(
    tmp_path,
    monkeypatch,
) -> None:
    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"

    initial = PaperBook("100")
    initial.save(path)

    stale = PaperBook.load(path)
    current = PaperBook.load(path)

    current.open_ticket(
        [_leg("current-selection")],
        "10",
        placed_at=_BASE_TS,
    )
    current.save(path)

    balance_before = stale.balance
    tickets_before = tuple(stale.tickets)
    lifecycle_before = tuple(stale._lifecycle)

    with pytest.raises(
        ValueError,
        match="snapshot authority is stale; reload current durable snapshot",
    ):
        stale.open_ticket(
            [_leg("stale-selection")],
            "5",
            placed_at="2026-09-23T01:00:02+00:00",
        )

    assert stale.balance == balance_before
    assert tuple(stale.tickets) == tickets_before
    assert tuple(stale._lifecycle) == lifecycle_before
