from __future__ import annotations

from decimal import Decimal

import pytest

import autosport._paperbook_preload_authority_guard as guard
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


def test_stale_bound_book_cannot_read_committed_stake_after_newer_generation(
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

    with pytest.raises(
        ValueError,
        match="snapshot authority is stale; reload current durable snapshot",
    ):
        _ = stale.committed_stake


def test_stale_bound_book_cannot_settle_after_newer_generation(
    tmp_path,
    monkeypatch,
) -> None:
    """Stale generation rejection happens before payout/balance mutation."""

    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"

    initial = PaperBook("100")
    original_ticket = initial.open_ticket(
        [_leg("shared-selection", "2")],
        "10",
        placed_at=_BASE_TS,
    )
    initial.save(path)

    stale = PaperBook.load(path)
    current = PaperBook.load(path)
    current.open_ticket(
        [_leg("newer-selection", "3")],
        "5",
        placed_at="2026-09-23T01:00:01+00:00",
    )
    current.save(path)
    durable_after_current = path.read_bytes()

    stale_ticket = stale.tickets[original_ticket.ticket_id]
    winning_quote_key = stale_ticket.legs[0].quote_key
    balance_before = stale.balance
    payout_before = stale_ticket.payout
    status_before = stale_ticket.status
    lifecycle_before = tuple(stale._lifecycle)
    settlement_times_before = dict(stale._settlement_times)

    with pytest.raises(
        ValueError,
        match="snapshot authority is stale; reload current durable snapshot",
    ):
        stale.settle(
            stale_ticket.ticket_id,
            {winning_quote_key},
            settled_at="2026-09-23T01:00:02+00:00",
        )

    assert stale.balance == balance_before
    assert stale_ticket.payout == payout_before
    assert stale_ticket.status is status_before
    assert tuple(stale._lifecycle) == lifecycle_before
    assert stale._settlement_times == settlement_times_before
    assert path.read_bytes() == durable_after_current


def test_snapshot_publication_lock_fails_closed_for_competing_writer_and_reader(
    tmp_path,
    monkeypatch,
) -> None:
    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"

    initial = PaperBook("100")
    initial.save(path)
    competing = PaperBook.load(path)

    witness_path = guard._witness_path(path)
    publication_lock = guard._acquire_snapshot_publication_lock(witness_path)
    try:
        with pytest.raises(
            ValueError,
            match="snapshot publication lock is held by another writer",
        ):
            competing.save(path)

        with pytest.raises(
            ValueError,
            match="snapshot publication lock is held by another writer",
        ):
            PaperBook.load(path)
    finally:
        guard._release_snapshot_publication_lock(publication_lock)

    restored = PaperBook.load(path)
    assert restored.balance == Decimal("100")
    assert restored.tickets == {}
