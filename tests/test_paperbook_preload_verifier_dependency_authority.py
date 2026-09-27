from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import FunctionType

import pytest

import autosport._paperbook_preload_authority_guard as guard
from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_TS = "2026-09-27T02:00:00+00:00"


def _authority_root(tmp_path: Path) -> str:
    return str(tmp_path.parent / f"{tmp_path.name}-paper-authority")


def _leg(selection_id: str) -> TicketLeg:
    return TicketLeg(
        "event-wrapper-verifier-dependency",
        "market-wrapper-verifier-dependency",
        selection_id,
        Decimal("2.5"),
        sport="soccer",
        exchange_side="back",
    )


def _captured_wrapper_globals(public_wrapper: FunctionType) -> dict[str, object]:
    closure = public_wrapper.__closure__
    assert closure is not None
    for cell in closure:
        value = cell.cell_contents
        if (
            type(value) is dict
            and "_require_value_type_callable_witnesses" in value
            and "_require_class_callable_graph_witnesses" in value
        ):
            return value
    raise AssertionError("captured PaperBook persistence globals are unavailable")


def _rebind_value_type_verifier_dependency(
    monkeypatch: pytest.MonkeyPatch,
    public_wrapper: FunctionType,
) -> None:
    captured_globals = _captured_wrapper_globals(public_wrapper)
    verifier = captured_globals["_require_value_type_callable_witnesses"]
    assert type(verifier) is FunctionType
    dependency_globals = verifier.__globals__
    original = dependency_globals["_require_class_callable_graph_witnesses"]
    assert type(original) is FunctionType

    def delegated(owner: type, witnesses: tuple[tuple[object, ...], ...]) -> None:
        original(owner, witnesses)

    assert delegated is not original
    monkeypatch.setitem(
        dependency_globals,
        "_require_class_callable_graph_witnesses",
        delegated,
    )


def test_path_load_rejects_rebound_verifier_global_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket([_leg("selection-load-verifier-dependency")], "10", placed_at=_TS)
    book.save(path)
    witness = guard._witness_path(path)
    snapshot_before = path.read_bytes()
    witness_before = witness.read_bytes()

    load_descriptor = vars(PaperBook)["load"]
    assert type(load_descriptor) is classmethod
    public_load = load_descriptor.__func__
    assert type(public_load) is FunctionType
    _rebind_value_type_verifier_dependency(monkeypatch, public_load)

    with pytest.raises(ValueError, match="authority|dependency|global|verifier"):
        PaperBook.load(path)

    assert path.read_bytes() == snapshot_before
    assert witness.read_bytes() == witness_before


def test_save_rejects_rebound_verifier_global_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket([_leg("selection-save-verifier-dependency-1")], "10", placed_at=_TS)
    book.save(path)
    witness = guard._witness_path(path)
    snapshot_before = path.read_bytes()
    witness_before = witness.read_bytes()
    book.open_ticket([_leg("selection-save-verifier-dependency-2")], "5", placed_at=_TS)

    public_save = vars(PaperBook)["save"]
    assert type(public_save) is FunctionType
    _rebind_value_type_verifier_dependency(monkeypatch, public_save)

    with pytest.raises(ValueError, match="authority|dependency|global|verifier"):
        book.save(path)

    assert path.read_bytes() == snapshot_before
    assert witness.read_bytes() == witness_before
