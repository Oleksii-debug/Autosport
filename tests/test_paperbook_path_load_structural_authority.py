from __future__ import annotations

import json
from decimal import Decimal

import pytest

from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_TS = "2026-09-27T00:45:00+00:00"


def _leg(selection_id: str = "selection-structural-load") -> TicketLeg:
    return TicketLeg(
        "event-structural-load",
        "market-structural-load",
        selection_id,
        Decimal("2.5"),
        sport="soccer",
        exchange_side="back",
    )


def _authority_root(tmp_path) -> str:
    return str(tmp_path.parent / f"{tmp_path.name}-paper-authority")


def test_witnessed_path_roundtrip_restores_positive_opening_authority(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    source = PaperBook("100")
    ticket = source.open_ticket([_leg()], "10", placed_at=_TS)
    source.save(path)

    loaded = PaperBook.load(path)
    assert loaded.balance == Decimal("90")
    assert loaded.committed_stake == Decimal("10")
    assert loaded.tickets[ticket.ticket_id].stake == Decimal("10")

    loaded.open_ticket([_leg("selection-2")], "5", placed_at=_TS)
    loaded.save(path)
    restored = PaperBook.load(path)
    assert restored.balance == Decimal("85")
    assert restored.committed_stake == Decimal("15")


def test_unwitnessed_copy_cannot_mint_positive_restart_authority(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    copied = tmp_path / "copied-paper-book.json"
    source = PaperBook("100")
    source.open_ticket([_leg()], "10", placed_at=_TS)
    source.save(path)
    copied.write_bytes(path.read_bytes())

    with pytest.raises(ValueError, match="missing independent durable opening witness"):
        PaperBook.load(copied)

    structural = PaperBook.load_bytes(copied.read_bytes())
    assert structural.balance == Decimal("90")
    with pytest.raises(
        ValueError,
        match="byte-loaded snapshot lacks product-issued opening authority",
    ):
        _ = structural.committed_stake


def test_path_load_rejects_whole_snapshot_rollback(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket([_leg("selection-1")], "10", placed_at=_TS)
    book.save(path)
    first_generation = path.read_bytes()

    book.open_ticket([_leg("selection-2")], "5", placed_at=_TS)
    book.save(path)
    path.write_bytes(first_generation)

    with pytest.raises(ValueError, match="independent durable opening witness"):
        PaperBook.load(path)


def test_failed_final_replace_recovers_last_committed_snapshot(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    first = book.open_ticket([_leg("selection-1")], "10", placed_at=_TS)
    book.save(path)
    last_good = path.read_bytes()

    book.open_ticket([_leg("selection-2")], "5", placed_at=_TS)

    import autosport._paperbook_preload_authority_guard as guard

    original_replace = guard._OS_REPLACE

    def fail_replace(*_args, **_kwargs):
        raise OSError("injected final replace failure")

    monkeypatch.setattr(guard, "_OS_REPLACE", fail_replace)
    with pytest.raises(OSError, match="injected final replace failure"):
        book.save(path)
    monkeypatch.setattr(guard, "_OS_REPLACE", original_replace)

    assert path.read_bytes() == last_good
    restored = PaperBook.load(path)
    assert tuple(restored.tickets) == (first.ticket_id,)
    assert restored.balance == Decimal("90")
    assert restored.committed_stake == Decimal("10")


def test_published_candidate_with_interrupted_commit_recovers_forward(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket([_leg("selection-1")], "10", placed_at=_TS)
    book.save(path)
    book.open_ticket([_leg("selection-2")], "5", placed_at=_TS)

    import autosport._paperbook_preload_authority_guard as guard

    original_append = guard._append_witness

    def interrupt_commit(snapshot_path, *, event, generation, snapshot_sha256):
        if event == guard._COMMIT:
            raise OSError("injected COMMIT interruption")
        return original_append(
            snapshot_path,
            event=event,
            generation=generation,
            snapshot_sha256=snapshot_sha256,
        )

    monkeypatch.setattr(guard, "_append_witness", interrupt_commit)
    with pytest.raises(OSError, match="injected COMMIT interruption"):
        book.save(path)
    monkeypatch.setattr(guard, "_append_witness", original_append)

    restored = PaperBook.load(path)
    assert restored.balance == Decimal("85")
    assert restored.committed_stake == Decimal("15")
    assert len(restored.tickets) == 2


def test_empty_snapshot_is_still_bound_to_independent_witness(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book-empty.json"
    book = PaperBook("100")
    book.save(path)
    assert PaperBook.load(path).balance == Decimal("100")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["tickets"] == []
    payload["initial_bankroll"] = "1000"
    payload["balance"] = "1000"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="independent durable opening witness"):
        PaperBook.load(path)


def test_witness_journal_tamper_is_not_restart_authority(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.save(path)

    import autosport._paperbook_preload_authority_guard as guard

    witness = guard._witness_path(path)
    lines = witness.read_text(encoding="utf-8").splitlines()
    final = json.loads(lines[-1])
    final["snapshot_sha256"] = "0" * 64
    lines[-1] = json.dumps(final, sort_keys=True, separators=(",", ":"))
    witness.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="digest mismatch|matching PREPARE"):
        PaperBook.load(path)


def test_exact_fresh_book_can_rebind_only_unchanged_witnessed_state(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    first = PaperBook("100")
    first.save(path)

    # Existing adoption runtimes may reconstruct an independently authoritative
    # empty PaperBook before comparing it with durable state. Exact canonical bytes
    # are sufficient to bind that object to this lineage; changed economics are not.
    equivalent = PaperBook("100")
    equivalent.save(path)
    assert PaperBook.load(path).balance == Decimal("100")

    changed = PaperBook("200")
    with pytest.raises(ValueError, match="verified path-bound authority"):
        changed.save(path)


def test_bound_book_rejects_authority_root_drift_and_fresh_overwrite(
    tmp_path, monkeypatch
) -> None:
    first_root = tmp_path.parent / f"{tmp_path.name}-paper-authority-a"
    second_root = tmp_path.parent / f"{tmp_path.name}-paper-authority-b"
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", str(first_root))
    path = tmp_path / "paper-book.json"
    original = PaperBook("100")
    original.open_ticket([_leg()], "10", placed_at=_TS)
    original.save(path)
    loaded = PaperBook.load(path)

    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", str(second_root))
    with pytest.raises(
        ValueError,
        match="path or authority root|verified path-bound authority",
    ):
        loaded.save(path)

    fresh = PaperBook("100")
    with pytest.raises(
        ValueError,
        match="verified path-bound authority|lacks independent durable authority",
    ):
        fresh.save(path)
