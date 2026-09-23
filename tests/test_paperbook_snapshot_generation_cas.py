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


def test_snapshot_publication_lock_fails_closed_for_competing_writer_and_reader(
    tmp_path,
    monkeypatch,
) -> None:
    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"

    initial = PaperBook("100")
    initial.save(path)
    competing = PaperBook.load(path)

    import autosport.paper as paper_module

    witness_path = paper_module._snapshot_witness_path(path)
    lock_fd = paper_module._acquire_snapshot_publication_lock(witness_path)
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
        paper_module._release_snapshot_publication_lock(lock_fd)

    restored = PaperBook.load(path)
    assert restored.balance == Decimal("100")
    assert restored.tickets == {}


def test_load_recovers_replaced_snapshot_after_commit_publication_interruption(
    tmp_path,
    monkeypatch,
) -> None:
    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"

    book = PaperBook("100")
    book.save(path)
    ticket = book.open_ticket(
        [_leg("current-selection", "3")],
        "10",
        placed_at=_BASE_TS,
    )

    import autosport.paper as paper_module

    original_append = paper_module._append_snapshot_witness
    fail_commit_once = True

    def _interrupt_commit(*args, **kwargs):
        nonlocal fail_commit_once
        if (
            fail_commit_once
            and kwargs.get("event") == paper_module._PAPER_SNAPSHOT_WITNESS_COMMIT
        ):
            fail_commit_once = False
            raise RuntimeError("injected commit publication interruption")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(
        paper_module,
        "_append_snapshot_witness",
        _interrupt_commit,
    )
    with pytest.raises(
        RuntimeError,
        match="injected commit publication interruption",
    ):
        book.save(path)
    monkeypatch.setattr(
        paper_module,
        "_append_snapshot_witness",
        original_append,
    )

    restored = PaperBook.load(path)
    assert restored.balance == Decimal("90")
    assert tuple(restored.tickets) == (ticket.ticket_id,)

    # Recovery must bind the exact completed generation so normal continuation
    # can advance again without re-baselining the durable authority.
    restored.save(path)
    reread = PaperBook.load(path)
    assert reread.balance == Decimal("90")
    assert tuple(reread.tickets) == (ticket.ticket_id,)
