from __future__ import annotations

from decimal import Decimal

import pytest

import autosport._paperbook_preload_authority_guard as guard
from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_BASE_TS = "2026-10-03T12:00:00+00:00"


def _bind_authority_root(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(tmp_path.parent / f"{tmp_path.name}-paperbook-generation-authority"),
    )


def _leg(selection_id: str, odds: str = "2") -> TicketLeg:
    return TicketLeg(
        "event-1",
        "market-1",
        selection_id,
        Decimal(odds),
        sport="soccer",
        exchange_side="back",
    )


def _saved_book(path) -> tuple[PaperBook, str]:
    book = PaperBook("100")
    ticket = book.open_ticket(
        [_leg("committed-selection")],
        "10",
        placed_at=_BASE_TS,
    )
    book.save(path)
    return book, ticket.ticket_id


def test_pending_new_generation_aborts_when_old_committed_snapshot_is_still_current(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PREPARE alone must not displace the already published economic state."""

    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"
    candidate_path = tmp_path / "candidate-paper-book.json"
    expected_book, committed_ticket_id = _saved_book(path)
    committed_snapshot_bytes = path.read_bytes()

    records, committed, pending = guard._read_witnesses(path)
    assert records
    assert committed is not None
    assert pending is None

    candidate = PaperBook.load(path)
    candidate.open_ticket(
        [_leg("unpublished-selection", "3")],
        "5",
        placed_at="2026-10-03T12:00:01+00:00",
    )
    guard._ORIGINAL_SAVE(candidate, candidate_path)
    candidate_sha = guard._file_sha256(candidate_path)
    assert candidate_sha is not None
    assert candidate_sha != committed[1]

    next_generation = committed[0] + 1
    guard._append_witness(
        path,
        event=guard._PREPARE,
        generation=next_generation,
        snapshot_sha256=candidate_sha,
    )
    witness_path = guard._witness_path(path)
    witness_before_recovery = witness_path.read_bytes()

    recovered = PaperBook.load(path)

    assert path.read_bytes() == committed_snapshot_bytes
    assert recovered.balance == expected_book.balance
    assert tuple(recovered.tickets) == (committed_ticket_id,)

    records_after, committed_after, pending_after = guard._read_witnesses(path)
    assert pending_after is None
    assert committed_after == committed
    assert records_after[-1]["event"] == guard._ABORT
    assert records_after[-1]["generation"] == next_generation
    assert records_after[-1]["snapshot_sha256"] == candidate_sha
    assert witness_path.read_bytes() != witness_before_recovery

    stable_witness = witness_path.read_bytes()
    reopened = PaperBook.load(path)
    assert reopened.balance == expected_book.balance
    assert tuple(reopened.tickets) == (committed_ticket_id,)
    assert path.read_bytes() == committed_snapshot_bytes
    assert witness_path.read_bytes() == stable_witness


def test_truncated_generation_witness_fails_closed_repeatably_without_mutation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corrupt publication metadata must not be replaced by newest-file heuristics."""

    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"
    _saved_book(path)
    snapshot_bytes = path.read_bytes()

    witness_path = guard._witness_path(path)
    lines = witness_path.read_bytes().splitlines(keepends=True)
    assert len(lines) >= 2
    final = lines[-1]
    truncated_final = final[: max(1, len(final) // 2)]
    witness_path.write_bytes(b"".join(lines[:-1]) + truncated_final)
    corrupted_witness = witness_path.read_bytes()

    for _attempt in range(2):
        with pytest.raises(ValueError, match="witness"):
            PaperBook.load(path)
        assert path.read_bytes() == snapshot_bytes
        assert witness_path.read_bytes() == corrupted_witness


def test_reordered_generation_witness_fails_closed_even_with_valid_latest_snapshot(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid snapshot bytes cannot outrank an ambiguous/reordered generation witness."""

    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"
    _saved_book(path)

    newer = PaperBook.load(path)
    newer_ticket = newer.open_ticket(
        [_leg("newer-selection", "4")],
        "5",
        placed_at="2026-10-03T12:00:02+00:00",
    )
    newer.save(path)
    latest_snapshot = path.read_bytes()

    healthy = PaperBook.load(path)
    assert newer_ticket.ticket_id in healthy.tickets

    witness_path = guard._witness_path(path)
    lines = witness_path.read_bytes().splitlines(keepends=True)
    assert len(lines) >= 4

    # Keep every record byte individually valid but reverse the final generation's
    # PREPARE/COMMIT publication order. Sequence/hash-chain/generation authority must
    # reject the metadata instead of accepting the newest parseable snapshot bytes.
    lines[-2], lines[-1] = lines[-1], lines[-2]
    witness_path.write_bytes(b"".join(lines))
    corrupted_witness = witness_path.read_bytes()

    for _attempt in range(2):
        with pytest.raises(ValueError, match="witness"):
            PaperBook.load(path)
        assert path.read_bytes() == latest_snapshot
        assert witness_path.read_bytes() == corrupted_witness



def test_deleted_middle_generation_witness_record_cannot_be_projected_across(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid latest snapshot cannot hide a hole in publication history."""

    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"
    _saved_book(path)

    newer = PaperBook.load(path)
    newer_ticket = newer.open_ticket(
        [_leg("second-generation-selection", "5")],
        "5",
        placed_at="2026-10-03T12:00:03+00:00",
    )
    newer.save(path)
    latest_snapshot = path.read_bytes()
    assert newer_ticket.ticket_id in PaperBook.load(path).tickets

    witness_path = guard._witness_path(path)
    lines = witness_path.read_bytes().splitlines(keepends=True)
    assert len(lines) >= 4

    # Remove generation 1 COMMIT while retaining generation 1 PREPARE and the
    # individually valid generation 2 PREPARE/COMMIT records. The reader must not
    # jump across the missing terminal record to bless the newest snapshot.
    del lines[1]
    witness_path.write_bytes(b"".join(lines))
    corrupted_witness = witness_path.read_bytes()

    for _attempt in range(2):
        with pytest.raises(ValueError, match="witness"):
            PaperBook.load(path)
        assert path.read_bytes() == latest_snapshot
        assert witness_path.read_bytes() == corrupted_witness
