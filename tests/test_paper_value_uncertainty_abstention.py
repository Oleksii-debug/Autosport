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
from autosport.opportunity import (
    ForecastRef,
    PredictiveEligibilityEvidence,
    QuoteRef,
)
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import PaperExecutionAdoptionRuntime
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
from autosport.paper_strategy import Forecast, PaperValueAgent
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
            market_snapshot_hash="a" * 64,
        )

    @staticmethod
    def _legacy_forecast(event: MarketEvent) -> Forecast:
        return Forecast(
            quote_key=event.quote_key,
            probability=Decimal("0.60"),
            model_id="legacy-paper-control",
            as_of_ts="2026-09-17T14:59:58+00:00",
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
        forecast: Forecast | ForecastRecord,
        *,
        predictive_ref: ForecastRef | None = None,
    ) -> PaperValueAgent:
        refs = (
            None
            if predictive_ref is None
            else {forecast.quote_key: predictive_ref}
        )
        return PaperValueAgent(
            {forecast.quote_key: forecast},
            stake=Decimal("1"),
            risk_policy=PaperRiskPolicy(economic_goal=goal),
            predictive_forecast_refs=refs,
        )

    @staticmethod
    def _self_attested_ref(
        event: MarketEvent,
        forecast: ForecastRecord,
    ) -> ForecastRef:
        evidence = PredictiveEligibilityEvidence(
            evaluation_id="caller-evaluation",
            evaluation_sha256="1" * 64,
            protocol_sha256="2" * 64,
            admission_policy_sha256="3" * 64,
            model_id=forecast.model_id,
            model_version=forecast.model_version,
            strategy_version=forecast.strategy_version,
            uncertainty_kind="absolute_probability_radius_v1",
            sample_size=500,
            minimum_sample_size=3,
            maximum_uncertainty=Decimal("1"),
            as_of=event.observed_ts,
            valid_until=event.observed_ts,
        )
        quote = QuoteRef.from_market_event(
            event,
            market_snapshot_hash=forecast.market_snapshot_hash,
        )
        return ForecastRef.from_forecast(
            forecast,
            quote,
            predictive_eligibility=evidence,
        )

    def test_goal_active_paper_value_abstains_when_uncertainty_erases_robust_edge(
        self,
    ) -> None:
        goal = self._goal()
        event = self._event()

        # Control case: prove this exact fixture reaches the canonical #623 PAPER
        # execution path. This legacy compatibility forecast is intentionally used
        # only as reachability control; modern ForecastRecord has a stricter
        # predictive-authority boundary below.
        with tempfile.TemporaryDirectory() as tmp:
            control_root = Path(tmp)
            control_context, control_ledger_path = self._context(
                control_root,
                event,
            )

            self._agent(
                goal,
                self._legacy_forecast(event),
            ).on_market_event(event, control_context)

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

        # Point EV is +0.20 at decimal odds 2.0, but the canonical predictive
        # uncertainty semantics are an absolute probability radius. The resulting
        # lower endpoint is zero, so robust BACK edge is not positive.
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
            self.assertTrue(
                any(
                    "canonical predictive ForecastRef authority" in note
                    for note in context.notes
                )
            )

    def test_zero_uncertainty_is_not_a_substitute_for_predictive_authority(
        self,
    ) -> None:
        goal = self._goal()
        event = self._event()
        forecast = self._forecast(event, uncertainty=Decimal("0"))

        with tempfile.TemporaryDirectory() as tmp:
            context, ledger_path = self._context(Path(tmp), event)

            self._agent(goal, forecast).on_market_event(event, context)

            self.assertEqual(context.paper_book.tickets, {})
            self.assertFalse(ledger_path.exists())
            self.assertTrue(
                any(
                    "canonical predictive ForecastRef authority" in note
                    for note in context.notes
                )
            )

    def test_self_attested_predictive_evidence_cannot_authorize_paper_exposure(
        self,
    ) -> None:
        goal = self._goal()
        event = self._event()
        forecast = self._forecast(event, uncertainty=Decimal("0.01"))
        caller_ref = self._self_attested_ref(event, forecast)

        with tempfile.TemporaryDirectory() as tmp:
            context, ledger_path = self._context(Path(tmp), event)

            self._agent(
                goal,
                forecast,
                predictive_ref=caller_ref,
            ).on_market_event(event, context)

            self.assertEqual(context.paper_book.tickets, {})
            self.assertFalse(ledger_path.exists())
            self.assertTrue(
                any(
                    "predictive eligibility was not resolved from canonical"
                    in note
                    for note in context.notes
                )
            )

    def test_predictive_reference_mapping_is_snapshotted_and_key_bound(self) -> None:
        event = self._event()
        forecast = self._forecast(event, uncertainty=Decimal("0.01"))
        caller_ref = self._self_attested_ref(event, forecast)
        refs = {event.quote_key: caller_ref}

        agent = self._agent(
            self._goal(),
            forecast,
            predictive_ref=caller_ref,
        )
        refs.clear()
        self.assertIs(
            agent.predictive_forecast_refs[event.quote_key],
            caller_ref,
        )

        with self.assertRaisesRegex(
            ValueError,
            "key must match",
        ):
            PaperValueAgent(
                {event.quote_key: forecast},
                predictive_forecast_refs={"wrong-key": caller_ref},
            )


if __name__ == "__main__":
    unittest.main()
