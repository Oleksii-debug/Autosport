from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autosport.paper import PaperBook
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
    PreparedPaperExecution,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionEvidenceRecord,
    PaperExecutionEvidenceRegistry,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-20T06:00:00+00:00"
STARTED_AT = "2026-09-20T06:00:00.100000+00:00"
EXPIRES_AT = "2026-09-20T06:01:00+00:00"


def action(action_id: str, *, odds: str = "2.50", stake: str = "10.00"):
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="paper-venue",
        account_id="paper-account",
        event_id=f"event-{action_id}",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side="BACK",
        requested_odds=odds,
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def prepared(
    runtime: PaperExecutionAdoptionRuntime,
    *actions: ExecutionAction,
    market_semantics_id: str | None = None,
) -> PreparedPaperExecution:
    return runtime._mint_prepared(
        PreparedPaperExecution(
            execution_plan=ExecutionPlan(
                plan_id="adoption-plan-1",
                bookmaker_profile_version="paper-profile-v1",
                decision_id="decision-1",
                approval_id="paper-only-no-real-money",
                created_at=QUOTE_AT,
                actions=tuple(actions),
            ),
            exposure_bindings=tuple(
                PaperExposureBinding(
                    action_id=item.action_id,
                    sport="soccer",
                    bankroll_id="paper-bankroll",
                    currency="EUR",
                    market_semantics_id=market_semantics_id,
                )
                for item in actions
            ),
            intent_evidence_json='{"schema":"test-intent-evidence"}',
        )
    )


def config(**overrides) -> PaperExecutionModelConfig:
    values = {
        "model_id": "paper-reality",
        "model_version": "2",
        "evidence_grade": EvidenceGrade.SYNTHETIC,
        "evidence_source": "test-seeded-model",
        "seed": "fixed-seed",
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


def evidence(
    current: ExecutionAction,
    outcome: PaperAttemptOutcome,
    *,
    odds: str | None = None,
    stake: str | None = None,
):
    return PaperExecutionEvidenceRecord(
        action_id=current.action_id,
        bookmaker_id=current.bookmaker_id,
        account_id=current.account_id,
        event_id=current.event_id,
        market_id=current.market_id,
        selection_id=current.selection_id,
        side=current.side,
        quote_id=current.quote_id,
        outcome=outcome,
        observed_at=STARTED_AT,
        evidence_grade=EvidenceGrade.CONFIGURED,
        evidence_source="fixture-observation",
        accepted_odds=odds,
        accepted_stake=stake,
    )


class PaperExecutionAdoptionTests(unittest.TestCase):
    def runtime(self, tmp: str, *, model=None):
        book = PaperBook("100.00")
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        runtime = PaperExecutionAdoptionRuntime(
            book=book,
            ledger=ledger,
            config=model or config(),
            max_quote_age=__import__("datetime").timedelta(seconds=5),
            paper_book_path=Path(tmp) / "paper-book.json",
        )
        return book, ledger, runtime

    def test_moved_accepted_quote_materializes_execution_truth_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("a1", odds="2.50", stake="10.00")
            current_prepared = prepared(runtime, current)
            registered = evidence(
                current,
                PaperAttemptOutcome.ACCEPTED,
                odds="2.25",
                stake="10.00",
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(registered)

            first = runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-1",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={"a1": registered.as_observation()},
                evidence_registry=registry,
            )
            second = runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-1",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={"a1": registered.as_observation()},
                evidence_registry=registry,
            )

            self.assertEqual(first.run.run_id, second.run.run_id)
            self.assertEqual(first.ticket_ids, second.ticket_ids)
            self.assertEqual(len(book.tickets), 1)
            ticket = next(iter(book.tickets.values()))
            self.assertEqual(str(ticket.stake), "10.00")
            self.assertEqual(str(ticket.legs[0].locked_odds), "2.25")
            self.assertEqual(ticket.legs[0].exchange_side, "back")
            self.assertIsNone(ticket.legs[0].market_semantics_id)
            self.assertEqual(ticket.placed_at, STARTED_AT)
            self.assertEqual(book.balance, __import__("decimal").Decimal("90.00"))

    def test_market_semantics_survives_accepted_adoption_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("a1", odds="2.50", stake="10.00")
            semantics = "soccer:h2h:v1"
            current_prepared = prepared(
                runtime,
                current,
                market_semantics_id=semantics,
            )
            registered = evidence(
                current,
                PaperAttemptOutcome.ACCEPTED,
                odds="2.25",
                stake="10.00",
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(registered)

            result = runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-semantics",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={"a1": registered.as_observation()},
                evidence_registry=registry,
            )

            self.assertEqual(len(result.ticket_ids), 1)
            ticket = next(iter(book.tickets.values()))
            self.assertEqual(ticket.legs[0].exchange_side, "back")
            self.assertEqual(ticket.legs[0].market_semantics_id, semantics)

            reloaded = PaperBook.load(Path(tmp) / "paper-book.json")
            reloaded_ticket = next(iter(reloaded.tickets.values()))
            self.assertEqual(reloaded_ticket.legs[0].exchange_side, "back")
            self.assertEqual(
                reloaded_ticket.legs[0].market_semantics_id,
                semantics,
            )

    def test_rejected_and_unknown_never_create_paper_exposure(self):
        for outcome, override in (
            (PaperAttemptOutcome.REJECTED, {"rejected_bps": 10_000}),
            (PaperAttemptOutcome.UNKNOWN, {"unknown_bps": 10_000}),
        ):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                book, _ledger, runtime = self.runtime(tmp, model=config(**override))
                current_prepared = prepared(runtime, action("a1"))
                first = runtime.execute(
                    prepared=current_prepared,
                    trigger_id=f"trigger-{outcome.value}",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )
                second = runtime.execute(
                    prepared=current_prepared,
                    trigger_id=f"trigger-{outcome.value}",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )
                self.assertEqual(first.run.run_id, second.run.run_id)
                self.assertEqual(first.run.attempts[0].outcome, outcome)
                self.assertEqual(first.ticket_ids, ())
                self.assertEqual(second.ticket_ids, ())
                self.assertEqual(book.tickets, {})

    def test_partial_materializes_only_exact_partial_stake(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("a1", stake="10.00")
            registered = evidence(
                current,
                PaperAttemptOutcome.PARTIAL,
                odds="2.40",
                stake="4.00",
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(registered)
            result = runtime.execute(
                prepared=prepared(runtime, current),
                trigger_id="trigger-partial",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={"a1": registered.as_observation()},
                evidence_registry=registry,
            )
            self.assertEqual(result.run.attempts[0].outcome, PaperAttemptOutcome.PARTIAL)
            self.assertEqual(len(book.tickets), 1)
            ticket = next(iter(book.tickets.values()))
            self.assertEqual(str(ticket.stake), "4.00")
            self.assertEqual(str(ticket.legs[0].locked_odds), "2.40")
            self.assertEqual(book.balance, __import__("decimal").Decimal("96.00"))

    def test_attempt_before_ticket_restart_materializes_same_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            book_path = Path(tmp) / "paper-book.json"
            current_action = action("a1")
            current_prepared = prepared(runtime, current_action)
            shadow = runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-crash-window",
                started_at=STARTED_AT,
                materialize_exposure=False,
            )
            self.assertEqual(book.tickets, {})
            self.assertEqual(PaperBook.load(book_path).tickets, {})

            # Simulate a fresh process after the durable #623 attempt but before
            # any exposure was published. Restart must re-mint authority from the
            # same canonical action instead of reusing an in-process capability.
            reloaded_book = PaperBook.load(book_path)
            restarted = PaperExecutionAdoptionRuntime(
                book=reloaded_book,
                ledger=ledger,
                config=runtime.config,
                max_quote_age=runtime.max_quote_age,
                paper_book_path=book_path,
            )
            restarted_prepared = prepared(restarted, current_action)
            resumed = restarted.execute(
                prepared=restarted_prepared,
                trigger_id="trigger-crash-window",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )
            self.assertEqual(shadow.run, resumed.run)
            self.assertEqual(len(reloaded_book.tickets), 1)

            # Simulate a second fresh process after durable PaperBook publication
            # but before the caller could publish its own COMMITTED progress.
            committed_book = PaperBook.load(book_path)
            restarted_again = PaperExecutionAdoptionRuntime(
                book=committed_book,
                ledger=ledger,
                config=runtime.config,
                max_quote_age=runtime.max_quote_age,
                paper_book_path=book_path,
            )
            restarted_again_prepared = prepared(restarted_again, current_action)
            again = restarted_again.execute(
                prepared=restarted_again_prepared,
                trigger_id="trigger-crash-window",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )
            self.assertEqual(resumed.ticket_ids, again.ticket_ids)
            self.assertEqual(len(committed_book.tickets), 1)
            self.assertEqual(PaperBook.load(book_path).tickets, committed_book.tickets)

    def test_second_action_rejection_keeps_only_first_accepted_exposure(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            a1, a2 = action("a1"), action("a2")
            e1 = evidence(
                a1,
                PaperAttemptOutcome.ACCEPTED,
                odds="2.45",
                stake="10.00",
            )
            e2 = evidence(a2, PaperAttemptOutcome.REJECTED)
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(e1)
            registry.register(e2)
            result = runtime.execute(
                prepared=prepared(runtime, a1, a2),
                trigger_id="trigger-second-reject",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={
                    "a1": e1.as_observation(),
                    "a2": e2.as_observation(),
                },
                evidence_registry=registry,
            )
            self.assertEqual(
                [item.outcome for item in result.run.attempts],
                [PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.REJECTED],
            )
            self.assertEqual(len(book.tickets), 1)
            ticket = next(iter(book.tickets.values()))
            self.assertEqual(ticket.legs[0].event_id, a1.event_id)
            self.assertEqual(
                result.run.pending_action_ids,
                (),
            )

    def test_stale_quote_attempt_is_durable_rejection_without_ticket(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            result = runtime.execute(
                prepared=prepared(runtime, action("a1")),
                trigger_id="trigger-stale",
                started_at="2026-09-20T06:00:06+00:00",
                materialize_exposure=True,
            )
            self.assertEqual(
                result.run.attempts[0].outcome,
                PaperAttemptOutcome.REJECTED,
            )
            self.assertEqual(result.ticket_ids, ())
            self.assertEqual(book.tickets, {})

    def test_suspended_action_is_durable_rejection_without_ticket(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            result = runtime.execute(
                prepared=prepared(runtime, action("a1")),
                trigger_id="trigger-suspended",
                started_at=STARTED_AT,
                materialize_exposure=True,
                suspended_action_ids=frozenset({"a1"}),
            )
            self.assertEqual(
                result.run.attempts[0].outcome,
                PaperAttemptOutcome.REJECTED,
            )
            self.assertTrue(result.run.attempts[0].suspended)
            self.assertEqual(result.ticket_ids, ())
            self.assertEqual(book.tickets, {})

    def test_duplicate_attempt_marker_fails_closed_after_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            book_path = Path(tmp) / "paper-book.json"
            current_action = action("a1")
            current_prepared = prepared(runtime, current_action)
            first = runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-marker-conflict",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )
            self.assertEqual(len(first.ticket_ids), 1)
            original = next(iter(book.tickets.values()))
            book.open_ticket(
                original.legs,
                original.stake,
                reason=original.strategy_reason,
                placed_at=original.placed_at,
                provider_source_ids=original.provider_source_ids,
                provider_accounts=original.provider_accounts,
                bankroll_id=original.bankroll_id,
                currency=original.currency,
            )
            book.save(book_path)

            reloaded = PaperBook.load(book_path)
            restarted = PaperExecutionAdoptionRuntime(
                book=reloaded,
                ledger=ledger,
                config=runtime.config,
                max_quote_age=runtime.max_quote_age,
                paper_book_path=book_path,
            )
            restarted_prepared = prepared(restarted, current_action)
            with self.assertRaisesRegex(
                Exception,
                "duplicate exposure",
            ):
                restarted.execute(
                    prepared=restarted_prepared,
                    trigger_id="trigger-marker-conflict",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )

    def test_shadow_execution_keeps_attempt_evidence_without_ticket(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            result = runtime.execute(
                prepared=prepared(runtime, action("a1")),
                trigger_id="trigger-shadow",
                started_at=STARTED_AT,
                materialize_exposure=False,
            )
            self.assertTrue(result.run.completed)
            self.assertEqual(result.ticket_ids, ())
            self.assertEqual(book.tickets, {})


if __name__ == "__main__":
    unittest.main()