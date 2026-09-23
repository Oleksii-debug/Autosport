from __future__ import annotations

from decimal import Decimal
import threading

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


def test_save_and_open_ticket_are_linearized_on_same_paperbook(
    tmp_path,
    monkeypatch,
) -> None:
    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"

    book = PaperBook("100")
    book.save(path)

    import autosport.paper as paper_module

    save_entered_lifecycle = threading.Event()
    allow_save_to_finish = threading.Event()
    open_started = threading.Event()
    open_finished = threading.Event()
    errors: list[BaseException] = []
    opened_ticket_ids: list[str] = []

    original_lifecycle_to_json = PaperBook._lifecycle_to_json

    def _blocked_lifecycle_to_json(self):
        save_entered_lifecycle.set()
        if not allow_save_to_finish.wait(timeout=5):
            raise AssertionError("timed out waiting to release save serialization")
        return original_lifecycle_to_json(self)

    monkeypatch.setattr(
        PaperBook,
        "_lifecycle_to_json",
        _blocked_lifecycle_to_json,
    )

    def _save_worker() -> None:
        try:
            book.save(path)
        except BaseException as exc:
            errors.append(exc)

    save_thread = threading.Thread(target=_save_worker, daemon=True)
    save_thread.start()
    assert save_entered_lifecycle.wait(timeout=5)

    # save() must own the same per-book state lock used by open_ticket().
    state_lock = paper_module._paperbook_state_lock(book)
    assert state_lock.acquire(blocking=False) is False

    def _open_worker() -> None:
        open_started.set()
        try:
            ticket = book.open_ticket(
                [_leg("concurrent-selection")],
                "10",
                placed_at=_BASE_TS,
            )
            opened_ticket_ids.append(ticket.ticket_id)
        except BaseException as exc:
            errors.append(exc)
        finally:
            open_finished.set()

    open_thread = threading.Thread(target=_open_worker, daemon=True)
    open_thread.start()
    assert open_started.wait(timeout=5)
    assert not open_finished.wait(timeout=0.1)

    allow_save_to_finish.set()
    save_thread.join(timeout=5)
    open_thread.join(timeout=5)
    assert not save_thread.is_alive()
    assert not open_thread.is_alive()
    assert errors == []
    assert len(opened_ticket_ids) == 1

    # The first durable snapshot linearized before the open transition.
    monkeypatch.setattr(
        PaperBook,
        "_lifecycle_to_json",
        original_lifecycle_to_json,
    )
    before_open = PaperBook.load(path)
    assert before_open.balance == Decimal("100")
    assert before_open.tickets == {}

    # In-memory state then contains the complete open transition, and a later
    # save persists that whole epoch rather than a balance/ticket/lifecycle mix.
    assert book.balance == Decimal("90")
    assert tuple(book.tickets) == (opened_ticket_ids[0],)
    book.save(path)
    after_open = PaperBook.load(path)
    assert after_open.balance == Decimal("90")
    assert tuple(after_open.tickets) == (opened_ticket_ids[0],)


def test_private_opening_authority_rejects_coherent_visible_stake_witness_rewrite(
    tmp_path,
    monkeypatch,
) -> None:
    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"

    book = PaperBook("100")
    ticket = book.open_ticket(
        [_leg("selection-1")],
        "10",
        placed_at=_BASE_TS,
    )
    book.save(path)
    durable_before = path.read_bytes()

    # Rewriting both caller-visible values defeats the old field-to-field guard,
    # and balance=80 keeps lifecycle replay coherent with the forged stake=20.
    ticket.stake = Decimal("20")
    ticket._opening_stake = Decimal("20")
    book.balance = Decimal("80")

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        book.save(path)

    assert path.read_bytes() == durable_before


def test_verified_load_private_authority_rejects_visible_leg_witness_rewrite(
    tmp_path,
    monkeypatch,
) -> None:
    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"

    original = PaperBook("100")
    original_ticket = original.open_ticket(
        [_leg("selection-1", "2")],
        "10",
        placed_at=_BASE_TS,
    )
    original.save(path)
    durable_before = path.read_bytes()

    restored = PaperBook.load(path)
    ticket = restored.tickets[original_ticket.ticket_id]
    inflated = _leg("selection-1", "100")
    assert inflated.quote_key == ticket.legs[0].quote_key

    # A verified load must install product-private opening authority from the
    # externally witnessed bytes before caller-visible _opening_* can be trusted.
    ticket.legs = (inflated,)
    ticket._opening_legs = (inflated,)

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        restored.save(path)

    assert path.read_bytes() == durable_before


def test_private_opening_authority_rejects_settlement_after_visible_leg_witness_rewrite(
    tmp_path,
    monkeypatch,
) -> None:
    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"

    book = PaperBook("100")
    ticket = book.open_ticket(
        [_leg("selection-1", "2")],
        "10",
        placed_at=_BASE_TS,
    )
    book.save(path)

    inflated = _leg("selection-1", "100")
    assert inflated.quote_key == ticket.legs[0].quote_key
    ticket.legs = (inflated,)
    ticket._opening_legs = (inflated,)

    balance_before = book.balance
    lifecycle_before = tuple(book._lifecycle)
    status_before = ticket.status
    payout_before = ticket.payout
    settled_at_before = ticket.settled_at

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        book.settle(
            ticket.ticket_id,
            {inflated.quote_key},
            settled_at="2026-09-23T02:00:00+00:00",
        )

    assert book.balance == balance_before
    assert tuple(book._lifecycle) == lifecycle_before
    assert ticket.status is status_before
    assert ticket.payout == payout_before
    assert ticket.settled_at == settled_at_before


def test_exact_serialized_candidate_rejects_post_validation_opening_rewrite(
    tmp_path,
    monkeypatch,
) -> None:
    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"

    book = PaperBook("100")
    ticket = book.open_ticket(
        [_leg("selection-1", "2")],
        "10",
        placed_at=_BASE_TS,
    )
    book.save(path)
    durable_before = path.read_bytes()

    import autosport.paper as paper_module

    original_binding = paper_module._snapshot_authority_binding
    calls = 0

    def _mutate_after_initial_validation(target):
        nonlocal calls
        binding = original_binding(target)
        if target is book:
            calls += 1
            # save() calls the binding once in its opening authority check, then
            # validates the whole live object, then calls it again immediately
            # before raw snapshot collection. Mutate on that second call so the
            # initial private-authority validation has already passed.
            if calls == 2:
                ticket.stake = Decimal("20")
                ticket._opening_stake = Decimal("20")
                book.balance = Decimal("80")
        return binding

    monkeypatch.setattr(
        paper_module,
        "_snapshot_authority_binding",
        _mutate_after_initial_validation,
    )

    with pytest.raises(
        ValueError,
        match=(
            "serialized candidate opening economic identity differs "
            "from product-issued authority"
        ),
    ):
        book.save(path)

    assert calls >= 2
    assert path.read_bytes() == durable_before


def test_exact_serialized_candidate_rejects_post_validation_ticket_history_deletion(
    tmp_path,
    monkeypatch,
) -> None:
    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"

    book = PaperBook("100")
    book.open_ticket(
        [_leg("selection-1", "2")],
        "10",
        placed_at=_BASE_TS,
    )
    book.save(path)
    durable_before = path.read_bytes()

    import autosport.paper as paper_module

    original_binding = paper_module._snapshot_authority_binding
    calls = 0

    def _erase_after_initial_validation(target):
        nonlocal calls
        binding = original_binding(target)
        if target is book:
            calls += 1
            if calls == 2:
                # This is a structurally coherent empty-book rewrite: without
                # the private ticket-set authority the serialized candidate can
                # replay to bankroll 100 and look self-consistent.
                book.tickets.clear()
                book._lifecycle.clear()
                book.balance = Decimal("100")
        return binding

    monkeypatch.setattr(
        paper_module,
        "_snapshot_authority_binding",
        _erase_after_initial_validation,
    )

    with pytest.raises(
        ValueError,
        match=(
            "serialized candidate ticket set differs from "
            "product-issued opening authority"
        ),
    ):
        book.save(path)

    assert calls >= 2
    assert path.read_bytes() == durable_before
