import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.forecast_origin import (
    bind_walk_forward_bundle_forecasts,
    verify_and_report,
    verify_workspace_forecast_origin,
)
from autosport.research_strategy import (
    RESEARCH_STRATEGY_ID,
    ResearchStrategyPlan,
    market_event_evidence_hash,
    research_market_snapshot_hash,
)
from autosport.session import AutosportSession


class ForecastOriginEvidenceTests(unittest.TestCase):
    def _plan(self) -> ResearchStrategyPlan:
        dataset = load_dataset(Path("examples/tt_demo"))
        latest = {}
        trigger = None
        for event in sorted(
            dataset.load_market_events(),
            key=lambda item: (item.observed_ts, item.sequence, item.dedupe_key),
        ):
            latest[event.quote_key] = event
            if event.sequence == 4:
                trigger = event
                break
        self.assertIsNotNone(trigger)
        assert trigger is not None
        evidence_hash = market_event_evidence_hash(trigger)
        snapshot_hash = research_market_snapshot_hash(latest, [trigger.quote_key])
        probability = Decimal("0.60")
        raw = {
            "schema_version": 1,
            "strategy_id": RESEARCH_STRATEGY_ID,
            "decisions": [
                {
                    "decision_id": "origin-proof-decision",
                    "trigger_quote_key": trigger.quote_key,
                    "decision_ts": trigger.observed_ts,
                    "stake": "10",
                    "candidate": {
                        "legs": [
                            {
                                "quote_key": trigger.quote_key,
                                "decimal_odds": str(trigger.decimal_odds),
                                "probability": str(probability),
                            }
                        ]
                    },
                    "scenario_groups": [
                        {
                            "group_id": "tt-demo-1-winner",
                            "outcomes": [
                                {
                                    "quote_key": "tt-demo-1|winner|player-a",
                                    "probability": str(Decimal("1") - probability),
                                },
                                {
                                    "quote_key": trigger.quote_key,
                                    "probability": str(probability),
                                },
                            ],
                        }
                    ],
                    "forecasts": [
                        {
                            "forecast_id": "forecast-origin-demo",
                            "quote_key": trigger.quote_key,
                            "probability": str(probability),
                            "model_id": "typed-demo-model",
                            "model_version": "1.0.0",
                            "strategy_version": "research-replay-v1",
                            "model_training_cutoff_ts": "2026-09-12T09:00:00+00:00",
                            "input_cutoff_ts": trigger.observed_ts,
                            "generated_at": trigger.observed_ts,
                            "uncertainty": "0.10",
                            "evidence_hashes": [evidence_hash],
                            "market_snapshot_hash": snapshot_hash,
                            "provenance": {"source": "sealed-test-plan"},
                        }
                    ],
                    "evidence": [
                        {
                            "evidence_id": "origin-evidence",
                            "quote_key": trigger.quote_key,
                            "source_id": trigger.source_id,
                            "observed_at": trigger.observed_ts,
                            "available_at": trigger.observed_ts,
                            "decimal_odds": str(trigger.decimal_odds),
                            "content_sha256": evidence_hash,
                            "quality_flags": [],
                            "market_snapshot_hash": snapshot_hash,
                        }
                    ],
                }
            ],
        }
        return ResearchStrategyPlan.from_dict(raw)

    def _completed_run(self, workspace: Path):
        dataset = load_dataset(Path("examples/tt_demo"))
        plan = self._plan()
        session = AutosportSession(
            workspace,
            "1000",
            strategy_id=RESEARCH_STRATEGY_ID,
            research_plan=plan,
        )
        result = session.run_dataset(dataset)
        return session, result, plan, dataset

    def test_completed_research_run_binds_exact_forecast_hash_to_transaction_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, result, plan, _dataset = self._completed_run(Path(tmp))
            try:
                evidence = verify_workspace_forecast_origin(tmp, result.replay.run_id)
                forecast = plan.instructions[0].forecasts[0]
                self.assertEqual(evidence.forecast_hashes[forecast.forecast_id], forecast.canonical_hash)
                report = evidence.report()
                self.assertTrue(report["truth"]["transaction_summary_hash_verified"])
                self.assertTrue(report["truth"]["canonical_decision_append_chain_verified"])
                self.assertTrue(report["truth"]["causal_replay_forecast_hashes_verified"])
                self.assertTrue(report["truth"]["outcome_fields_absent_from_causal_decisions"])
                self.assertFalse(report["truth"]["independent_pre_outcome_model_generation_verified"])
                self.assertFalse(report["truth"]["temporal_holdout_protocol_verified"])
                self.assertFalse(report["truth"]["profitability_claim"])
                self.assertFalse(report["truth"]["real_money_execution"])
            finally:
                session.close()

    def test_old_run_remains_verifiable_after_later_canonical_ledger_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session, first, _plan, dataset = self._completed_run(workspace)
            try:
                second = session.run_dataset(dataset, allow_repeat=True)
                self.assertNotEqual(first.replay.run_id, second.replay.run_id)
                first_evidence = verify_workspace_forecast_origin(workspace, first.replay.run_id)
                second_evidence = verify_workspace_forecast_origin(workspace, second.replay.run_id)
                self.assertEqual(len(first_evidence.forecasts), 1)
                self.assertEqual(len(second_evidence.forecasts), 1)
                self.assertNotEqual(
                    first_evidence.decision_ledger_sha256,
                    second_evidence.decision_ledger_sha256,
                )
            finally:
                session.close()

    def test_walk_forward_forecast_must_match_exact_audited_record_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session, result, plan, _dataset = self._completed_run(workspace)
            try:
                forecast = plan.instructions[0].forecasts[0]
                bundle_path = workspace / "evaluation.json"
                bundle_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "forecasts": [forecast.to_dict()],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                evidence = verify_workspace_forecast_origin(workspace, result.replay.run_id)
                self.assertEqual(bind_walk_forward_bundle_forecasts(evidence, bundle_path), 1)
                report = verify_and_report(
                    workspace,
                    result.replay.run_id,
                    bundle_path=bundle_path,
                )
                self.assertTrue(report["truth"]["evaluation_bundle_forecasts_bound"])
                self.assertEqual(report["bound_evaluation_forecast_count"], 1)
                self.assertFalse(report["truth"]["temporal_holdout_protocol_verified"])

                tampered = forecast.to_dict()
                tampered["probability"] = "0.99"
                bundle_path.write_text(
                    json.dumps({"schema_version": 1, "forecasts": [tampered]}),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "hash does not match causal run audit"):
                    bind_walk_forward_bundle_forecasts(evidence, bundle_path)
            finally:
                session.close()

    def test_tampered_per_run_ledger_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session, result, _plan, _dataset = self._completed_run(workspace)
            try:
                path = (
                    workspace
                    / ".run-transactions"
                    / result.replay.run_id
                    / "run-decisions.jsonl"
                )
                text = path.read_text(encoding="utf-8")
                path.write_text(text.replace("forecast-origin-demo", "forecast-origin-evil"), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "run decisions are not the transaction Decision Ledger suffix"):
                    verify_workspace_forecast_origin(workspace, result.replay.run_id)
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
