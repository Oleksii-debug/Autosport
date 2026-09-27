from __future__ import annotations

from decimal import Decimal

import pytest

import autosport._paperbook_preload_authority_guard as guard
from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy


_TS = "2026-09-27T19:40:00+00:00"


def _leg(selection_id: str) -> TicketLeg:
    return TicketLeg(
        "risk-generation-event",
        "risk-generation-market",
        selection_id,
        Decimal("2"),
        sport="soccer",
        exchange_side="back",
    )


def _authority_root(tmp_path) -> str:
    return str(tmp_path.parent / f"{tmp_path.name}-risk-generation-authority")


def _assert_risk_rejected(book: object) -> None:
    assert PaperRiskPolicy._book_state(book) is None
    assert PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book) is None
    assert PaperRiskPolicy._historical_risk_metrics(book) is None
    assert PaperRiskPolicy._shadow_book_for_allocation(book) is None


def _assert_risk_admitted(book: PaperBook) -> None:
    assert PaperRiskPolicy._book_state(book) is not None
    assert PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book) is not None
    assert PaperRiskPolicy._historical_risk_metrics(book) is not None
    assert PaperRiskPolicy._shadow_book_for_allocation(book) is not None


def test_risk_generation_guard_preserves_invalid_input_fail_closed_contract() -> None:
    _assert_risk_rejected(object())
    _assert_risk_rejected(None)


def test_stale_risk_read_rejects_even_if_live_validator_descriptor_is_rebound(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"

    initial = PaperBook("100")
    initial.open_ticket([_leg("initial")], "10", placed_at=_TS)
    initial.save(path)

    stale = PaperBook.load(path)
    current = PaperBook.load(path)
    current.open_ticket(
        [_leg("newer")],
        "5",
        placed_at="2026-09-27T19:40:01+00:00",
    )
    current.save(path)

    def bypass_validator(cls, book) -> None:
        del cls, book

    monkeypatch.setattr(
        PaperBook,
        "_validate_loaded_state",
        classmethod(bypass_validator),
    )

    _assert_risk_rejected(stale)


def test_generation_stable_risk_read_fails_closed_while_publication_lock_is_held(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"

    initial = PaperBook("100")
    initial.open_ticket([_leg("locked")], "10", placed_at=_TS)
    initial.save(path)
    loaded = PaperBook.load(path)
    _assert_risk_admitted(loaded)

    publication_lock = guard._acquire_snapshot_publication_lock(guard._witness_path(path))
    try:
        _assert_risk_rejected(loaded)
    finally:
        guard._release_snapshot_publication_lock(publication_lock)

    _assert_risk_admitted(loaded)


def test_frozen_risk_validator_rejects_local_economic_mutation_after_live_rebind(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"

    initial = PaperBook("100")
    initial.open_ticket([_leg("mutation")], "10", placed_at=_TS)
    initial.save(path)
    loaded = PaperBook.load(path)

    # This mutation makes balance inconsistent with the canonical lifecycle. A caller
    # must not be able to bless it by replacing the live class validator after import.
    loaded.balance = Decimal("999")

    def bypass_validator(cls, book) -> None:
        del cls, book

    monkeypatch.setattr(
        PaperBook,
        "_validate_loaded_state",
        classmethod(bypass_validator),
    )

    _assert_risk_rejected(loaded)
