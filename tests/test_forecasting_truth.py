import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.agents import AgentContext, AgentOrchestrator, MarketMirrorAgent
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import MarketEvent
from autosport.forecasting import (
    ForecastOutcomeFact,
    ForecastRecord,
    JsonlForecastLedger,
    TemporalEvaluationWindow,
    evaluate_forecast_window,
    evaluate_walk_forward,
)
from autosport.paper import PaperBook
from autosport.paper_strategy import PaperValueAgent


class ForecastTruthTests(unittest.TestCase):
    def _record(
        self,
        forecast_id: str = "f-1",
        generated_at: str = "2026-02-10T12:00:00+00:00",
        training_cutoff: str = "2026-01-31T23:59:59+00:00",
        input_cutoff: str = "2026-02-10T11:59:00+00:00",
        probability: str = "0.70",
    ) -> ForecastRecord:
        return ForecastRecord(
            quote_key="e|winner|a",
            probability=Decimal(probability),
            model_id="tt-baseline",
            model_version="1.2.0",
            strategy_version="paper-value-v2",
            model_training_cutoff_ts=training_cutoff,
            input_cutoff_ts=input_cutoff,
            generated_at=generated_at,
            uncertainty=Decimal("0.08"),
            evidence_hashes=("a" * 64,),
            market_snapshot_hash="b" * 64,
            provenance={"dataset": "train-2026-01", "feature_set": "market-v1"},
            forecast_id=forecast_id,
        )

    def _window(
        self,
        window_id: str = "feb-holdout",
        start: str = "2026-02-01T00:00:00+00:00",
        end: str = "2026-02-28T23:59:59+00:00",
        training_end: str = "2026-01-31T23:59:59+00:00",
    ) -> TemporalEvaluationWindow:
        return TemporalEvaluationWindow(window_id, training_end, start, end, "holdout")

    def test_forecast_rejects_future_provenance_and_bad_causal_order(self):
        with self.assertRaisesRegex(ValueError, "future-result"):
            ForecastRecord(
                quote_key="e|winner|a",
                probability=Decimal("0.5"),
                model_id="m",
                model_version="1",
                strategy_version="s",
                model_training_cutoff_ts="2026-01-01T00:00:00+00:00",
                input_cutoff_ts="2026-01-02T00:00:00+00:00",
                generated_at="2026-01-03T00:00:00+00:00",
                provenance={"final_result": "a"},
            )
        with self.assertRaisesRegex(ValueError, "training cutoff"):
            self._record(
                training_cutoff="2026-02-10T12:00:00+00:00",
                input_cutoff="2026-02-10T11:59:00+00:00",
            )
        with self.assertRaisesRegex(ValueError, "input cutoff"):
            self._record(
                input_cutoff="2026-02-10T12:01:00+00:00",
                generated_at="2026-02-10T12:00:00+00:00",
            )

    def test_forecast_ledger_is_pre_outcome_and_hashes_record(self):
        record = self._record()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forecasts.jsonl"
            digest = JsonlForecastLedger(path).append(record)
            envelope = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(digest, record.canonical_hash)
            self.assertEqual(envelope["sha256"], record.canonical_hash)
            self.assertEqual(envelope["record"]["forecast_id"], "f-1")
            self.assertNotIn("outcome", envelope["record"])

    def test_holdout_metrics_and_calibration_are_evaluated_post_outcome(self):
        records = [
            self._record("f-1", probability="0.8"),
            self._record(
                "f-2",
                generated_at="2026-02-11T12:00:00+00:00",
                input_cutoff="2026-02-11T11:59:00+00:00",
                probability="0.2",
            ),
        ]
        outcomes = [
            ForecastOutcomeFact("f-1", 1, "2026-02-10T14:00:00+00:00"),
            ForecastOutcomeFact("f-2", 0, "2026-02-11T14:00:00+00:00"),
        ]
        report = evaluate_forecast_window(records, outcomes, self._window(), bins=5)
        self.assertEqual(report.count, 2)
        self.assertAlmostEqual(report.brier_score, 0.04)
        self.assertGreater(report.log_loss, 0)
        self.assertAlmostEqual(report.mean_uncertainty, 0.08)
        self.assertEqual(report.model_versions, ("1.2.0",))
        self.assertEqual(report.strategy_versions, ("paper-value-v2",))
        self.assertEqual(sum(item.count for item in report.calibration), 2)

    def test_holdout_rejects_model_training_after_training_boundary(self):
        record = self._record(training_cutoff="2026-02-02T00:00:00+00:00")
        outcome = ForecastOutcomeFact("f-1", 1, "2026-02-10T14:00:00+00:00")
        with self.assertRaisesRegex(ValueError, "holdout/walk-forward leakage"):
            evaluate_forecast_window([record], [outcome], self._window())

    def test_duplicate_outcome_fact_fails_closed(self):
        record = self._record()
        outcomes = [
            ForecastOutcomeFact("f-1", 1, "2026-02-10T14:00:00+00:00"),
            ForecastOutcomeFact("f-1", 0, "2026-02-10T15:00:00+00:00"),
        ]
        with self.assertRaisesRegex(ValueError, "duplicate outcome"):
            evaluate_forecast_window([record], outcomes, self._window())

    def test_walk_forward_requires_non_overlapping_temporal_folds(self):
        records = [
            self._record("f-1"),
            self._record(
                "f-2",
                generated_at="2026-03-10T12:00:00+00:00",
                training_cutoff="2026-02-28T23:59:59+00:00",
                input_cutoff="2026-03-10T11:59:00+00:00",
            ),
        ]
        outcomes = [
            ForecastOutcomeFact("f-1", 1, "2026-02-10T14:00:00+00:00"),
            ForecastOutcomeFact("f-2", 0, "2026-03-10T14:00:00+00:00"),
        ]
        windows = [
            self._window(),
            self._window(
                "mar-holdout",
                "2026-03-01T00:00:00+00:00",
                "2026-03-31T23:59:59+00:00",
                "2026-02-28T23:59:59+00:00",
            ),
        ]
        reports = evaluate_walk_forward(records, outcomes, windows)
        self.assertEqual(tuple(report.window_id for report in reports), ("feb-holdout", "mar-holdout"))

        overlapping = [
            self._window(),
            self._window(
                "overlap",
                "2026-02-20T00:00:00+00:00",
                "2026-03-01T00:00:00+00:00",
                "2026-02-19T23:59:59+00:00",
            ),
        ]
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            evaluate_walk_forward(records, outcomes, overlapping)

    def test_paper_decision_carries_versioned_forecast_provenance(self):
        event = MarketEvent.from_dict(
            {
                "event_id": "e",
                "market_id": "winner",
                "selection_id": "a",
                "decimal_odds": "2.0",
                "observed_ts": "2026-02-10T12:00:01+00:00",
                "source_id": "fixture",
                "sequence": 1,
            }
        )
        record = self._record(
            input_cutoff="2026-02-10T12:00:00+00:00",
            generated_at="2026-02-10T12:00:00+00:00",
        )
        record = ForecastRecord(
            **{**record.to_dict(), "probability": Decimal(record.probability), "uncertainty": Decimal(record.uncertainty), "evidence_hashes": tuple(record.evidence_hashes)}
        )
        # to_dict preserves the explicit forecast_id while rebuilding an immutable record.
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "decisions.jsonl"
            book = PaperBook("10000")
            context = AgentContext(
                book,
                replay_run_id="run-forecast",
                decision_ledger=JsonlDecisionLedger(ledger_path),
            )
            AgentOrchestrator(
                [MarketMirrorAgent(), PaperValueAgent({event.quote_key: record}, "50", "0.05")],
                context,
            ).on_market_event(event)
            envelope = json.loads(ledger_path.read_text(encoding="utf-8"))
            payload = envelope["record"]["payload"]
            self.assertEqual(payload["forecast_id"], record.forecast_id)
            self.assertEqual(payload["forecast_hash"], record.canonical_hash)
            self.assertEqual(payload["model_version"], "1.2.0")
            self.assertEqual(payload["strategy_version"], "paper-value-v2")
            self.assertEqual(payload["uncertainty"], "0.08")

    def test_paper_agent_does_not_use_forecast_from_after_market_event(self):
        event = MarketEvent.from_dict(
            {
                "event_id": "e",
                "market_id": "winner",
                "selection_id": "a",
                "decimal_odds": "2.0",
                "observed_ts": "2026-02-10T12:00:00+00:00",
                "source_id": "fixture",
                "sequence": 1,
            }
        )
        future = self._record(
            input_cutoff="2026-02-10T12:00:01+00:00",
            generated_at="2026-02-10T12:00:01+00:00",
        )
        book = PaperBook("10000")
        context = AgentContext(book, replay_run_id="causal-run")
        PaperValueAgent({event.quote_key: future}).on_market_event(event, context)
        self.assertEqual(len(book.tickets), 0)


if __name__ == "__main__":
    unittest.main()
