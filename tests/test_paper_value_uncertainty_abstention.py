from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.agents import AgentContext
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import MarketEvent
from autosport.economic_goal import EconomicGoalContract
from autosport.forecasting import ForecastRecord
from autosport.paper import PaperBook
from autosport.paper_strategy import PaperValueAgent
from autosport.risk import PaperRiskPolicy


class PaperValueUncertaintyAbstentionTests(unittest.TestCase):
    @staticmethod
    def _goal() -> EconomicGoalContract:
        return EconomicGoalContract(
            goal_id="goal-paper-uncertainty",
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
    def _event() -> MarketEvent:
        return MarketEvent(
            event_id="event-uncertainty",
            market_id="market-uncertainty",
            selection_id="selection-uncertainty",
            decimal_odds=Decimal("2"),
            observed_ts="2026-09-17T15:00:00+00:00",
            source_id="provider-1",
            sequence=1,
            source_ts="2026-09-17T14:59:59+00:00",
            ingest_ts="2026-09-17T15:00:00+00:00",
        )

    @staticmethod
    def _forecast(event: MarketEvent) -> ForecastRecord:
        return ForecastRecord(
            quote_key=event.quote_key,
            probability=Decimal("0.60"),
            model_id="model-uncertainty",
            model_version="model-uncertainty-v1",
            strategy_version="strategy-uncertainty-v1",
            model_training_cutoff_ts="2026-09-17T14:00:00+00:00",
            input_cutoff_ts="2026-09-17T14:59:58+00:00",
            generated_at="2026-09-17T14:59:58+00:00",
            uncertainty=Decimal("1"),
        )

    def test_goal_active_paper_value_abstains_when_uncertainty_erases_robust_edge(
        self,
    ) -> None:
        goal = self._goal()
        event = self._event()
        forecast = self._forecast(event)

        # Point EV is +0.20 at decimal odds 2.0, but an absolute-probability
        # uncertainty radius of 1.0 leaves no positive lower-bound probability
        # and therefore no robust positive edge. The PAPER path must not turn
        # this point estimate into a positive material action.
        self.assertGreater(forecast.probability * event.decimal_odds - Decimal("1"), 0)
        robust_probability_lower = max(
            Decimal("0"),
            forecast.probability - forecast.uncertainty,
        )
        self.assertLessEqual(
            robust_probability_lower * event.decimal_odds - Decimal("1"),
            0,
        )

        agent = PaperValueAgent(
            {event.quote_key: forecast},
            stake=Decimal("1"),
            risk_policy=PaperRiskPolicy(economic_goal=goal),
        )

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "decisions.jsonl"
            context = AgentContext(
                PaperBook("100"),
                latest_quotes={event.quote_key: event},
                replay_run_id="run-uncertainty",
                decision_ledger=JsonlDecisionLedger(ledger_path),
            )

            agent.on_market_event(event, context)

            self.assertEqual(context.paper_book.tickets, {})
            self.assertFalse(
                ledger_path.exists(),
                "abstention must not persist a positive material decision",
            )


if __name__ == "__main__":
    unittest.main()
