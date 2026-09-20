from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from autosport.domain import MarketEvent
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
        model_id="paper-value-authority-test",
        model_version="1",
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


def test_public_paper_value_prepare_is_descriptor_only(tmp_path) -> None:
    book = PaperBook("100.00")
    runtime = PaperExecutionAdoptionRuntime(
        book=book,
        ledger=PaperExecutionLedger(tmp_path / "paper-execution.jsonl"),
        config=_config(),
        max_quote_age=timedelta(seconds=5),
        paper_book_path=tmp_path / "paper-book.json",
    )
    event = MarketEvent(
        event_id="event-a",
        market_id="market-a",
        selection_id="selection-a",
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-20T09:00:00+00:00",
        source_id="provider-a",
        sequence=1,
        sport="football",
    )

    descriptor = runtime.prepare_paper_value_action(
        event=event,
        stake=Decimal("10.00"),
        decision_id="decision-a",
        account_id="account-a",
        bankroll_id="bankroll-a",
        currency="EUR",
    )

    # The public compatibility seam may describe the exact execution plan so the
    # DecisionRecord can bind it durably, but it must not mint a positive runtime
    # capability from caller-authored values.
    assert descriptor.__class__.__name__ == "PaperValueExecutionDescriptor"
    assert runtime._prepared_authorities == {}

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="lacks active durable decision authority",
    ):
        runtime.execute(
            prepared=descriptor,
            trigger_id="decision-a",
            started_at=event.observed_ts,
            materialize_exposure=True,
        )

    assert book.balance == Decimal("100.00")
    assert not book.tickets
