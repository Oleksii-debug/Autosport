from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import autosport._paperbook_preload_authority_guard as guard
from autosport.domain import PaperTicket, TicketLeg
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
    return cls("999")


def _hostile_from_raw_snapshot(cls, _raw: object):
    return cls("999")


def _hostile_value_init(_self, *_args: object, **_kwargs: object) -> None:
    raise AssertionError("retargeted canonical value constructor was dispatched")


def _hostile_save(_self, path: str | Path) -> None:
    Path(path).write_bytes(b"caller-forged-paperbook-bytes")


def _hostile_lifecycle_to_json(_self) -> list[dict[str, object]]:
    raise AssertionError("retargeted serializer class helper was dispatched")


def _hostile_authority_root(_ledger_path: Path) -> Path:
    raise AssertionError("mutated independent authority-root executable was dispatched")


def _saved_book(tmp_path: Path, selection_id: str) -> tuple[Path, Path, bytes, bytes]:
    path = tmp_path / "paper-book.json"
    source = PaperBook("100")
    source.open_ticket([_leg(selection_id)], "10", placed_at=_TS)
    source.save(path)
    witness = guard._witness_path(path)
    return path, witness, path.read_bytes(), witness.read_bytes()


def test_path_load_rejects_in_place_canonical_decoder_code_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path, witness, snapshot_before, witness_before = _saved_book(tmp_path, "selection-decoder")

    decoder = guard._LOAD_BYTES
    assert decoder.__code__ is not _hostile_load_bytes.__code__
    monkeypatch.setattr(decoder, "__code__", _hostile_load_bytes.__code__)

    with pytest.raises(ValueError, match="executable|authority"):
        PaperBook.load(path)

    assert path.read_bytes() == snapshot_before
    assert witness.read_bytes() == witness_before


def test_path_load_rejects_transitive_class_parser_code_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path, witness, snapshot_before, witness_before = _saved_book(tmp_path, "selection-class-parser")

    parser_descriptor = vars(PaperBook)["_from_raw_snapshot"]
    assert type(parser_descriptor) is classmethod
    parser = parser_descriptor.__func__
    assert parser.__code__ is not _hostile_from_raw_snapshot.__code__
    monkeypatch.setattr(parser, "__code__", _hostile_from_raw_snapshot.__code__)

    with pytest.raises(ValueError, match="class|executable|authority"):
        PaperBook.load(path)

    assert path.read_bytes() == snapshot_before
    assert witness.read_bytes() == witness_before


def test_path_load_rejects_transitive_class_parser_descriptor_rebind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path, witness, snapshot_before, witness_before = _saved_book(tmp_path, "selection-class-rebind")

    monkeypatch.setattr(
        PaperBook,
        "_from_raw_snapshot",
        classmethod(_hostile_from_raw_snapshot),
    )

    with pytest.raises(ValueError, match="class|dispatch|authority"):
        PaperBook.load(path)

    assert path.read_bytes() == snapshot_before
    assert witness.read_bytes() == witness_before


@pytest.mark.parametrize("value_type", [TicketLeg, PaperTicket])
def test_path_load_rejects_parser_value_constructor_retarget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value_type: type,
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path, witness, snapshot_before, witness_before = _saved_book(
        tmp_path,
        f"selection-value-{value_type.__name__.lower()}",
    )

    original_init = vars(value_type)["__init__"]
    assert original_init is not _hostile_value_init
    monkeypatch.setattr(value_type, "__init__", _hostile_value_init)

    with pytest.raises(ValueError, match="class|executable|authority"):
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


def test_save_uses_frozen_publication_helpers_after_guard_global_rebind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket([_leg("selection-save-global-1")], "10", placed_at=_TS)
    book.save(path)
    book.open_ticket([_leg("selection-save-global-2")], "5", placed_at=_TS)

    hostile_calls = 0

    def hostile_append(*_args: object, **_kwargs: object) -> None:
        nonlocal hostile_calls
        hostile_calls += 1
        raise AssertionError("rebound witness publisher was dispatched")

    monkeypatch.setattr(guard, "_append_witness", hostile_append)
    book.save(path)

    assert hostile_calls == 0
    loaded = PaperBook.load(path)
    assert loaded.balance == Decimal("85")
    assert len(loaded.tickets) == 2


def test_save_rejects_transitive_serializer_class_helper_mutation_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket([_leg("selection-save-class-1")], "10", placed_at=_TS)
    book.save(path)
    snapshot_before = path.read_bytes()
    witness = guard._witness_path(path)
    witness_before = witness.read_bytes()
    book.open_ticket([_leg("selection-save-class-2")], "5", placed_at=_TS)

    serializer_helper = vars(PaperBook)["_lifecycle_to_json"]
    assert serializer_helper.__code__ is not _hostile_lifecycle_to_json.__code__
    monkeypatch.setattr(serializer_helper, "__code__", _hostile_lifecycle_to_json.__code__)

    with pytest.raises(ValueError, match="class|executable|authority"):
        book.save(path)

    assert path.read_bytes() == snapshot_before
    assert witness.read_bytes() == witness_before


def test_path_load_rejects_rebound_guard_witness_before_unwitnessed_copy_is_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_path_load_uses_frozen_independent_root_after_original_code_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path, witness, snapshot_before, witness_before = _saved_book(tmp_path, "selection-root-code")

    root_selector = guard._paper_authority_root
    original_code = root_selector.__code__
    assert original_code is not _hostile_authority_root.__code__
    monkeypatch.setattr(root_selector, "__code__", _hostile_authority_root.__code__)

    try:
        loaded = PaperBook.load(path)
        assert loaded.balance == Decimal("90")
        assert path.read_bytes() == snapshot_before
    finally:
        root_selector.__code__ = original_code

    assert witness.read_bytes() == witness_before
