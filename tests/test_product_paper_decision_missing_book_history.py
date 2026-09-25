from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    execute_paper_plan,
)
from autosport.product_paper_decision_cycle import (
    ProductPaperDecisionCycle,
    ProductPaperDecisionCycleError,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-22T06:00:00+00:00"
STARTED_AT = "2026-09-22T06:00:00.100000+00:00"
EXPIRES_AT = "2026-09-22T06:01:00+00:00"


def _execution_plan() -> ExecutionPlan:
    action = ExecutionAction(
        action_id="prior-action-1",
        bookmaker_id="paper-venue",
        account_id="paper-account",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side="BACK",
        requested_odds="2.50",
        requested_stake="10.00",
        quote_id="quote-1",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )
    return ExecutionPlan(
        plan_id="prior-paper-plan",
        bookmaker_profile_version="paper-profile-v1",
        decision_id="prior-decision",
        approval_id="paper-only",
        created_at=QUOTE_AT,
        actions=(action,),
    )


def _execution_config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="paper-reality",
        model_version="2",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="missing-book-history-falsifier",
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


class ProductPaperDecisionMissingBookHistoryTests(unittest.TestCase):
    def test_missing_book_with_durable_accepted_execution_cannot_reset_bankroll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            ledger = PaperExecutionLedger(workspace / "paper-execution.jsonl")
            run = execute_paper_plan(
                plan=_execution_plan(),
                trigger_id="prior-trigger",
                config=_execution_config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )
            self.assertTrue(run.completed)
            self.assertTrue(run.all_actions_accepted)
            self.assertGreater(len(ledger.events()), 0)

            # Model the state immediately before destructive loss of the canonical
            # PaperBook: accepted exposure was materialized, then only the book
            # snapshot was deleted while append-only execution evidence survived.
            prior_book = PaperBook("100")
            prior_book.open_ticket(
                (
                    TicketLeg(
                        "event-1",
                        "market-1",
                        "selection-1",
                        Decimal("2.50"),
                    ),
                ),
                Decimal("10"),
                reason="paper_execution_attempt_id=prior-attempt",
                placed_at=STARTED_AT,
            )
            prior_book.save(workspace / "paper_book.json")
            self.assertEqual(
                PaperBook.load(workspace / "paper_book.json").balance,
                Decimal("90"),
            )
            (workspace / "paper_book.json").unlink()

            cycle = object.__new__(ProductPaperDecisionCycle)
            cycle.runtime = SimpleNamespace(
                workspace=workspace,
                manifest=SimpleNamespace(
                    source_id="provider-a",
                    initial_bankroll="100",
                ),
            )

            with self.assertRaises(ProductPaperDecisionCycleError):
                cycle._load_current_book()

            self.assertFalse(
                (workspace / "paper_book.json").exists(),
                "missing durable PaperBook was silently recreated at initial bankroll",
            )


if __name__ == "__main__":
    unittest.main()
