import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.agents import AgentContext
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import MarketEvent, TicketLeg, TicketStatus
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.paper_strategy import Forecast, PaperValueAgent
from autosport.risk import PaperRiskPolicy


class EconomicGoalEndogenousStakeTests(unittest.TestCase):
    @staticmethod
    def _event(
        *,
        event_id: str = "event-1",
        market_id: str = "market-1",
        selection_id: str = "selection-1",
        sequence: int = 1,
    ) -> MarketEvent:
        return MarketEvent(
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            decimal_odds=Decimal("2"),
            observed_ts="2026-09-17T15:00:00+00:00",
            source_id="provider-1",
            sequence=sequence,
            source_ts="2026-09-17T14:59:59+00:00",
            ingest_ts="2026-09-17T15:00:00+00:00",
        )

    @staticmethod
    def _goal(**overrides: object) -> EconomicGoalContract:
        values: dict[str, object] = {
            "goal_id": "goal-endogenous-stake",
            "revision": 1,
            "bankroll_id": "paper-bankroll",
            "currency": "USD",
            "max_stake_fraction": Decimal("0.02"),
            "max_session_loss_fraction": Decimal("1"),
            "max_day_loss_fraction": Decimal("1"),
            "max_drawdown_fraction": Decimal("1"),
            "max_capital_at_risk_fraction": Decimal("0.20"),
            "max_turnover_fraction": Decimal("1000"),
            "max_risk_of_ruin": Decimal("1"),
            "max_concurrent_positions": 10,
        }
        values.update(overrides)
        return EconomicGoalContract(**values)  # type: ignore[arg-type]

    @staticmethod
    def _policy(goal: EconomicGoalContract) -> PaperRiskPolicy:
        return PaperRiskPolicy(
            max_ticket_fraction=Decimal("1"),
            max_committed_fraction=Decimal("1"),
            minimum_cash_reserve_fraction=Decimal("0"),
            economic_goal=goal,
        )

    @staticmethod
    def _agent(event: MarketEvent, policy: PaperRiskPolicy) -> PaperValueAgent:
        return PaperValueAgent(
            {
                event.quote_key: Forecast(
                    quote_key=event.quote_key,
                    probability=Decimal("0.60"),
                    model_id="test-model",
                    as_of_ts="2026-09-17T14:59:58+00:00",
                )
            },
            stake=Decimal("999"),
            risk_policy=policy,
        )

    @staticmethod
    def _open_tickets(book: PaperBook):
        return tuple(
            ticket
            for ticket in book.tickets.values()
            if ticket.status is TicketStatus.OPEN
        )

    def test_active_economic_goal_removes_fixed_caller_stake_authority(self) -> None:
        event = self._event()
        goal = self._goal()
        book = PaperBook("1000")
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlDecisionLedger(Path(tmp) / "decisions.jsonl")
            context = AgentContext(
                book,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-endogenous-stake",
                decision_ledger=ledger,
            )

            self._agent(event, self._policy(goal)).on_market_event(event, context)

            open_tickets = self._open_tickets(book)
            self.assertEqual(len(open_tickets), 1)
            ticket = open_tickets[0]
            self.assertEqual(ticket.stake, Decimal("20.00"))
            self.assertNotEqual(ticket.stake, Decimal("999"))
            records = ledger.verified_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].payload["stake"], "20.00")

    def test_existing_open_exposure_tightens_next_goal_derived_stake_and_restart_is_idempotent(self) -> None:
        event = self._event(event_id="event-2", market_id="market-2", sequence=2)
        goal = self._goal(
            max_stake_fraction=Decimal("0.20"),
            max_capital_at_risk_fraction=Decimal("0.25"),
        )
        book = PaperBook("100")
        book.open_ticket(
            [TicketLeg("existing-event", "existing-market", "existing-selection", Decimal("2"))],
            Decimal("20"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "decisions.jsonl"
            book_path = root / "paper.json"
            ledger = JsonlDecisionLedger(ledger_path)
            context = AgentContext(
                book,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-exposure-aware-stake",
                decision_ledger=ledger,
            )

            self._agent(event, self._policy(goal)).on_market_event(event, context)

            open_tickets = self._open_tickets(book)
            self.assertEqual(len(open_tickets), 2)
            new_ticket = next(
                ticket
                for ticket in open_tickets
                if ticket.legs[0].event_id == event.event_id
            )
            self.assertEqual(new_ticket.stake, Decimal("5.00"))
            self.assertEqual(book.committed_stake, Decimal("25.00"))
            book.save(book_path)

            restarted_book = PaperBook.load(book_path)
            restarted_context = AgentContext(
                restarted_book,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-exposure-aware-stake",
                decision_ledger=JsonlDecisionLedger(ledger_path),
            )
            self._agent(event, self._policy(goal)).on_market_event(event, restarted_context)

            self.assertEqual(len(self._open_tickets(restarted_book)), 2)
            self.assertEqual(restarted_book.committed_stake, Decimal("25.00"))
            self.assertEqual(len(restarted_context.decision_ledger.verified_records()), 1)

    def test_exhausted_goal_exposure_returns_zero_by_opening_nothing(self) -> None:
        event = self._event(event_id="event-3", market_id="market-3", sequence=3)
        goal = self._goal(
            max_stake_fraction=Decimal("0.20"),
            max_capital_at_risk_fraction=Decimal("0.20"),
        )
        book = PaperBook("100")
        existing = book.open_ticket(
            [TicketLeg("existing-event", "existing-market", "existing-selection", Decimal("2"))],
            Decimal("20"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlDecisionLedger(Path(tmp) / "decisions.jsonl")
            context = AgentContext(
                book,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-zero-stake",
                decision_ledger=ledger,
            )

            self._agent(event, self._policy(goal)).on_market_event(event, context)

            self.assertEqual(self._open_tickets(book), (existing,))
            self.assertEqual(book.committed_stake, Decimal("20"))
            self.assertFalse((Path(tmp) / "decisions.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
