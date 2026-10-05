from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

import autosport._paperbook_preload_authority_guard as paper_guard
from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="paper-reality",
        model_version="2",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="test-seeded-model",
        seed="fixed-seed",
        max_quote_age_ms=5_000,
        min_delay_ms=100,
        max_delay_ms=100,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


def _runtime(tmp_path, *, book: PaperBook, book_path):
    return PaperExecutionAdoptionRuntime(
        book=book,
        ledger=PaperExecutionLedger(tmp_path / "paper-execution.jsonl"),
        config=_config(),
        max_quote_age=timedelta(seconds=5),
        paper_book_path=book_path,
    )


def test_existing_snapshot_constructor_is_read_only_for_bound_caller(tmp_path) -> None:
    book_path = tmp_path / "paper-book.json"
    PaperBook("100").save(book_path)

    caller = PaperBook.load(book_path)
    peer = PaperBook.load(book_path)
    bytes_before = book_path.read_bytes()

    runtime = _runtime(tmp_path, book=caller, book_path=book_path)

    assert runtime.book is caller
    assert book_path.read_bytes() == bytes_before
    assert peer.committed_stake == Decimal("0")

    ticket = peer.open_ticket(
        [
            TicketLeg(
                "event-existing-binding",
                "market-existing-binding",
                "selection-existing-binding",
                Decimal("2.5"),
                sport="soccer",
                exchange_side="back",
            )
        ],
        Decimal("10"),
        placed_at="2026-09-27T21:57:00+00:00",
    )
    peer.save(book_path)

    restored = PaperBook.load(book_path)
    assert restored.balance == Decimal("90")
    assert tuple(restored.tickets) == (ticket.ticket_id,)


def test_existing_snapshot_rejects_unbound_structurally_equal_caller(tmp_path) -> None:
    book_path = tmp_path / "paper-book.json"
    PaperBook("100").save(book_path)
    unbound = PaperBook("100")
    bytes_before = book_path.read_bytes()

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="lacks current durable snapshot authority",
    ):
        _runtime(tmp_path, book=unbound, book_path=book_path)

    assert book_path.read_bytes() == bytes_before
    current = PaperBook.load(book_path)
    assert current.balance == Decimal("100")
    assert current.committed_stake == Decimal("0")


def test_existing_snapshot_binding_ignores_rebound_guard_validator(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book_path = tmp_path / "paper-book.json"
    PaperBook("100").save(book_path)
    unbound = PaperBook("100")
    snapshot_before = book_path.read_bytes()
    witness_path = paper_guard._witness_path(book_path)
    witness_before = witness_path.read_bytes()
    hostile_calls: list[tuple[object, object]] = []

    def hostile(book: object, path: object) -> None:
        hostile_calls.append((book, path))
        return None

    monkeypatch.setattr(paper_guard, "_require_bound_book", hostile)

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="lacks current durable snapshot authority",
    ):
        _runtime(tmp_path, book=unbound, book_path=book_path)

    assert hostile_calls == []
    assert book_path.read_bytes() == snapshot_before
    assert witness_path.read_bytes() == witness_before


def test_existing_snapshot_bound_caller_survives_rebound_guard_validator(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book_path = tmp_path / "paper-book.json"
    PaperBook("100").save(book_path)
    caller = PaperBook.load(book_path)
    hostile_calls: list[tuple[object, object]] = []

    def hostile(book: object, path: object) -> None:
        hostile_calls.append((book, path))
        raise AssertionError("rebound mutable binding validator was dispatched")

    monkeypatch.setattr(paper_guard, "_require_bound_book", hostile)

    runtime = _runtime(tmp_path, book=caller, book_path=book_path)

    assert runtime.book is caller
    assert hostile_calls == []


def test_existing_snapshot_binding_rejects_retargeted_consumer_globals(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book_path = tmp_path / "paper-book.json"
    PaperBook("100").save(book_path)
    unbound = PaperBook("100")
    snapshot_before = book_path.read_bytes()
    witness_path = paper_guard._witness_path(book_path)
    witness_before = witness_path.read_bytes()
    hostile_calls: list[tuple[object, object]] = []

    def hostile(book: object, path: object) -> None:
        hostile_calls.append((book, path))
        return None

    public_init = PaperExecutionAdoptionRuntime.__init__
    monkeypatch.setitem(public_init.__globals__, "_FROZEN_REQUIRE_BOUND_BOOK", hostile)
    monkeypatch.setitem(public_init.__globals__, "_FROZEN_REQUIRE_BOUND_BOOK_CODE", hostile.__code__)
    monkeypatch.setitem(public_init.__globals__, "_REQUIRE_CURRENT_BINDING", hostile)

    with pytest.raises(PaperExecutionAdoptionError, match="lacks current durable snapshot authority"):
        _runtime(tmp_path, book=unbound, book_path=book_path)

    assert hostile_calls == []
    assert book_path.read_bytes() == snapshot_before
    assert witness_path.read_bytes() == witness_before


def test_existing_snapshot_bound_caller_survives_retargeted_consumer_globals(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book_path = tmp_path / "paper-book.json"
    PaperBook("100").save(book_path)
    caller = PaperBook.load(book_path)
    hostile_calls: list[tuple[object, object]] = []

    def hostile(book: object, path: object) -> None:
        hostile_calls.append((book, path))
        raise AssertionError("retargeted consumer globals were dispatched")

    public_init = PaperExecutionAdoptionRuntime.__init__
    monkeypatch.setitem(public_init.__globals__, "_FROZEN_REQUIRE_BOUND_BOOK", hostile)
    monkeypatch.setitem(public_init.__globals__, "_FROZEN_REQUIRE_BOUND_BOOK_CODE", hostile.__code__)
    monkeypatch.setitem(public_init.__globals__, "_REQUIRE_CURRENT_BINDING", hostile)

    runtime = _runtime(tmp_path, book=caller, book_path=book_path)

    assert runtime.book is caller
    assert hostile_calls == []
