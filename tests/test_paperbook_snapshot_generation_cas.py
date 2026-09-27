from __future__ import annotations

from decimal import Decimal

import pytest

import autosport._paperbook_preload_authority_guard as guard
from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy


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


def test_stale_bound_book_cannot_authorize_risk_state_after_newer_generation(
    tmp_path,
    monkeypatch,
) -> None:
    """Risk/read validation must consume the same durable generation authority."""

    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"

    initial = PaperBook("100")
    initial.open_ticket(
        [_leg("initial-selection")],
        "10",
        placed_at=_BASE_TS,
    )
    initial.save(path)

    stale = PaperBook.load(path)
    current = PaperBook.load(path)
    current.open_ticket(
        [_leg("newer-selection")],
        "5",
        placed_at="2026-09-23T01:00:01+00:00",
    )
    current.save(path)

    with pytest.raises(
        ValueError,
        match="snapshot authority is stale; reload current durable snapshot",
    ):
        PaperBook._validate_loaded_state(stale)

    assert PaperRiskPolicy._book_state(stale) is None
    assert PaperRiskPolicy.risk_of_ruin_portfolio_sha256(stale) is None
    assert PaperRiskPolicy._historical_risk_metrics(stale) is None
    assert PaperRiskPolicy._shadow_book_for_allocation(stale) is None

    live = PaperBook.load(path)
    assert PaperRiskPolicy._book_state(live) is not None
    assert PaperRiskPolicy.risk_of_ruin_portfolio_sha256(live) is not None
    assert PaperRiskPolicy._historical_risk_metrics(live) is not None
    assert PaperRiskPolicy._shadow_book_for_allocation(live) is not None


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
    initial.open_ticket(
        [_leg("locked-read-selection")],
        "10",
        placed_at=_BASE_TS,
    )
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

        with pytest.raises(
            ValueError,
            match="snapshot publication lock is held by another writer",
        ):
            _ = competing.committed_stake
    finally:
        guard._release_snapshot_publication_lock(publication_lock)

    restored = PaperBook.load(path)
    assert restored.balance == Decimal("90")
    assert restored.committed_stake == Decimal("10")


def test_load_recovers_replaced_snapshot_after_commit_publication_interruption(
    tmp_path,
    monkeypatch,
) -> None:
    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"
    candidate_path = tmp_path / "candidate-paper-book.json"

    initial = PaperBook("100")
    initial.save(path)
    current = PaperBook.load(path)
    current_ticket = current.open_ticket(
        [_leg("current-selection", "3")],
        "10",
        placed_at=_BASE_TS,
    )

    # Stage the exact bytes that the canonical serializer would have replaced,
    # bypassing only witness publication so the test can model process death in
    # the narrow post-replace / pre-COMMIT crash window.
    guard._ORIGINAL_SAVE(current, candidate_path)
    candidate_bytes = candidate_path.read_bytes()
    candidate_sha = guard._file_sha256(candidate_path)
    assert candidate_sha is not None

    _records, committed, pending = guard._read_witnesses(path)
    assert committed is not None
    assert pending is None
    generation = committed[0] + 1
    guard._append_witness(
        path,
        event=guard._PREPARE,
        generation=generation,
        snapshot_sha256=candidate_sha,
    )
    path.write_bytes(candidate_bytes)

    recovered = PaperBook.load(path)
    assert recovered.balance == Decimal("90")
    assert tuple(recovered.tickets) == (current_ticket.ticket_id,)

    _records, committed_after, pending_after = guard._read_witnesses(path)
    assert pending_after is None
    assert committed_after == (generation, candidate_sha)

    # Recovery must bind that exact completed generation so ordinary continuation
    # advances from it instead of silently re-baselining the durable authority.
    recovered.save(path)
    _records, committed_final, pending_final = guard._read_witnesses(path)
    assert pending_final is None
    assert committed_final is not None
    assert committed_final[0] > generation


def test_authenticated_book_can_stage_to_fresh_path_and_promote_canonically(
    tmp_path,
    monkeypatch,
) -> None:
    """RunTransaction-style staging preserves one witnessed BASE -> NEW lineage."""

    _bind_authority_root(tmp_path, monkeypatch)
    canonical = tmp_path / "paper_book.json"
    staged = tmp_path / ".run-transactions" / "run-1" / "paper_book.next.json"
    staged.parent.mkdir(parents=True)

    initial = PaperBook("100")
    initial.save(canonical)
    base_sha = guard._file_sha256(canonical)
    assert base_sha is not None

    working = PaperBook.load(canonical)
    ticket = working.open_ticket(
        [_leg("staged-selection", "3")],
        "10",
        placed_at=_BASE_TS,
    )
    working.save(staged)
    new_sha = guard._file_sha256(staged)
    assert new_sha is not None
    assert new_sha != base_sha

    # Staging must not mutate canonical BASE authority.
    canonical_before = PaperBook.load(canonical)
    assert canonical_before.balance == Decimal("100")
    assert not canonical_before.tickets

    staged_before = PaperBook.load(staged)
    assert staged_before.balance == Decimal("90")
    assert tuple(staged_before.tickets) == (ticket.ticket_id,)

    guard._promote_verified_snapshot(
        staged,
        canonical,
        expected_base_sha256=base_sha,
        expected_new_sha256=new_sha,
    )

    promoted = PaperBook.load(canonical)
    assert promoted.balance == Decimal("90")
    assert tuple(promoted.tickets) == (ticket.ticket_id,)
    assert guard._file_sha256(canonical) == new_sha

    _records, committed, pending = guard._read_witnesses(canonical)
    assert pending is None
    assert committed is not None
    assert committed[1] == new_sha


def test_canonical_promotion_rejects_tampered_staged_bytes(
    tmp_path,
    monkeypatch,
) -> None:
    _bind_authority_root(tmp_path, monkeypatch)
    canonical = tmp_path / "paper_book.json"
    staged = tmp_path / ".run-transactions" / "run-2" / "paper_book.next.json"
    staged.parent.mkdir(parents=True)

    initial = PaperBook("100")
    initial.save(canonical)
    base_sha = guard._file_sha256(canonical)
    assert base_sha is not None
    canonical_bytes = canonical.read_bytes()

    working = PaperBook.load(canonical)
    working.open_ticket(
        [_leg("tamper-selection")],
        "10",
        placed_at=_BASE_TS,
    )
    working.save(staged)
    new_sha = guard._file_sha256(staged)
    assert new_sha is not None

    staged.write_bytes(staged.read_bytes() + b"\n")

    with pytest.raises(
        ValueError,
        match="staged promotion snapshot hash mismatch",
    ):
        guard._promote_verified_snapshot(
            staged,
            canonical,
            expected_base_sha256=base_sha,
            expected_new_sha256=new_sha,
        )

    assert canonical.read_bytes() == canonical_bytes
    restored = PaperBook.load(canonical)
    assert restored.balance == Decimal("100")
    assert not restored.tickets
