from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from autosport.agents import AgentContext
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import MarketEvent
from autosport.economic_goal import EconomicGoalContract
from autosport.forecasting import ForecastRecord
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import PaperExecutionAdoptionRuntime
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
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
    def _forecast(
        event: MarketEvent,
        *,
        uncertainty: Decimal,
    ) -> ForecastRecord:
        return ForecastRecord(
            quote_key=event.quote_key,
            probability=Decimal("0.60"),
            model_id="model-uncertainty",
            model_version="model-uncertainty-v1",
            strategy_version="strategy-uncertainty-v1",
            model_training_cutoff_ts="2026-09-17T14:00:00+00:00",
            input_cutoff_ts="2026-09-17T14:59:58+00:00",
            generated_at="2026-09-17T14:59:58+00:00",
            uncertainty=uncertainty,
        )

    @staticmethod
    def _execution_config() -> PaperExecutionModelConfig:
        return PaperExecutionModelConfig(
            model_id="paper-uncertainty-execution-fixture",
            model_version="1",
            evidence_grade=EvidenceGrade.SYNTHETIC,
            evidence_source="test-paper-uncertainty-composition",
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

    @classmethod
    def _context(
        cls,
        root: Path,
        event: MarketEvent,
    ) -> tuple[AgentContext, Path]:
        book = PaperBook("100")
        ledger_path = root / "decisions.jsonl"
        runtime = PaperExecutionAdoptionRuntime(
            book=book,
            ledger=PaperExecutionLedger(root / "paper-execution.jsonl"),
            config=cls._execution_config(),
            max_quote_age=timedelta(seconds=5),
            paper_book_path=root / "paper-execution-book.json",
        )
        context = AgentContext(
            book,
            latest_quotes={event.quote_key: event},
            replay_run_id="run-uncertainty",
            decision_ledger=JsonlDecisionLedger(ledger_path),
            paper_execution=runtime,
            paper_provider_accounts=(("provider-1", "paper-account-1"),),
        )
        return context, ledger_path

    @staticmethod
    def _agent(
        goal: EconomicGoalContract,
        forecast: ForecastRecord,
    ) -> PaperValueAgent:
        return PaperValueAgent(
            {forecast.quote_key: forecast},
            stake=Decimal("1"),
            risk_policy=PaperRiskPolicy(economic_goal=goal),
        )

    def test_goal_active_paper_value_abstains_when_uncertainty_erases_robust_edge(
        self,
    ) -> None:
        goal = self._goal()
        event = self._event()

        # Control case: prove this exact fixture reaches the canonical #623 PAPER
        # execution path. Without this control, a missing execution/account authority
        # could make the uncertainty assertion vacuously green.
        with tempfile.TemporaryDirectory() as tmp:
            control_root = Path(tmp)
            control_forecast = self._forecast(
                event,
                uncertainty=Decimal("0"),
            )
            control_context, control_ledger_path = self._context(
                control_root,
                event,
            )

            self._agent(goal, control_forecast).on_market_event(
                event,
                control_context,
            )

            self.assertEqual(len(control_context.paper_book.tickets), 1)
            self.assertTrue(control_ledger_path.exists())
            self.assertEqual(
                len(control_context.decision_ledger.verified_records()),
                1,
            )

        forecast = self._forecast(
            event,
            uncertainty=Decimal("1"),
        )

        # Point EV is +0.20 at decimal odds 2.0, but an absolute-probability
        # uncertainty radius of 1.0 leaves no positive lower-bound probability
        # and therefore no robust positive edge. The PAPER path must not turn
        # this point estimate into a positive material action.
        self.assertGreater(
            forecast.probability * event.decimal_odds - Decimal("1"),
            0,
        )
        robust_probability_lower = max(
            Decimal("0"),
            forecast.probability - forecast.uncertainty,
        )
        self.assertLessEqual(
            robust_probability_lower * event.decimal_odds - Decimal("1"),
            0,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, ledger_path = self._context(root, event)

            self._agent(goal, forecast).on_market_event(event, context)

            self.assertEqual(context.paper_book.tickets, {})
            self.assertFalse(
                ledger_path.exists(),
                "abstention must not persist a positive material decision",
            )


if __name__ == "__main__":
    unittest.main()
