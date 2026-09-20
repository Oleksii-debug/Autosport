import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from autosport.agents import AgentContext
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import MarketEvent
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import PaperExecutionAdoptionRuntime
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
from autosport.paper_strategy import (
    Forecast,
    PaperDecisionReconciliationRequired,
    PaperValueAgent,
)
from autosport.risk import PaperRiskPolicy


class PaperValueEconomicGoalIntegrationTests(unittest.TestCase):
    @staticmethod
    def _goal() -> EconomicGoalContract:
        return EconomicGoalContract(
            goal_id="goal-paper-value",
            revision=1,
            bankroll_id="paper-bankroll",
            currency="USD",
            max_stake_fraction=Decimal("0.10"),
            max_capital_at_risk_fraction=Decimal("0.50"),
            max_risk_of_ruin=Decimal("1"),
            max_concurrent_positions=2,
            max_quote_age_seconds=Decimal("5"),
            minimum_data_quality=Decimal("0"),
        )

    @staticmethod
    def _event(*, source_ts: str = "2026-09-17T14:59:59+00:00") -> MarketEvent:
        return MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            decimal_odds=Decimal("2"),
            observed_ts="2026-09-17T15:00:00+00:00",
            source_id="provider-1",
            sequence=1,
            source_ts=source_ts,
            ingest_ts="2026-09-17T15:00:00+00:00",
        )

    @staticmethod
    def _agent(event: MarketEvent, goal: EconomicGoalContract) -> PaperValueAgent:
        forecast = Forecast(
            quote_key=event.quote_key,
            probability=Decimal("0.60"),
            model_id="model-1",
            as_of_ts="2026-09-17T14:59:58+00:00",
        )
        return PaperValueAgent(
            {event.quote_key: forecast},
            stake=Decimal("1"),
            risk_policy=PaperRiskPolicy(economic_goal=goal),
        )

    @staticmethod
    def _config() -> PaperExecutionModelConfig:
        return PaperExecutionModelConfig(
            model_id="paper-value-test",
            model_version="1",
            evidence_grade=EvidenceGrade.SYNTHETIC,
            evidence_source="paper-value-test",
            seed="paper-value-economic-goal",
            max_quote_age_ms=5_000,
            min_delay_ms=0,
            max_delay_ms=0,
            rejected_bps=0,
            partial_bps=0,
            unknown_bps=0,
            partial_fill_bps=5000,
            max_slippage_bps=0,
        )

    def _context(
        self,
        root: Path,
        event: MarketEvent,
        *,
        book: PaperBook | None = None,
        ledger_cls=JsonlDecisionLedger,
    ) -> tuple[AgentContext, PaperExecutionLedger]:
        current_book = book or PaperBook("100")
        execution_ledger = PaperExecutionLedger(root / "paper-execution.jsonl")
        runtime = PaperExecutionAdoptionRuntime(
            book=current_book,
            ledger=execution_ledger,
            config=self._config(),
            max_quote_age=timedelta(seconds=5),
            paper_book_path=root / "paper_book.json",
        )
        context = AgentContext(
            current_book,
            latest_quotes={event.quote_key: event},
            replay_run_id="run-1",
            decision_ledger=ledger_cls(root / "decisions.jsonl"),
            paper_execution=runtime,
            paper_provider_accounts=((event.source_id, "paper-account"),),
        )
        return context, execution_ledger

    def test_active_goal_materializes_only_after_canonical_execution_attempt(self) -> None:
        goal = self._goal()
        event = self._event()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, execution_ledger = self._context(root, event)

            self._agent(event, goal).on_market_event(event, context)

            self.assertEqual(len(context.paper_book.tickets), 1)
            self.assertEqual(context.paper_book.balance, Decimal("98"))
            records = context.decision_ledger.verified_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].action, "OPEN_PAPER_VALUE_TICKET")
            self.assertNotIn("ticket_id", records[0].payload)
            run_id = records[0].payload["execution_run_id"]
            run_events = execution_ledger.events(run_id)
            self.assertEqual(
                tuple(item["event_type"] for item in run_events),
                ("RUN_RESERVED", "ATTEMPT_RECORDED", "RUN_COMPLETED"),
            )
            ticket = next(iter(context.paper_book.tickets.values()))
            self.assertIn("paper_execution_attempt_id=", ticket.strategy_reason)
            attempt = next(
                item for item in run_events if item["event_type"] == "ATTEMPT_RECORDED"
            )
            self.assertEqual(str(ticket.stake), attempt["payload"]["execution_stake"])
            self.assertEqual(
                str(ticket.legs[0].locked_odds),
                attempt["payload"]["execution_odds"],
            )

    def test_restart_redelivery_reuses_same_decision_attempt_and_ticket(self) -> None:
        goal = self._goal()
        event = self._event()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, execution_ledger = self._context(root, event)
            self._agent(event, goal).on_market_event(event, context)
            first_ticket_id = next(iter(context.paper_book.tickets))
            first_record = context.decision_ledger.verified_records()[0]
            first_event_count = len(execution_ledger.events())

            restarted_book = PaperBook.load(root / "paper_book.json")
            restarted_context, restarted_execution = self._context(
                root,
                event,
                book=restarted_book,
            )
            self._agent(event, goal).on_market_event(event, restarted_context)

            self.assertEqual(restarted_book.balance, Decimal("98"))
            self.assertEqual(tuple(restarted_book.tickets), (first_ticket_id,))
            self.assertEqual(restarted_context.decision_ledger.verified_records(), (first_record,))
            self.assertEqual(len(restarted_execution.events()), first_event_count)

    def test_missing_execution_authority_cannot_open_ticket(self) -> None:
        goal = self._goal()
        event = self._event()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlDecisionLedger(Path(tmp) / "decisions.jsonl")
            context = AgentContext(
                PaperBook("100"),
                latest_quotes={event.quote_key: event},
                replay_run_id="run-1",
                decision_ledger=ledger,
            )

            self._agent(event, goal).on_market_event(event, context)

            self.assertEqual(context.paper_book.balance, Decimal("100"))
            self.assertEqual(context.paper_book.tickets, {})
            self.assertFalse(ledger.path.exists())
            self.assertTrue(
                any("canonical #623 execution" in note for note in context.notes)
            )

    def test_missing_provider_account_authority_cannot_open_ticket(self) -> None:
        goal = self._goal()
        event = self._event()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book = PaperBook("100")
            execution_ledger = PaperExecutionLedger(root / "paper-execution.jsonl")
            runtime = PaperExecutionAdoptionRuntime(
                book=book,
                ledger=execution_ledger,
                config=self._config(),
                max_quote_age=timedelta(seconds=5),
                paper_book_path=root / "paper_book.json",
            )
            context = AgentContext(
                book,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-1",
                decision_ledger=JsonlDecisionLedger(root / "decisions.jsonl"),
                paper_execution=runtime,
            )

            self._agent(event, goal).on_market_event(event, context)

            self.assertEqual(book.balance, Decimal("100"))
            self.assertEqual(book.tickets, {})
            self.assertEqual(execution_ledger.events(), ())

    def test_deleted_decision_history_cannot_reprice_existing_execution(self) -> None:
        goal = self._goal()
        event = self._event()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, _ = self._context(root, event)
            self._agent(event, goal).on_market_event(event, context)
            (root / "decisions.jsonl").unlink()

            restarted_book = PaperBook.load(root / "paper_book.json")
            restarted_context, restarted_execution = self._context(
                root,
                event,
                book=restarted_book,
            )
            before = len(restarted_execution.events())
            with self.assertRaisesRegex(
                PaperDecisionReconciliationRequired,
                "#623 execution history exists without",
            ):
                self._agent(event, goal).on_market_event(event, restarted_context)

            self.assertEqual(len(restarted_execution.events()), before)
            self.assertEqual(restarted_book.balance, Decimal("98"))
            self.assertEqual(len(restarted_book.tickets), 1)

    def test_economic_decision_write_failure_happens_before_execution_mutation(self) -> None:
        class FailingEconomicLedger(JsonlDecisionLedger):
            def append_economic(self, record, authority):
                raise OSError("injected economic-ledger write failure")

        goal = self._goal()
        event = self._event()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, execution_ledger = self._context(
                root,
                event,
                ledger_cls=FailingEconomicLedger,
            )

            with self.assertRaisesRegex(OSError, "injected economic-ledger write failure"):
                self._agent(event, goal).on_market_event(event, context)

            self.assertEqual(context.paper_book.balance, Decimal("100"))
            self.assertEqual(context.paper_book.tickets, {})
            self.assertEqual(execution_ledger.events(), ())

    def test_post_write_uncertainty_reuses_verified_decision_then_executes_once(self) -> None:
        class RaiseAfterCommitLedger(JsonlDecisionLedger):
            def append_economic(self, record, authority):
                super().append_economic(record, authority)
                raise OSError("injected post-write fsync uncertainty")

        goal = self._goal()
        event = self._event()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, execution_ledger = self._context(
                root,
                event,
                ledger_cls=RaiseAfterCommitLedger,
            )
            agent = self._agent(event, goal)

            agent.on_market_event(event, context)
            agent.on_market_event(event, context)

            self.assertEqual(context.paper_book.balance, Decimal("98"))
            self.assertEqual(len(context.paper_book.tickets), 1)
            self.assertEqual(len(context.decision_ledger.verified_records()), 1)
            self.assertEqual(
                sum(
                    item["event_type"] == "ATTEMPT_RECORDED"
                    for item in execution_ledger.events()
                ),
                1,
            )

    def test_active_goal_stale_quote_never_creates_decision_attempt_or_ticket(self) -> None:
        goal = self._goal()
        event = self._event(source_ts="2026-09-17T14:59:50+00:00")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, execution_ledger = self._context(root, event)

            self._agent(event, goal).on_market_event(event, context)

            self.assertEqual(context.paper_book.tickets, {})
            self.assertEqual(execution_ledger.events(), ())
            self.assertFalse((root / "decisions.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
