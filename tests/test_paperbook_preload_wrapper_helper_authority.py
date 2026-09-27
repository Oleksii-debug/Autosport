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


def _leg() -> TicketLeg:
    return TicketLeg(
        "event-wrapper-helper-authority",
        "market-wrapper-helper-authority",
        "selection-wrapper-helper-authority",
        Decimal("2.5"),
        sport="soccer",
        exchange_side="back",
    )


def _no_class_graph_check(_owner: type, _witnesses: tuple[tuple[object, ...], ...]) -> None:
    return None


def _forged_from_raw_snapshot(cls, _raw: object):
    return cls("999")


def test_path_load_rejects_in_place_mutation_of_reachable_wrapper_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public load wrapper must not trust a mutable reachable verifier executable."""

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
    verifier = public_load.__globals__["_require_class_callable_graph_witnesses"]
    assert type(verifier) is FunctionType

    parser_descriptor = vars(PaperBook)["_from_raw_snapshot"]
    assert type(parser_descriptor) is classmethod
    parser = parser_descriptor.__func__

    assert verifier.__code__ is not _no_class_graph_check.__code__
    assert parser.__code__ is not _forged_from_raw_snapshot.__code__
    monkeypatch.setattr(verifier, "__code__", _no_class_graph_check.__code__)
    monkeypatch.setattr(parser, "__code__", _forged_from_raw_snapshot.__code__)

    with pytest.raises(ValueError, match="authority|executable|dispatch|verifier"):
        PaperBook.load(path)

    assert path.read_bytes() == snapshot_before
    assert witness.read_bytes() == witness_before
