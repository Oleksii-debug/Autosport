import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.agents import AgentContext
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import MarketEvent
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.paper_strategy import Forecast, PaperValueAgent
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

    def test_active_goal_fails_closed_on_stale_quote_before_ticket_or_decision(self) -> None:
        goal = self._goal()
        event = self._event(source_ts="2026-09-17T14:59:50+00:00")
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlDecisionLedger(Path(tmp) / "decisions.jsonl")
            context = AgentContext(
                PaperBook("100"),
                latest_quotes={event.quote_key: event},
                replay_run_id="run-1",
                decision_ledger=ledger,
            )

            self._agent(event, goal).on_market_event(event, context)

            self.assertEqual(len(context.paper_book.tickets), 0)
            self.assertEqual(ledger.verified_records(), ())


if __name__ == "__main__":
    unittest.main()
