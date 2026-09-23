import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path

from autosport.agents import AgentContext
from autosport.decision_ledger import (
    ECONOMIC_DECISION_KIND,
    DecisionRecord,
    JsonlDecisionLedger,
)
from autosport.domain import MarketEvent
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import PaperExecutionAdoptionError
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
    def _assert_decision_binds_ticket(record: DecisionRecord, book: PaperBook) -> None:
        ticket = next(iter(book.tickets.values()))
        material_action_id = record.payload["material_action_id"]
        assert isinstance(material_action_id, str) and material_action_id
        assert f"decision_id={material_action_id}" in ticket.strategy_reason
        assert isinstance(record.payload["execution_run_id"], str)
        assert record.payload["execution_run_id"]

    def test_active_goal_allows_fresh_proposal_and_persists_restart_verifiable_provenance(self) -> None:
        goal = self._goal()
        event = self._event()
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(ledger_path)
            context = AgentContext(
                PaperBook("100"),
                latest_quotes={event.quote_key: event},
                replay_run_id="run-1",
                decision_ledger=ledger,
            )

            self._agent(event, goal).on_market_event(event, context)

            self.assertEqual(len(context.paper_book.tickets), 1)
            records = ledger.verified_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].action, "OPEN_PAPER_VALUE_TICKET")
            self.assertRegex(records[0].payload["material_action_id"], r"^[0-9a-f]{64}$")
            self._assert_decision_binds_ticket(records[0], context.paper_book)

            restarted = JsonlDecisionLedger(ledger_path)
            rebound = restarted.verified_economic_decision(records[0].decision_id, goal)
            self.assertEqual(rebound.decision_id, records[0].decision_id)
            self.assertEqual(
                rebound.payload["economic_goal_provenance"]["goal_id"],
                goal.goal_id,
            )
            self.assertEqual(
                rebound.payload["economic_goal_provenance"]["revision"],
                goal.revision,
            )

    def test_restart_redelivery_is_idempotent_when_book_and_ledger_are_both_durable(self) -> None:
        goal = self._goal()
        event = self._event()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "decisions.jsonl"
            book_path = root / "paper.json"
            book = PaperBook("100")
            context = AgentContext(
                book,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-1",
                decision_ledger=JsonlDecisionLedger(ledger_path),
            )
            self._agent(event, goal).on_market_event(event, context)
            book.save(book_path)

            first_ticket_id = next(iter(book.tickets))
            first_action_id = context.decision_ledger.verified_records()[0].payload[
                "material_action_id"
            ]
            restarted_book = PaperBook.load(book_path)
            restarted_ledger = JsonlDecisionLedger(ledger_path)
            restarted_context = AgentContext(
                restarted_book,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-1",
                decision_ledger=restarted_ledger,
            )

            self._agent(event, goal).on_market_event(event, restarted_context)

            self.assertEqual(restarted_book.balance, Decimal("98"))
            self.assertEqual(tuple(restarted_book.tickets), (first_ticket_id,))
            records = restarted_ledger.verified_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].payload["material_action_id"], first_action_id)

    def test_restart_ledger_only_commit_fails_closed_without_second_material_action(self) -> None:
        goal = self._goal()
        event = self._event()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "decisions.jsonl"
            pristine_book_path = root / "paper-before-action.json"
            pristine = PaperBook("100")
            pristine.save(pristine_book_path)
            live_context = AgentContext(
                pristine,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-1",
                decision_ledger=JsonlDecisionLedger(ledger_path),
            )
            self._agent(event, goal).on_market_event(event, live_context)
            self.assertEqual(len(live_context.decision_ledger.verified_records()), 1)

            # #623 publishes its canonical PaperBook snapshot with accepted execution
            # truth. A caller that restarts from an older application-level snapshot
            # must therefore fail closed before an AgentContext can bind a runtime to
            # that stale book.
            restarted_book = PaperBook.load(pristine_book_path)
            restarted_ledger = JsonlDecisionLedger(ledger_path)
            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "configured PaperBook does not match durable snapshot",
            ):
                AgentContext(
                    restarted_book,
                    latest_quotes={event.quote_key: event},
                    replay_run_id="run-1",
                    decision_ledger=restarted_ledger,
                )

            self.assertEqual(restarted_book.balance, Decimal("100"))
            self.assertEqual(restarted_book.tickets, {})
            self.assertEqual(len(restarted_ledger.verified_records()), 1)

    def test_restart_paper_only_commit_fails_closed_without_second_material_action(self) -> None:
        goal = self._goal()
        event = self._event()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "decisions.jsonl"
            book_path = root / "paper-after-action.json"
            book = PaperBook("100")
            context = AgentContext(
                book,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-1",
                decision_ledger=JsonlDecisionLedger(ledger_path),
            )
            self._agent(event, goal).on_market_event(event, context)
            book.save(book_path)
            ledger_path.unlink()

            # Simulate the converse split boundary: #623 attempt/exposure truth and
            # the PaperBook snapshot survived, but the decision append did not.
            # Restart must fail closed instead of treating the durable execution as a
            # fresh opportunity and opening a duplicate ticket.
            restarted_book = PaperBook.load(book_path)
            restarted_ledger = JsonlDecisionLedger(ledger_path)
            restarted_context = AgentContext(
                restarted_book,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-1",
                decision_ledger=restarted_ledger,
            )

            with self.assertRaisesRegex(
                PaperDecisionReconciliationRequired,
                "#623 execution history exists without its durable paper-value decision",
            ):
                self._agent(event, goal).on_market_event(event, restarted_context)

            self.assertEqual(restarted_book.balance, Decimal("98"))
            self.assertEqual(len(restarted_book.tickets), 1)
            self.assertFalse(ledger_path.exists())

    def test_active_goal_fails_closed_on_stale_quote_before_ticket_or_decision(self) -> None:
        goal = self._goal()
        event = self._event(source_ts="2026-09-17T14:59:50+00:00")
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(ledger_path)
            context = AgentContext(
                PaperBook("100"),
                latest_quotes={event.quote_key: event},
                replay_run_id="run-1",
                decision_ledger=ledger,
            )

            self._agent(event, goal).on_market_event(event, context)

            self.assertEqual(len(context.paper_book.tickets), 0)
            self.assertFalse(ledger_path.exists())

    def test_active_goal_without_decision_ledger_fails_closed_and_remains_retryable(self) -> None:
        goal = self._goal()
        event = self._event()
        agent = self._agent(event, goal)
        with tempfile.TemporaryDirectory() as tmp:
            book = PaperBook("100")
            context = AgentContext(
                book,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-1",
                decision_ledger=None,
            )
            self.assertIsNotNone(context.paper_execution)
            ledger_path = context.paper_execution.paper_book_path.parent / "decisions.jsonl"

            agent.on_market_event(event, context)

            self.assertEqual(book.balance, Decimal("100"))
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.committed_stake, Decimal("0"))
            self.assertFalse(ledger_path.exists())

            ledger = JsonlDecisionLedger(ledger_path)
            context.decision_ledger = ledger
            agent.on_market_event(event, context)

            self.assertEqual(book.balance, Decimal("98"))
            self.assertEqual(len(book.tickets), 1)
            records = ledger.verified_records()
            self.assertEqual(len(records), 1)
            self._assert_decision_binds_ticket(records[0], book)
            restarted = JsonlDecisionLedger(ledger_path)
            rebound = restarted.verified_economic_decision(records[0].decision_id, goal)
            self.assertEqual(rebound, records[0])

    def test_economic_ledger_failure_leaves_no_ticket_balance_or_acted_residue(self) -> None:
        class FailingEconomicLedger(JsonlDecisionLedger):
            def append_economic(self, record, contract):
                raise OSError("injected economic-ledger write failure")

        goal = self._goal()
        event = self._event()
        agent = self._agent(event, goal)
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "decisions.jsonl"
            book = PaperBook("100")
            context = AgentContext(
                book,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-1",
                decision_ledger=FailingEconomicLedger(ledger_path),
            )

            with self.assertRaisesRegex(OSError, "injected economic-ledger write failure"):
                agent.on_market_event(event, context)

            self.assertEqual(book.balance, Decimal("100"))
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.committed_stake, Decimal("0"))
            self.assertFalse(ledger_path.exists())

            # The same agent/event must remain retryable: a failed ledger write must
            # not leak into the strategy's duplicate-action suppression state.
            ledger = JsonlDecisionLedger(ledger_path)
            context.decision_ledger = ledger
            agent.on_market_event(event, context)

            self.assertEqual(book.balance, Decimal("98"))
            self.assertEqual(len(book.tickets), 1)
            records = ledger.verified_records()
            self.assertEqual(len(records), 1)
            self._assert_decision_binds_ticket(records[0], book)

    def test_economic_ledger_post_write_error_keeps_verified_ticket_commit(self) -> None:
        original_append_economic = JsonlDecisionLedger.append_economic

        def raise_after_commit(ledger, record, contract):
            original_append_economic(ledger, record, contract)
            raise OSError("injected post-write fsync uncertainty")

        goal = self._goal()
        event = self._event()
        agent = self._agent(event, goal)
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(ledger_path)
            book = PaperBook("100")
            context = AgentContext(
                book,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-1",
                decision_ledger=ledger,
            )

            # Preserve the exact ledger authority required by the execution-origin
            # fence while injecting the same post-write uncertainty at the method
            # boundary. This keeps the test focused on durable readback recovery.
            with patch.object(
                JsonlDecisionLedger,
                "append_economic",
                new=raise_after_commit,
            ):
                # The append reports an uncertain error only after durable bytes exist.
                # Verified readback resolves the transaction as committed, so the paper
                # ticket remains and the synthetic transport error is not surfaced.
                agent.on_market_event(event, context)

                self.assertEqual(book.balance, Decimal("98"))
                self.assertEqual(len(book.tickets), 1)
                records = ledger.verified_records()
                self.assertEqual(len(records), 1)
                self._assert_decision_binds_ticket(records[0], book)

                # _acted must agree with the proven durable commit and prevent a duplicate
                # position if the same event is delivered again in the same process.
                agent.on_market_event(event, context)
                self.assertEqual(book.balance, Decimal("98"))
                self.assertEqual(len(book.tickets), 1)
                self.assertEqual(len(ledger.verified_records()), 1)

    def test_post_write_same_id_different_decision_fails_closed(self) -> None:
        class WrongDecisionAfterCommitLedger(JsonlDecisionLedger):
            def append_economic(self, record, contract):
                wrong = DecisionRecord(
                    replay_run_id=record.replay_run_id,
                    agent=record.agent,
                    observed_ts=record.observed_ts,
                    action="ALTERED_MATERIAL_ACTION",
                    payload=dict(record.payload),
                    context_hash=record.context_hash,
                    decision_id=record.decision_id,
                    recorded_at=record.recorded_at,
                    decision_kind=ECONOMIC_DECISION_KIND,
                )
                super().append_economic(wrong, contract)
                raise OSError("injected mismatched durable decision")

        goal = self._goal()
        event = self._event()
        agent = self._agent(event, goal)
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "decisions.jsonl"
            ledger = WrongDecisionAfterCommitLedger(ledger_path)
            book = PaperBook("100")
            context = AgentContext(
                book,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-1",
                decision_ledger=ledger,
            )

            with self.assertRaisesRegex(OSError, "injected mismatched durable decision"):
                agent.on_market_event(event, context)

            self.assertEqual(book.balance, Decimal("100"))
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.committed_stake, Decimal("0"))
            records = ledger.verified_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].action, "ALTERED_MATERIAL_ACTION")


if __name__ == "__main__":
    unittest.main()