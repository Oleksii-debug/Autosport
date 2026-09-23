from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from autosport.agents import AgentContext
from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.domain import MarketEvent, TicketLeg
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
from autosport.paper_strategy import Forecast, PaperValueAgent
from autosport.risk import PaperRiskPolicy


class _ConcurrentAllocationLedger(JsonlDecisionLedger):
    """Create a valid competing allocation after the strategy's first risk PASS."""

    def __init__(self, path, book: PaperBook) -> None:
        super().__init__(path)
        self._book = book
        self.mutated = False

    def append(self, record: DecisionRecord) -> str:
        digest = super().append(record)
        if not self.mutated:
            self._book.open_ticket(
                (
                    TicketLeg(
                        "event-concurrent",
                        "market-concurrent",
                        "selection-concurrent",
                        Decimal("2.00"),
                        sport="football",
                    ),
                ),
                Decimal("60.00"),
                reason="simultaneous-paper-allocation",
                placed_at="2026-09-20T08:59:59+00:00",
            )
            self.mutated = True
        return digest


def _runtime(tmp_path, book: PaperBook) -> PaperExecutionAdoptionRuntime:
    config = PaperExecutionModelConfig(
        model_id="risk-admission-stale-book-test",
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
    return PaperExecutionAdoptionRuntime(
        book=book,
        ledger=PaperExecutionLedger(tmp_path / "paper-execution.jsonl"),
        config=config,
        max_quote_age=timedelta(seconds=5),
        paper_book_path=tmp_path / "paper-book.json",
    )


def test_fresh_general_risk_pass_cannot_be_rebound_to_changed_paper_book(tmp_path) -> None:
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
    book = PaperBook("100.00")
    runtime = _runtime(tmp_path, book)
    ledger = _ConcurrentAllocationLedger(tmp_path / "decisions.jsonl", book)
    context = AgentContext(
        book,
        replay_run_id="replay-a",
        decision_ledger=ledger,
        paper_execution=runtime,
        paper_provider_accounts=(("provider-a", "account-a"),),
    )
    policy = PaperRiskPolicy(max_ticket_fraction=Decimal("0.02"))
    agent = PaperValueAgent(
        {
            event.quote_key: Forecast(
                quote_key=event.quote_key,
                probability=Decimal("0.75"),
                model_id="model-a",
                as_of_ts=event.observed_ts,
            )
        },
        stake=Decimal("1.00"),
        minimum_expected_profit_per_unit=Decimal("0"),
        risk_policy=policy,
    )

    # The initial 1.00 stake is admissible against 100.00 (2% = 2.00). The
    # custom durable ledger then creates a valid simultaneous 60.00 allocation,
    # leaving balance 40.00, where the same 1.00 stake exceeds the 0.80 limit.
    with pytest.raises(
        PaperExecutionAdoptionError,
        match="no longer passes canonical risk evaluation",
    ):
        agent.on_market_event(event, context)

    assert ledger.mutated is True
    assert book.balance == Decimal("40.00")
    assert len(book.tickets) == 1
    assert runtime.ledger.events() == ()
    assert len(tuple(ledger.verified_records())) == 1

    admission_root = tmp_path / ".paper-value-risk-admissions"
    assert not admission_root.exists()
