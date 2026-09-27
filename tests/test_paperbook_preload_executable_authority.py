from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import autosport._paperbook_preload_authority_guard as guard
from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_TS = "2026-09-27T02:00:00+00:00"


def _authority_root(tmp_path: Path) -> str:
    return str(tmp_path.parent / f"{tmp_path.name}-paper-authority")


def _leg(selection_id: str = "selection-executable-authority") -> TicketLeg:
    return TicketLeg(
        "event-executable-authority",
        "market-executable-authority",
        selection_id,
        Decimal("2.5"),
        sport="soccer",
        exchange_side="back",
    )


def _hostile_load_bytes(cls, _payload: bytes):
    # The independent witness authenticates the original bytes, but a mutated
    # decoder executable can currently return unrelated economics afterwards.
    return cls("999")


def _hostile_save(_self, path: str | Path) -> None:
    # A mutated canonical serializer executable can currently supply arbitrary
    # bytes that the outer guard will then PREPARE/COMMIT as authoritative.
    Path(path).write_bytes(b"caller-forged-paperbook-bytes")


def test_path_load_rejects_in_place_canonical_decoder_code_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    source = PaperBook("100")
    source.open_ticket([_leg()], "10", placed_at=_TS)
    source.save(path)
    snapshot_before = path.read_bytes()
    witness = guard._witness_path(path)
    witness_before = witness.read_bytes()

    decoder = guard._LOAD_BYTES
    assert decoder.__code__ is not _hostile_load_bytes.__code__
    monkeypatch.setattr(decoder, "__code__", _hostile_load_bytes.__code__)

    with pytest.raises(ValueError, match="executable|authority"):
        PaperBook.load(path)

    assert path.read_bytes() == snapshot_before
    assert witness.read_bytes() == witness_before


def test_save_rejects_in_place_canonical_serializer_code_mutation_before_witness_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket([_leg("selection-1")], "10", placed_at=_TS)
    book.save(path)
    snapshot_before = path.read_bytes()
    witness = guard._witness_path(path)
    witness_before = witness.read_bytes()

    book.open_ticket([_leg("selection-2")], "5", placed_at=_TS)
    serializer = guard._ORIGINAL_SAVE
    assert serializer.__code__ is not _hostile_save.__code__
    monkeypatch.setattr(serializer, "__code__", _hostile_save.__code__)

    with pytest.raises(ValueError, match="executable|authority"):
        book.save(path)

    assert path.read_bytes() == snapshot_before
    assert witness.read_bytes() == witness_before


def test_path_load_rejects_rebound_guard_witness_before_unwitnessed_copy_is_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rebinding the outer witness helper must not turn copied bytes into positive restart truth."""

    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    authoritative_path = tmp_path / "paper-book.json"
    source = PaperBook("100")
    source.open_ticket([_leg("selection-guard-global")], "10", placed_at=_TS)
    source.save(authoritative_path)

    copied_path = tmp_path / "copied-paper-book.json"
    copied_path.write_bytes(authoritative_path.read_bytes())
    assert not guard._witness_path(copied_path).exists()

    hostile_calls = 0

    def hostile_verify(_snapshot_path: Path, _payload: bytes) -> None:
        nonlocal hostile_calls
        hostile_calls += 1
        return None

    monkeypatch.setattr(guard, "_verify_snapshot_witness", hostile_verify)

    with pytest.raises(ValueError, match="authority|witness|executable"):
        PaperBook.load(copied_path)

    assert hostile_calls == 0
    assert not guard._witness_path(copied_path).exists()
