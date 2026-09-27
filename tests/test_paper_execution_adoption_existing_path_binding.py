from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import PaperExecutionAdoptionRuntime
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


def test_existing_snapshot_binds_exact_caller_book_before_later_mutation(tmp_path) -> None:
    book_path = tmp_path / "paper-book.json"
    PaperBook("100").save(book_path)

    caller = PaperBook("100")
    runtime = PaperExecutionAdoptionRuntime(
        book=caller,
        ledger=PaperExecutionLedger(tmp_path / "paper-execution.jsonl"),
        config=_config(),
        max_quote_age=timedelta(seconds=5),
        paper_book_path=book_path,
    )

    assert runtime.book is caller
    ticket = caller.open_ticket(
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
    caller.save(book_path)

    restored = PaperBook.load(book_path)
    assert restored.balance == Decimal("90")
    assert tuple(restored.tickets) == (ticket.ticket_id,)
