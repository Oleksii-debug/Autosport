from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path
from threading import Event, RLock, Thread

from autosport.domain import MarketEvent, TicketLeg
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
    PaperExecutionEvidenceRecord,
    PaperExecutionEvidenceRegistry,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    PaperExecutionStateError,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-20T06:00:00+00:00"
STARTED_AT = "2026-09-20T06:00:00.100000+00:00"
EXPIRES_AT = "2026-09-20T06:01:00+00:00"


def action(
    action_id: str,
    *,
    odds: str = "2.50",
    stake: str = "10.00",
    side: str = "BACK",
):
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="paper-venue",
        account_id="paper-account",
        event_id=f"event-{action_id}",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side=side,
        requested_odds=odds,
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def prepared(
    runtime: PaperExecutionAdoptionRuntime,
    *actions: ExecutionAction,
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


def market_event(exchange_side: str | None) -> MarketEvent:
    return MarketEvent(
        event_id="event-side",
        market_id="market-side",
        selection_id="selection-side",
        decimal_odds=Decimal("2.50"),
        observed_ts=QUOTE_AT,
        source_id="paper-venue",
        sequence=1,
        source_ts=QUOTE_AT,
        ingest_ts=QUOTE_AT,
        sport="soccer",
        exchange_side=exchange_side,
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

    def test_prepared_scope_inputs_are_canonical_before_mint(self):
        with self.assertRaisesRegex(
            ValueError,
            "bankroll_id and currency must be supplied together",
        ):
            PaperExposureBinding(
                action_id="a1",
                sport="soccer",
                bankroll_id="paper-bankroll",
                currency=None,
            )
        with self.assertRaisesRegex(
            ValueError,
            "currency must be three-letter uppercase ASCII",
        ):
            PaperExposureBinding(
                action_id="a1",
                sport="soccer",
                bankroll_id="paper-bankroll",
                currency="eur",
            )

        binding = PaperExposureBinding(
            action_id="a1",
            sport="soccer",
            bankroll_id="paper-bankroll",
            currency="EUR",
        )
        execution_plan = ExecutionPlan(
            plan_id="adoption-plan-canonical-json",
            bookmaker_profile_version="paper-profile-v1",
            decision_id="decision-canonical-json",
            approval_id="paper-only-no-real-money",
            created_at=QUOTE_AT,
            actions=(action("a1"),),
        )
        with self.assertRaisesRegex(
            ValueError,
            "canonical JSON object",
        ):
            PreparedPaperExecution(
                execution_plan=execution_plan,
                exposure_bindings=(binding,),
                intent_evidence_json='{ "schema": "noncanonical" }',
            )
        with self.assertRaisesRegex(
            ValueError,
            "valid canonical JSON",
        ):
            PreparedPaperExecution(
                execution_plan=execution_plan,
                exposure_bindings=(binding,),
                intent_evidence_json="{not-json",
            )

    def test_paper_value_lay_fails_before_execution_or_book_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "LAY PAPER adoption is unavailable",
            ):
                runtime.prepare_paper_value_action(
                    event=market_event("lay"),
                    stake=Decimal("10.00"),
                    decision_id="decision-lay",
                    account_id="paper-account",
                    bankroll_id="paper-bankroll",
                    currency="EUR",
                )

            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_paper_value_back_and_legacy_side_remain_back_compatible(self):
        for exchange_side in ("back", None):
            with self.subTest(exchange_side=exchange_side), tempfile.TemporaryDirectory() as tmp:
                _book, _ledger, runtime = self.runtime(tmp)
                current = runtime.prepare_paper_value_action(
                    event=market_event(exchange_side),
                    stake=Decimal("10.00"),
                    decision_id=f"decision-{exchange_side or 'legacy'}",
                    account_id="paper-account",
                    bankroll_id="paper-bankroll",
                    currency="EUR",
                )
                self.assertEqual(current.execution_plan.actions[0].side, "BACK")

    def test_materializer_rejects_non_back_action_before_attempt_adoption(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, _ledger, runtime = self.runtime(tmp)
            binding = PaperExposureBinding(
                action_id="lay-action",
                sport="soccer",
                bankroll_id="paper-bankroll",
                currency="EUR",
            )
            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "PaperBook materialization supports BACK execution only",
            ):
                runtime._materialize_attempt(
                    attempt=object(),
                    action=action("lay-action", side="LAY"),
                    binding=binding,
                    decision_id="decision-lay",
                )

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
            self.assertEqual(ticket.placed_at, STARTED_AT)
            self.assertEqual(book.balance, __import__("decimal").Decimal("90.00"))

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

    def test_observed_materialized_book_state_is_recoverable_from_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            pre_action_book = PaperBook("100.00")
            current_action = action("a1", odds="2.50", stake="10.00")
            current_prepared = prepared(runtime, current_action)
            registered = evidence(
                current_action,
                PaperAttemptOutcome.ACCEPTED,
                odds="2.25",
                stake="10.00",
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(registered)
            runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-observed-recovery",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={"a1": registered.as_observation()},
                evidence_registry=registry,
            )
            self.assertEqual(len(book.tickets), 1)

            restarted_book = PaperBook.load(Path(tmp) / "paper-book.json")
            restarted = PaperExecutionAdoptionRuntime(
                book=restarted_book,
                ledger=ledger,
                config=runtime.config,
                max_quote_age=runtime.max_quote_age,
                paper_book_path=Path(tmp) / "paper-book.json",
            )
            restarted_prepared = prepared(restarted, current_action)
            restarted.assert_recoverable_book_state(
                pre_action_book=pre_action_book,
                prepared=restarted_prepared,
                trigger_id="trigger-observed-recovery",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )

    def test_exposure_scope_cannot_be_retrofitted_after_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("a1"))
            trigger_id = "trigger-retro-scope"
            run_id = runtime.expected_run_id(
                current_prepared,
                trigger_id,
            )
            ledger.reserve_run(
                run_id=run_id,
                trigger_id=trigger_id,
                plan=current_prepared.execution_plan,
                config=runtime.config,
                started_at=STARTED_AT,
                observation_evidence_ids={},
            )
            event_count = len(ledger.events())

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "cannot be retroactively published",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id=trigger_id,
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )
            self.assertEqual(len(ledger.events()), event_count)
            self.assertEqual(book.tickets, {})

    def test_exposure_scope_publication_serializes_against_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            entered = Event()
            release = Event()

            class PausingLedger(PaperExecutionLedger):
                def __init__(self, ledger_path):
                    super().__init__(ledger_path)
                    self.pause_next_load = False

                def _load_unlocked(self):
                    events = super()._load_unlocked()
                    if self.pause_next_load:
                        self.pause_next_load = False
                        entered.set()
                        if not release.wait(5):
                            raise AssertionError(
                                "timed out waiting to release scope publication"
                            )
                    return events

            ledger = PausingLedger(path)
            book = PaperBook("100.00")
            runtime = PaperExecutionAdoptionRuntime(
                book=book,
                ledger=ledger,
                config=config(),
                max_quote_age=__import__("datetime").timedelta(seconds=5),
                paper_book_path=Path(tmp) / "paper-book.json",
            )
            current_prepared = prepared(runtime, action("a1"))
            trigger_id = "trigger-scope-reservation-race"
            run_id = runtime.expected_run_id(
                current_prepared,
                trigger_id,
            )
            ledger.pause_next_load = True
            failures = []

            def publish_scope():
                try:
                    runtime._publish_exposure_scope(
                        prepared=current_prepared,
                        run_id=run_id,
                    )
                except BaseException as exc:
                    failures.append(exc)

            worker = Thread(target=publish_scope)
            worker.start()
            self.assertTrue(
                entered.wait(2),
                "scope publication did not enter ledger critical section",
            )

            competing = PaperExecutionLedger(path)
            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "writer lock exists",
            ):
                competing.reserve_run(
                    run_id=run_id,
                    trigger_id=trigger_id,
                    plan=current_prepared.execution_plan,
                    config=runtime.config,
                    started_at=STARTED_AT,
                    observation_evidence_ids={},
                )

            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])

            competing.reserve_run(
                run_id=run_id,
                trigger_id=trigger_id,
                plan=current_prepared.execution_plan,
                config=runtime.config,
                started_at=STARTED_AT,
                observation_evidence_ids={},
            )
            self.assertEqual(
                [
                    event["event_type"]
                    for event in ledger.events(run_id)
                ],
                [
                    "PAPER_EXPOSURE_SCOPE_BOUND",
                    "RUN_RESERVED",
                ],
            )

    def test_observed_attempt_restart_recovers_inputs_without_caller_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            book_path = Path(tmp) / "paper-book.json"
            current_action = action("a1", odds="2.50", stake="10.00")
            current_prepared = prepared(runtime, current_action)
            registered = evidence(
                current_action,
                PaperAttemptOutcome.ACCEPTED,
                odds="2.25",
                stake="10.00",
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(registered)
            shadow = runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-observed-crash-window",
                started_at=STARTED_AT,
                materialize_exposure=False,
                observations={"a1": registered.as_observation()},
                evidence_registry=registry,
            )
            self.assertEqual(book.tickets, {})

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
                trigger_id="trigger-observed-crash-window",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )

            self.assertEqual(resumed.run, shadow.run)
            self.assertEqual(len(reloaded_book.tickets), 1)
            ticket = next(iter(reloaded_book.tickets.values()))
            self.assertEqual(ticket.stake, Decimal("10.00"))
            self.assertEqual(
                ticket.legs[0].locked_odds,
                Decimal("2.25"),
            )

    def test_suspended_attempt_restart_recovers_durable_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            book_path = Path(tmp) / "paper-book.json"
            current_action = action("a1")
            current_prepared = prepared(runtime, current_action)
            shadow = runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-suspended-crash-window",
                started_at=STARTED_AT,
                materialize_exposure=False,
                suspended_action_ids=frozenset({"a1"}),
            )
            self.assertTrue(shadow.run.attempts[0].suspended)
            self.assertEqual(book.tickets, {})

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
                trigger_id="trigger-suspended-crash-window",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )

            self.assertEqual(resumed.run, shadow.run)
            self.assertTrue(resumed.run.attempts[0].suspended)
            self.assertEqual(reloaded_book.tickets, {})

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




    def test_same_paper_book_path_shares_execution_lock_across_runtimes(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, first = self.runtime(tmp)
            second = PaperExecutionAdoptionRuntime(
                book=book,
                ledger=ledger,
                config=config(),
                max_quote_age=__import__("datetime").timedelta(seconds=5),
                paper_book_path=Path(tmp) / "." / "paper-book.json",
            )
            self.assertIs(first._execution_lock, second._execution_lock)

    def test_execution_guard_serializes_distinct_runtimes_for_same_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, first = self.runtime(tmp)
            second = PaperExecutionAdoptionRuntime(
                book=book,
                ledger=ledger,
                config=config(),
                max_quote_age=__import__("datetime").timedelta(seconds=5),
                paper_book_path=Path(tmp) / "paper-book.json",
            )
            entered = Event()
            release = Event()
            second_entered = Event()

            def hold_first():
                with first.execution_guard():
                    entered.set()
                    self.assertTrue(release.wait(timeout=2))

            def enter_second():
                with second.execution_guard():
                    second_entered.set()

            first_thread = Thread(target=hold_first)
            second_thread = Thread(target=enter_second)
            first_thread.start()
            self.assertTrue(entered.wait(timeout=2))
            second_thread.start()
            self.assertFalse(second_entered.wait(timeout=0.05))
            release.set()
            first_thread.join(timeout=2)
            second_thread.join(timeout=2)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertTrue(second_entered.is_set())

    def test_different_paper_book_paths_do_not_share_execution_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_book = PaperBook("100.00")
            first = PaperExecutionAdoptionRuntime(
                book=first_book,
                ledger=PaperExecutionLedger(Path(tmp) / "first-execution.jsonl"),
                config=config(),
                max_quote_age=__import__("datetime").timedelta(seconds=5),
                paper_book_path=Path(tmp) / "first-paper-book.json",
            )
            second_book = PaperBook("100.00")
            second = PaperExecutionAdoptionRuntime(
                book=second_book,
                ledger=PaperExecutionLedger(Path(tmp) / "second-execution.jsonl"),
                config=config(),
                max_quote_age=__import__("datetime").timedelta(seconds=5),
                paper_book_path=Path(tmp) / "second-paper-book.json",
            )
            self.assertIsNot(first._execution_lock, second._execution_lock)




    def test_fresh_execution_rejects_paperbook_change_after_prepare(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("a1"))
            trigger_id = "trigger-stale-prepared"
            run_id = runtime.expected_run_id(current_prepared, trigger_id)

            book.open_ticket(
                (
                    TicketLeg(
                        "event-concurrent",
                        "market-concurrent",
                        "selection-concurrent",
                        Decimal("2.00"),
                    ),
                ),
                Decimal("1.00"),
                reason="concurrent paper mutation",
                placed_at=QUOTE_AT,
            )

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "PaperBook changed after execution preparation",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id=trigger_id,
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )

            self.assertEqual(ledger.events(run_id), ())
            self.assertEqual(len(book.tickets), 1)

    def test_fresh_execution_rechecks_paperbook_before_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("a1"))
            trigger_id = "trigger-mid-run-paperbook-race"
            run_id = runtime.expected_run_id(current_prepared, trigger_id)

            import autosport.paper_execution_adoption as adoption_module

            real_execute = adoption_module.execute_paper_plan
            mutated = {"value": False}

            def execute_then_mutate(**kwargs):
                run = real_execute(**kwargs)
                if not mutated["value"]:
                    mutated["value"] = True
                    book.open_ticket(
                        (
                            TicketLeg(
                                "event-concurrent",
                                "market-concurrent",
                                "selection-concurrent",
                                Decimal("2.00"),
                            ),
                        ),
                        Decimal("1.00"),
                        reason="concurrent paper mutation during execution",
                        placed_at=QUOTE_AT,
                    )
                return run

            with patch(
                "autosport.paper_execution_adoption.execute_paper_plan",
                side_effect=execute_then_mutate,
            ):
                with self.assertRaisesRegex(
                    PaperExecutionAdoptionError,
                    "PaperBook changed after execution preparation",
                ):
                    runtime.execute(
                        prepared=current_prepared,
                        trigger_id=trigger_id,
                        started_at=STARTED_AT,
                        materialize_exposure=True,
                    )

            self.assertTrue(mutated["value"])
            self.assertTrue(ledger.events(run_id))
            self.assertEqual(len(book.tickets), 1)
            self.assertNotIn(
                "paper_execution_attempt_id=",
                next(iter(book.tickets.values())).strategy_reason,
            )



    def test_execution_guard_rejects_serialization_authority_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, _ledger, runtime = self.runtime(tmp)
            runtime._execution_lock = RLock()

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "serialization authority changed",
            ):
                with runtime.execution_guard():
                    self.fail("rebound serialization authority must not be entered")

    def test_execution_guard_rejects_quote_age_authority_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, _ledger, runtime = self.runtime(tmp)
            runtime.max_quote_age = __import__("datetime").timedelta(seconds=500)

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "quote-age authority changed",
            ):
                with runtime.execution_guard():
                    self.fail("mutated quote-age authority must not be entered")

    def test_execution_guard_rejects_same_fingerprint_config_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, _ledger, runtime = self.runtime(tmp)
            replacement = config()
            self.assertEqual(replacement.fingerprint, runtime.config.fingerprint)
            runtime.config = replacement

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "model authority changed",
            ):
                with runtime.execution_guard():
                    self.fail("replacement execution model must not be entered")

    def test_execution_guard_rejects_same_path_ledger_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, ledger, runtime = self.runtime(tmp)
            runtime.ledger = PaperExecutionLedger(ledger.path)

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "ledger authority changed",
            ):
                with runtime.execution_guard():
                    self.fail("replacement execution ledger must not be entered")

    def test_execution_guard_rejects_paper_book_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            replacement = PaperBook(str(book.initial_bankroll))
            runtime.book = replacement

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "PaperBook authority changed",
            ):
                with runtime.execution_guard():
                    self.fail("replacement PaperBook must not be entered")



    def test_minted_execution_rejects_in_place_intent_evidence_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, _ledger, runtime = self.runtime(tmp)
            capability = prepared(runtime, action("a1"))
            object.__setattr__(
                capability,
                "intent_evidence_json",
                '{"schema":"tampered-intent-evidence"}',
            )

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "prepared execution semantics changed",
            ):
                runtime.expected_run_id(capability, "trigger-1")

    def test_minted_execution_rejects_in_place_exposure_binding_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, _ledger, runtime = self.runtime(tmp)
            capability = prepared(runtime, action("a1"))
            object.__setattr__(
                capability,
                "exposure_bindings",
                (
                    PaperExposureBinding(
                        action_id="a1",
                        sport="soccer",
                        bankroll_id="other-bankroll",
                        currency="EUR",
                    ),
                ),
            )

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "prepared execution semantics changed",
            ):
                runtime.expected_run_id(capability, "trigger-1")

    def test_minted_execution_rejects_in_place_execution_plan_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, _ledger, runtime = self.runtime(tmp)
            capability = prepared(runtime, action("a1"))
            original = capability.execution_plan
            replacement = ExecutionPlan(
                plan_id=original.plan_id,
                bookmaker_profile_version=original.bookmaker_profile_version,
                decision_id=original.decision_id,
                approval_id=original.approval_id,
                created_at=original.created_at,
                actions=(action("a1", stake="99.00"),),
            )
            object.__setattr__(capability, "execution_plan", replacement)

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "prepared execution semantics changed",
            ):
                runtime.expected_run_id(capability, "trigger-1")



    def test_expected_run_id_rejects_runtime_authority_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, _ledger, runtime = self.runtime(tmp)
            capability = prepared(runtime, action("a1"))
            runtime.config = config()

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "model authority changed",
            ):
                runtime.expected_run_id(capability, "trigger-1")

    def test_recovery_book_check_rejects_runtime_authority_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            capability = prepared(runtime, action("a1"))
            pre_action = PaperBook(str(book.initial_bankroll))
            runtime.max_quote_age = __import__("datetime").timedelta(seconds=500)

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "quote-age authority changed",
            ):
                runtime.assert_recoverable_book_state(
                    pre_action_book=pre_action,
                    prepared=capability,
                    trigger_id="trigger-1",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )



    def test_execution_guard_rejects_ledger_path_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, ledger, runtime = self.runtime(tmp)
            ledger.path = Path(tmp) / "foreign-paper-execution.jsonl"

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "ledger persistence authority changed",
            ):
                with runtime.execution_guard():
                    self.fail("rebound ledger path must not be entered")

    def test_execution_guard_rejects_ledger_writer_lock_path_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, ledger, runtime = self.runtime(tmp)
            ledger._lock_path = Path(tmp) / "foreign-writer.lock"

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "ledger persistence authority changed",
            ):
                with runtime.execution_guard():
                    self.fail("rebound ledger writer lock must not be entered")

    def test_execution_guard_rejects_ledger_anchor_path_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, ledger, runtime = self.runtime(tmp)
            ledger._anchor_path = Path(tmp) / "foreign-anchor.json"

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "ledger persistence authority changed",
            ):
                with runtime.execution_guard():
                    self.fail("rebound ledger anchor must not be entered")

    def test_execution_guard_rejects_ledger_process_lock_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, ledger, runtime = self.runtime(tmp)
            ledger._lock = RLock()

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "ledger persistence authority changed",
            ):
                with runtime.execution_guard():
                    self.fail("rebound ledger process lock must not be entered")



    def test_execution_guard_rejects_relative_persistence_path_cwd_drift(self):
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            try:
                os.chdir(first)
                book = PaperBook("1000")
                ledger = PaperExecutionLedger("paper-execution.jsonl")
                runtime = PaperExecutionAdoptionRuntime(
                    book=book,
                    ledger=ledger,
                    config=config(),
                    max_quote_age=__import__("datetime").timedelta(seconds=5),
                    paper_book_path="paper_book.json",
                )
                os.chdir(second)

                with self.assertRaisesRegex(
                    PaperExecutionAdoptionError,
                    "persistence authority changed|path resolution changed",
                ):
                    with runtime.execution_guard():
                        self.fail("CWD drift must not retarget durable execution authority")
            finally:
                os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()