from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import autosport._paperbook_preload_authority_guard as guard
import autosport.paper as paper_module
from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_TS = "2026-09-27T12:50:00+00:00"


def _authority_root(tmp_path: Path) -> str:
    return str(tmp_path.parent / f"{tmp_path.name}-paper-authority")


def _leg() -> TicketLeg:
    return TicketLeg(
        "event-delegate-closure",
        "market-delegate-closure",
        "selection-delegate-closure",
        Decimal("2.5"),
        sport="soccer",
        exchange_side="back",
    )


class _HostileAuthorities(dict[object, object]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def __setitem__(self, key: object, value: object) -> None:
        self.calls += 1
        super().__setitem__(key, value)

    def get(self, key: object, default: object = None) -> object:
        self.calls += 1
        return super().get(key, default)


def _closure_cell(function: object, freevar: str):
    code = getattr(function, "__code__")
    closure = getattr(function, "__closure__")
    assert closure is not None
    index = code.co_freevars.index(freevar)
    return closure[index]


def _witnessed_book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"
    source = PaperBook("100")
    source.open_ticket([_leg()], "10", placed_at=_TS)
    source.save(path)
    return path


def test_positive_path_load_rejects_opening_registry_closure_retarget_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _witnessed_book(tmp_path, monkeypatch)

    # The canonical opening-authority functions are factory closures sharing the
    # same private authority mapping cell.  The predecessor witness recorded only
    # the cell object, so replacing its contents preserved __closure__ tuple equality
    # while redirecting positive load into caller-controlled state.
    cell = _closure_cell(guard._REGISTER_OPENING, "authorities")
    original_authorities = cell.cell_contents
    hostile = _HostileAuthorities()
    cell.cell_contents = hostile
    try:
        with pytest.raises(ValueError, match="closure|executable|authority"):
            PaperBook.load(path)
    finally:
        cell.cell_contents = original_authorities

    assert hostile.calls == 0


def test_positive_path_load_rejects_delegate_global_retarget_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _witnessed_book(tmp_path, monkeypatch)
    hostile_calls = 0

    def hostile_commitment(_ticket: object) -> tuple[object, ...]:
        nonlocal hostile_calls
        hostile_calls += 1
        return ("forged-opening-authority",)

    # _INSTALL_OPENING late-resolves this paper-module helper.  Globals-dict identity
    # alone therefore did not witness the actual authority implementation selected by
    # the captured delegate.
    monkeypatch.setattr(paper_module, "_ticket_opening_commitment", hostile_commitment)

    with pytest.raises(ValueError, match="global|executable|authority"):
        PaperBook.load(path)

    assert hostile_calls == 0
