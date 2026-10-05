from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent
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

    def test_constructor_rejects_timedelta_subclass_before_comparison(self):
        class HostileTimedelta(timedelta):
            def __le__(self, _other):
                raise AssertionError("hostile timedelta comparison must not execute")

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with self.assertRaisesRegex(
                TypeError,
                "max_quote_age must be an exact timedelta",
            ):
                PaperExecutionAdoptionRuntime(
                    book=PaperBook("1000"),
                    ledger=PaperExecutionLedger(workspace / "paper-execution.jsonl"),
                    config=self._config(),
                    max_quote_age=HostileTimedelta(seconds=5),
                    paper_book_path=workspace / "paper_book.json",
                )

    def test_execute_with_clock_rejects_datetime_subclass_before_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            runtime, prepared = self._runtime_and_prepared(workspace)

            class HostileDateTime(datetime):
                def utcoffset(self):
                    raise AssertionError("hostile utcoffset must not execute")

                def astimezone(self, *args, **kwargs):
                    raise AssertionError("hostile astimezone must not execute")

            hostile = HostileDateTime(
                2026,
                9,
                21,
                12,
                0,
                tzinfo=timezone.utc,
            )

            with self.assertRaisesRegex(
                TypeError,
                "PAPER execution clock must return exact datetime",
            ):
                runtime.execute_with_clock(
                    prepared=prepared,
                    trigger_id="trigger-1",
                    clock=lambda: hostile,
                    materialize_exposure=False,
                )

    def test_execute_with_clock_rejects_product_clock_before_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("a1"))
            backwards = datetime.fromisoformat(
                "2026-09-20T05:59:59.999999+00:00"
            )

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "start clock precedes the authorized plan",
            ):
                runtime.execute_with_clock(
                    prepared=current_prepared,
                    trigger_id="trigger-backward-clock",
                    clock=lambda: backwards,
                    materialize_exposure=True,
                )

            self.assertEqual(ledger.events(), ())
            self.assertEqual(book.tickets, {})

    def test_exposure_scope_only_recovery_uses_new_execution_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("a1"))
            trigger_id = "trigger-scope-only-recovery"
            run_id = runtime.expected_run_id(current_prepared, trigger_id)

            # Simulate a crash after scope publication but before #623 reserves
            # the run. Scope evidence is authority for exposure identity, not proof
            # that execution already started.
            runtime._publish_exposure_scope(
                prepared=current_prepared,
                run_id=run_id,
            )
            pre_recovery_events = ledger.events(run_id)
            self.assertEqual(
                [event["event_type"] for event in pre_recovery_events],
                [runtime._EXPOSURE_SCOPE_EVENT_TYPE],
            )

            recovery_time = datetime.fromisoformat(
                "2026-09-20T06:01:00+00:00"
            )
            result = runtime.execute_with_clock(
                prepared=current_prepared,
                trigger_id=trigger_id,
                clock=lambda: recovery_time,
                materialize_exposure=True,
            )

            self.assertEqual(result.run.started_at, recovery_time.isoformat())
            self.assertEqual(
                result.run.attempts[0].outcome,
                PaperAttemptOutcome.REJECTED,
            )
            self.assertIn("expired", result.run.attempts[0].reason)
            self.assertEqual(book.tickets, {})
            reservations = [
                event
                for event in ledger.events(run_id)
                if event["event_type"] == "RUN_RESERVED"
            ]
            self.assertEqual(len(reservations), 1)
            self.assertEqual(
                reservations[0]["payload"]["started_at"],
                recovery_time.isoformat(),
            )

    def test_execute_with_clock_ignores_rebound_durable_loader_for_start_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("a1"))
            trigger_id = "trigger-rebound-durable-loader"
            run_id = runtime.expected_run_id(current_prepared, trigger_id)
            real_load = ledger._load_unlocked
            forged_calls = [0]

            fake_reservation = {
                "event_type": "RUN_RESERVED",
                "run_id": run_id,
                "payload": {
                    "trigger_id": trigger_id,
                    "plan_id": current_prepared.execution_plan.plan_id,
                    "plan_fingerprint": current_prepared.execution_plan.fingerprint,
                    "model_fingerprint": runtime.config.fingerprint,
                    "started_at": STARTED_AT,
                    "action_ids": [
                        item.action_id
                        for item in current_prepared.execution_plan.actions
                    ],
                    "observation_evidence_ids": {},
                },
            }

            def forged_load():
                forged_calls[0] += 1
                if forged_calls[0] == 1:
                    return [fake_reservation]
                return real_load()

            ledger._load_unlocked = forged_load
            actual_execution_time = datetime.fromisoformat(
                "2026-09-20T06:01:00+00:00"
            )

            result = runtime.execute_with_clock(
                prepared=current_prepared,
                trigger_id=trigger_id,
                clock=lambda: actual_execution_time,
                materialize_exposure=True,
            )

            self.assertEqual(forged_calls[0], 0)
            self.assertEqual(
                result.run.started_at,
                actual_execution_time.isoformat(),
            )
            self.assertEqual(
                result.run.attempts[0].outcome,
                PaperAttemptOutcome.REJECTED,
            )
            self.assertIn("expired", result.run.attempts[0].reason)
            self.assertEqual(book.tickets, {})

    def test_execute_with_clock_ignores_rebound_ledger_reader_for_start_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("a1"))
            trigger_id = "trigger-rebound-start-reader"
            run_id = runtime.expected_run_id(current_prepared, trigger_id)
            real_events = ledger.events

            fake_reservation = {
                "event_type": "RUN_RESERVED",
                "payload": {
                    "trigger_id": trigger_id,
                    "plan_id": current_prepared.execution_plan.plan_id,
                    "plan_fingerprint": current_prepared.execution_plan.fingerprint,
                    "model_fingerprint": runtime.config.fingerprint,
                    "started_at": STARTED_AT,
                    "action_ids": [
                        item.action_id
                        for item in current_prepared.execution_plan.actions
                    ],
                    "observation_evidence_ids": {},
                },
            }

            def forged_events(requested_run_id=None):
                durable = real_events(requested_run_id)
                # The adversarial reader lies only while no canonical event exists.
                # Once execution publishes its real scope/reservation, downstream
                # ledger validation sees the genuine durable history.
                if durable:
                    return durable
                return (fake_reservation,)

            ledger.events = forged_events
            actual_execution_time = datetime.fromisoformat(
                "2026-09-20T06:01:00+00:00"
            )

            result = runtime.execute_with_clock(
                prepared=current_prepared,
                trigger_id=trigger_id,
                clock=lambda: actual_execution_time,
                materialize_exposure=True,
            )

            self.assertEqual(
                result.run.started_at,
                actual_execution_time.isoformat(),
            )
            self.assertEqual(
                result.run.attempts[0].outcome,
                PaperAttemptOutcome.REJECTED,
            )
            self.assertIn("expired", result.run.attempts[0].reason)
            self.assertEqual(book.tickets, {})
            reservations = [
                event
                for event in real_events(run_id)
                if event["event_type"] == "RUN_RESERVED"
            ]
            self.assertEqual(len(reservations), 1)
            self.assertEqual(
                reservations[0]["payload"]["started_at"],
                actual_execution_time.isoformat(),
            )

    def test_execute_with_clock_ignores_instance_shadowed_start_resolver(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("a1"))
            runtime.resolve_execution_started_at = (
                lambda **_kwargs: STARTED_AT
            )
            actual_execution_time = datetime.fromisoformat(
                "2026-09-20T06:01:00+00:00"
            )

            result = runtime.execute_with_clock(
                prepared=current_prepared,
                trigger_id="trigger-shadowed-start-resolver",
                clock=lambda: actual_execution_time,
                materialize_exposure=True,
            )

            self.assertEqual(
                result.run.started_at,
                actual_execution_time.isoformat(),
            )
            self.assertEqual(
                result.run.attempts[0].outcome,
                PaperAttemptOutcome.REJECTED,
            )
            self.assertIn("expired", result.run.attempts[0].reason)
            self.assertEqual(book.tickets, {})
            reservations = [
                event
                for event in ledger.events()
                if event["event_type"] == "RUN_RESERVED"
            ]
            self.assertEqual(len(reservations), 1)
            self.assertEqual(
                reservations[0]["payload"]["started_at"],
                actual_execution_time.isoformat(),
            )

    def test_execute_with_clock_ignores_instance_shadowed_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, _ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("a1"))
            forged_result = object()
            runtime.execute = lambda **_kwargs: forged_result
            actual_execution_time = datetime.fromisoformat(STARTED_AT)

            result = runtime.execute_with_clock(
                prepared=current_prepared,
                trigger_id="trigger-shadowed-execute",
                clock=lambda: actual_execution_time,
                materialize_exposure=False,
            )

            self.assertIsNot(result, forged_result)
            self.assertEqual(result.run.started_at, STARTED_AT)
            self.assertTrue(result.run.completed)

    def test_execute_with_clock_samples_after_execution_lock_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("a1"))
            clock_value = [
                datetime.fromisoformat("2026-09-20T06:00:00.100000+00:00")
            ]

            class _AdvanceClockOnEnter:
                def __enter__(self):
                    clock_value[0] = datetime.fromisoformat(
                        "2026-09-20T06:01:00+00:00"
                    )
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

            runtime._execution_lock = _AdvanceClockOnEnter()
            result = runtime.execute_with_clock(
                prepared=current_prepared,
                trigger_id="trigger-lock-clock",
                clock=lambda: clock_value[0],
                materialize_exposure=True,
            )

            self.assertEqual(
                result.run.started_at,
                "2026-09-20T06:01:00+00:00",
            )
            self.assertEqual(
                result.run.attempts[0].outcome,
                PaperAttemptOutcome.REJECTED,
            )
            self.assertIn("expired", result.run.attempts[0].reason)
            self.assertEqual(book.tickets, {})
            reservations = [
                event
                for event in ledger.events()
                if event["event_type"] == "RUN_RESERVED"
            ]
            self.assertEqual(
                reservations[0]["payload"]["started_at"],
                "2026-09-20T06:01:00+00:00",
            )

    def test_execute_with_clock_reuses_reserved_start_with_configured_observation(self):
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
            clock_value = [datetime.fromisoformat(STARTED_AT)]

            first = runtime.execute_with_clock(
                prepared=current_prepared,
                trigger_id="trigger-clock-observed-restart",
                clock=lambda: clock_value[0],
                materialize_exposure=True,
                observations={"a1": registered.as_observation()},
                evidence_registry=registry,
            )
            clock_value[0] = datetime.fromisoformat(
                "2026-09-20T06:00:00.200000+00:00"
            )
            second = runtime.execute_with_clock(
                prepared=current_prepared,
                trigger_id="trigger-clock-observed-restart",
                clock=lambda: clock_value[0],
                materialize_exposure=True,
                observations={"a1": registered.as_observation()},
                evidence_registry=registry,
            )

            self.assertEqual(first.run, second.run)
            self.assertEqual(second.run.started_at, STARTED_AT)
            self.assertEqual(first.ticket_ids, second.ticket_ids)
            self.assertEqual(len(book.tickets), 1)

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