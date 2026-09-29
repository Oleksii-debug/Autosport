from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
    PreparedPaperExecution,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-20T06:00:00+00:00"
STARTED_AT = "2026-09-20T06:00:00.100000+00:00"
EXPIRES_AT = "2026-09-20T06:01:00+00:00"


def _config(**overrides) -> PaperExecutionModelConfig:
    values = {
        "model_id": "paper-reality",
        "model_version": "append-recovery-v1",
        "evidence_grade": EvidenceGrade.SYNTHETIC,
        "evidence_source": "append-recovery-test",
        "seed": "append-recovery-fixed-seed",
        "max_quote_age_ms": 5_000,
        "min_delay_ms": 100,
        "max_delay_ms": 100,
        "rejected_bps": 0,
        "partial_bps": 0,
        "unknown_bps": 0,
        "partial_fill_bps": 5_000,
        "max_slippage_bps": 0,
    }
    values.update(overrides)
    return PaperExecutionModelConfig(**values)


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="append-recovery-action",
        bookmaker_id="paper-venue",
        account_id="paper-account",
        event_id="event-main",
        market_id="market-main",
        selection_id="selection-main",
        side="BACK",
        requested_odds="2.50",
        requested_stake="10.00",
        quote_id="quote-main",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def _prepared(
    runtime: PaperExecutionAdoptionRuntime,
    action: ExecutionAction,
) -> PreparedPaperExecution:
    return runtime._mint_prepared(
        PreparedPaperExecution(
            execution_plan=ExecutionPlan(
                plan_id="append-recovery-plan",
                bookmaker_profile_version="paper-profile-v1",
                decision_id="decision-append-recovery",
                approval_id="paper-only-no-real-money",
                created_at=QUOTE_AT,
                actions=(action,),
            ),
            exposure_bindings=(
                PaperExposureBinding(
                    action_id=action.action_id,
                    sport="soccer",
                    bankroll_id="paper-bankroll",
                    currency="EUR",
                ),
            ),
            intent_evidence_json='{"schema":"append-recovery-test"}',
        )
    )


def _runtime(
    root: Path,
    book: PaperBook,
    *,
    model: PaperExecutionModelConfig,
) -> PaperExecutionAdoptionRuntime:
    return PaperExecutionAdoptionRuntime(
        book=book,
        ledger=PaperExecutionLedger(root / "paper-execution.jsonl"),
        config=model,
        max_quote_age=timedelta(seconds=5),
        paper_book_path=root / "paper_book.json",
    )


def _persist_unrelated_ticket(book: PaperBook, path: Path) -> None:
    book.open_ticket(
        [
            TicketLeg(
                event_id="event-unrelated",
                market_id="market-unrelated",
                selection_id="selection-unrelated",
                locked_odds=Decimal("2.00"),
                sport="soccer",
            )
        ],
        Decimal("1.00"),
        reason="unrelated durable mutation",
        placed_at="2026-09-20T06:00:00.200000+00:00",
    )
    book.save(path)


class PaperExecutionAppendRecoveryTests(unittest.TestCase):
    def test_live_retry_accepts_exact_authorized_post_action_book_without_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = _config()
            book = PaperBook("100.00")
            runtime = _runtime(root, book, model=model)
            book.save(root / "live_decision_pre_action_book.json")
            action = _action()

            first = runtime.execute(
                prepared=_prepared(runtime, action),
                trigger_id="live-append-recovery-exact",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )
            self.assertEqual(first.run.attempts[0].outcome, PaperAttemptOutcome.ACCEPTED)
            self.assertEqual(len(book.tickets), 1)
            first_ticket_ids = tuple(book.tickets)

            restarted_book = PaperBook.load(root / "paper_book.json")
            restarted = _runtime(root, restarted_book, model=model)
            resumed = restarted.execute(
                prepared=_prepared(restarted, action),
                trigger_id="live-append-recovery-exact",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )

            self.assertEqual(resumed.run.run_id, first.run.run_id)
            self.assertEqual(tuple(restarted_book.tickets), first_ticket_ids)
            self.assertEqual(len(restarted_book.tickets), 1)
            self.assertEqual(restarted_book.balance, Decimal("90.00"))

    def test_live_retry_rejects_unrelated_book_mutation_after_accepted_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = _config()
            book = PaperBook("100.00")
            runtime = _runtime(root, book, model=model)
            book.save(root / "live_decision_pre_action_book.json")
            action = _action()

            first = runtime.execute(
                prepared=_prepared(runtime, action),
                trigger_id="live-append-recovery-accepted",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )
            self.assertEqual(first.run.attempts[0].outcome, PaperAttemptOutcome.ACCEPTED)
            self.assertEqual(len(book.tickets), 1)

            _persist_unrelated_ticket(book, root / "paper_book.json")
            restarted_book = PaperBook.load(root / "paper_book.json")
            restarted = _runtime(root, restarted_book, model=model)

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "exact pre-action or #623-authorized post-action state",
            ):
                restarted.execute(
                    prepared=_prepared(restarted, action),
                    trigger_id="live-append-recovery-accepted",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )

    def test_live_retry_rejects_changed_book_for_terminal_no_fill_attempts(self):
        for outcome, override in (
            (PaperAttemptOutcome.REJECTED, {"rejected_bps": 10_000}),
            (PaperAttemptOutcome.UNKNOWN, {"unknown_bps": 10_000}),
        ):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                model = _config(**override)
                book = PaperBook("100.00")
                runtime = _runtime(root, book, model=model)
                book.save(root / "live_decision_pre_action_book.json")
                action = _action()
                trigger_id = f"live-append-recovery-{outcome.value.lower()}"

                result = runtime.execute(
                    prepared=_prepared(runtime, action),
                    trigger_id=trigger_id,
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )
                self.assertEqual(result.run.attempts[0].outcome, outcome)
                self.assertEqual(result.ticket_ids, ())
                self.assertEqual(book.tickets, {})

                _persist_unrelated_ticket(book, root / "paper_book.json")
                restarted_book = PaperBook.load(root / "paper_book.json")
                restarted = _runtime(root, restarted_book, model=model)

                with self.assertRaisesRegex(
                    PaperExecutionAdoptionError,
                    "exact pre-action or #623-authorized post-action state",
                ):
                    restarted.execute(
                        prepared=_prepared(restarted, action),
                        trigger_id=trigger_id,
                        started_at=STARTED_AT,
                        materialize_exposure=True,
                    )

    def test_shadow_live_retry_requires_exact_pre_action_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = _config()
            book = PaperBook("100.00")
            runtime = _runtime(root, book, model=model)
            book.save(root / "live_decision_pre_action_book.json")
            action = _action()

            result = runtime.execute(
                prepared=_prepared(runtime, action),
                trigger_id="live-append-recovery-shadow",
                started_at=STARTED_AT,
                materialize_exposure=False,
            )
            self.assertEqual(result.ticket_ids, ())
            self.assertEqual(book.tickets, {})

            _persist_unrelated_ticket(book, root / "paper_book.json")
            restarted_book = PaperBook.load(root / "paper_book.json")
            restarted = _runtime(root, restarted_book, model=model)

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "SHADOW recovery PaperBook differs from exact pre-action state",
            ):
                restarted.execute(
                    prepared=_prepared(restarted, action),
                    trigger_id="live-append-recovery-shadow",
                    started_at=STARTED_AT,
                    materialize_exposure=False,
                )


if __name__ == "__main__":
    unittest.main()
