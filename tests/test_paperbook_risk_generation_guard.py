from __future__ import annotations

from decimal import Decimal

import pytest

import autosport._paperbook_preload_authority_guard as guard
from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


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


def test_risk_read_rejects_nested_paperbook_validator_rebind(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"

    initial = PaperBook("100")
    initial.open_ticket([_leg("nested-validator")], "10", placed_at=_TS)
    initial.save(path)
    loaded = PaperBook.load(path)
    _assert_risk_admitted(loaded)

    def bypass_lifecycle_reachability(cls, book) -> None:
        del cls, book

    monkeypatch.setattr(
        PaperBook,
        "_validate_lifecycle_reachability",
        classmethod(bypass_lifecycle_reachability),
    )

    _assert_risk_rejected(loaded)


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


def test_owner_facing_evaluate_holds_one_generation_and_denies_writer_conflict(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"

    initial = PaperBook("100")
    initial.open_ticket([_leg("evaluate")], "10", placed_at=_TS)
    initial.save(path)
    loaded = PaperBook.load(path)
    policy = PaperRiskPolicy()

    admitted = policy.evaluate(loaded, Decimal("1"))
    assert admitted.allowed is True

    publication_lock = guard._acquire_snapshot_publication_lock(guard._witness_path(path))
    try:
        blocked = policy.evaluate(loaded, Decimal("1"))
    finally:
        guard._release_snapshot_publication_lock(publication_lock)

    assert blocked.allowed is False
    assert blocked.reason == "virtual bankroll generation authority is invalid"


def test_concentration_generation_guard_is_reentrant_and_authority_failure_is_denial(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", _authority_root(tmp_path))
    path = tmp_path / "paper-book.json"

    initial = PaperBook("100")
    initial.open_ticket([_leg("open")], "10", placed_at=_TS)
    initial.save(path)
    loaded = PaperBook.load(path)
    context = ProposedTicketRiskContext(legs=(_leg("candidate"),))

    # The outer concentration read holds the publication lock while its original
    # implementation calls the wrapped _book_state. Read-side reentrancy must keep
    # that nested validation inside the same critical section rather than self-deny.
    decision = PaperRiskPolicy._identity_concentration_decision(
        loaded,
        Decimal("1"),
        context,
        dimension="event",
        limit=Decimal("0.90"),
    )
    assert decision is not None
    assert decision.allowed is False
    assert decision.reason == "owner event concentration limit exceeded"

    publication_lock = guard._acquire_snapshot_publication_lock(guard._witness_path(path))
    try:
        blocked = PaperRiskPolicy._identity_concentration_decision(
            loaded,
            Decimal("1"),
            context,
            dimension="event",
            limit=Decimal("0.90"),
        )
    finally:
        guard._release_snapshot_publication_lock(publication_lock)

    assert blocked is not None
    assert blocked.allowed is False
    assert blocked.reason == "owner event concentration evidence is invalid"


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
