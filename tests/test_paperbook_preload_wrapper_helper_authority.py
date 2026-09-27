from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import FunctionType

import pytest

import autosport._paperbook_preload_authority_guard as guard
from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_TS = "2026-09-27T02:00:00+00:00"
_VERIFIER_NAMES = (
    "_require_delegate_graph_witnesses",
    "_require_class_callable_graph_witnesses",
    "_require_value_type_callable_witnesses",
)


def _authority_root(tmp_path: Path) -> str:
    return str(tmp_path.parent / f"{tmp_path.name}-paper-authority")


def _leg(selection_id: str = "selection-wrapper-helper-authority") -> TicketLeg:
    return TicketLeg(
        "event-wrapper-helper-authority",
        "market-wrapper-helper-authority",
        selection_id,
        Decimal("2.5"),
        sport="soccer",
        exchange_side="back",
    )


def _no_one_arg_check(_value: object) -> None:
    return None


def _no_two_arg_check(_owner: type, _witnesses: tuple[tuple[object, ...], ...]) -> None:
    return None


def _forged_from_raw_snapshot(cls, _raw: object):
    return cls("999")


def _inner_persistence_wrapper(public_wrapper: FunctionType) -> FunctionType:
    closure = public_wrapper.__closure__
    assert closure is not None
    for cell in closure:
        value = cell.cell_contents
        if (
            type(value) is FunctionType
            and "_require_class_callable_graph_witnesses" in value.__globals__
        ):
            return value
    raise AssertionError("sealed inner PaperBook persistence wrapper is unavailable")


def _replacement_code(verifier_name: str):
    if verifier_name == "_require_class_callable_graph_witnesses":
        return _no_two_arg_check.__code__
    return _no_one_arg_check.__code__


@pytest.mark.parametrize("verifier_name", _VERIFIER_NAMES)
def test_path_load_rejects_in_place_mutation_of_reachable_wrapper_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verifier_name: str,
) -> None:
    """The outer seal must reject mutation of every reachable inner verifier executable."""

    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    book.save(path)
    witness = guard._witness_path(path)
    snapshot_before = path.read_bytes()
    witness_before = witness.read_bytes()

    load_descriptor = vars(PaperBook)["load"]
    assert type(load_descriptor) is classmethod
    public_load = load_descriptor.__func__
    inner_load = _inner_persistence_wrapper(public_load)
    verifier = inner_load.__globals__[verifier_name]
    assert type(verifier) is FunctionType

    parser_descriptor = vars(PaperBook)["_from_raw_snapshot"]
    assert type(parser_descriptor) is classmethod
    parser = parser_descriptor.__func__

    replacement_code = _replacement_code(verifier_name)
    assert verifier.__code__ is not replacement_code
    assert parser.__code__ is not _forged_from_raw_snapshot.__code__
    monkeypatch.setattr(verifier, "__code__", replacement_code)
    monkeypatch.setattr(parser, "__code__", _forged_from_raw_snapshot.__code__)

    with pytest.raises(ValueError, match="authority|executable|dispatch|verifier"):
        PaperBook.load(path)

    assert path.read_bytes() == snapshot_before
    assert witness.read_bytes() == witness_before


@pytest.mark.parametrize("verifier_name", _VERIFIER_NAMES)
def test_save_rejects_in_place_mutation_of_reachable_wrapper_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verifier_name: str,
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket([_leg("selection-save-wrapper-helper-1")], "10", placed_at=_TS)
    book.save(path)
    witness = guard._witness_path(path)
    snapshot_before = path.read_bytes()
    witness_before = witness.read_bytes()
    book.open_ticket([_leg("selection-save-wrapper-helper-2")], "5", placed_at=_TS)

    public_save = vars(PaperBook)["save"]
    assert type(public_save) is FunctionType
    inner_save = _inner_persistence_wrapper(public_save)
    verifier = inner_save.__globals__[verifier_name]
    assert type(verifier) is FunctionType
    replacement_code = _replacement_code(verifier_name)
    assert verifier.__code__ is not replacement_code
    monkeypatch.setattr(verifier, "__code__", replacement_code)

    with pytest.raises(ValueError, match="authority|executable|dispatch|verifier"):
        book.save(path)

    assert path.read_bytes() == snapshot_before
    assert witness.read_bytes() == witness_before
