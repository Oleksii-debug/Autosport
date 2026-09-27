from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

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
