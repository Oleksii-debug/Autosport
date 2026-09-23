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
    """Commit one independently admissible allocation after the first risk PASS."""

    def __init__(self, path, book: PaperBook, risk_policy: PaperRiskPolicy) -> None:
        super().__init__(path)
        self._book = book
        self._risk_policy = risk_policy
        self.mutated = False

    def append(self, record: DecisionRecord) -> str:
        digest = super().append(record)
        if not self.mutated:
            # At committed exposure 19.00, either competing 1.00 action is
            # independently valid: aggregate exposure would become exactly the
            # canonical 20% cap. Execute the competitor first to model the race.
            assert self._risk_policy.evaluate(self._book, Decimal("1.00")).allowed
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
                Decimal("1.00"),
                reason="simultaneous-paper-allocation",
                placed_at="2026-09-20T08:59:59+00:00",
            )
            self.mutated = True
            assert not self._risk_policy.evaluate(
                self._book,
                Decimal("1.00"),
            ).allowed
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


def _seed_canonically_admissible_committed_exposure(
    book: PaperBook,
    policy: PaperRiskPolicy,
) -> None:
    for index in range(19):
        assert policy.evaluate(book, Decimal("1.00")).allowed
        book.open_ticket(
            (
                TicketLeg(
                    f"event-seed-{index}",
                    f"market-seed-{index}",
                    f"selection-seed-{index}",
                    Decimal("2.00"),
                    sport="football",
                ),
            ),
            Decimal("1.00"),
            reason="canonical-risk-admissible-seed",
            placed_at="2026-09-20T08:00:00+00:00",
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
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.02"),
        max_committed_fraction=Decimal("0.20"),
    )
    _seed_canonically_admissible_committed_exposure(book, policy)
    assert book.balance == Decimal("81.00")
    assert policy.evaluate(book, Decimal("1.00")).allowed

    runtime = _runtime(tmp_path, book)
    ledger = _ConcurrentAllocationLedger(
        tmp_path / "decisions.jsonl",
        book,
        policy,
    )
    context = AgentContext(
        book,
        replay_run_id="replay-a",
        decision_ledger=ledger,
        paper_execution=runtime,
        paper_provider_accounts=(("provider-a", "account-a"),),
    )
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

    # Both 1.00 actions independently see committed=19.00 and can reach the 20.00
    # cap. The competing action wins the race after this action's first PASS. The
    # stale action must not then mint admission/execution authority for committed=21.
    with pytest.raises(
        PaperExecutionAdoptionError,
        match="no longer passes canonical risk evaluation",
    ):
        agent.on_market_event(event, context)

    assert ledger.mutated is True
    assert book.balance == Decimal("80.00")
    assert len(book.tickets) == 20
    assert not policy.evaluate(book, Decimal("1.00")).allowed
    assert runtime.ledger.events() == ()
    assert len(tuple(ledger.verified_records())) == 1

    admission_root = tmp_path / ".paper-value-risk-admissions"
    assert not admission_root.exists()
