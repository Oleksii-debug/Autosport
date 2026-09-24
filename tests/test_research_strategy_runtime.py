import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.research_strategy import (
    RESEARCH_STRATEGY_ID,
    ResearchStrategyPlan,
    market_event_evidence_hash,
    research_market_snapshot_hash,
)
from autosport.run_registry import RunRegistry
from autosport.session import AutosportSession
from autosport.strategies import experiment_strategy_id


class ResearchStrategyRuntimeTests(unittest.TestCase):
    def _plan_dict(self, *, probability: str = "0.60", odds: str | None = None):
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
        candidate_odds = odds or str(trigger.decimal_odds)
        evidence_hash = market_event_evidence_hash(trigger)
        snapshot_hash = research_market_snapshot_hash(latest, [trigger.quote_key])
        other_quote = "tt-demo-1|winner|player-a"
        p = Decimal(probability)
        return {
            "schema_version": 1,
            "strategy_id": RESEARCH_STRATEGY_ID,
            "decisions": [
                {
                    "decision_id": "research-demo-1",
                    "trigger_quote_key": trigger.quote_key,
                    "decision_ts": trigger.observed_ts,
                    "stake": "10",
                    "candidate": {
                        "legs": [
                            {
                                "quote_key": trigger.quote_key,
                                "decimal_odds": candidate_odds,
                                "probability": probability,
                            }
                        ]
                    },
                    "scenario_groups": [
                        {
                            "group_id": "tt-demo-1-winner",
                            "outcomes": [
                                {"quote_key": other_quote, "probability": str(Decimal("1") - p)},
                                {"quote_key": trigger.quote_key, "probability": probability},
                            ],
                        }
                    ],
                    "forecasts": [
                        {
                            "forecast_id": "forecast-demo-b",
                            "quote_key": trigger.quote_key,
                            "probability": probability,
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
                            "evidence_id": "evidence-demo-b",
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

    def test_research_strategy_runs_full_typed_pipeline_in_dataset_session(self):
        dataset = load_dataset(Path("examples/tt_demo"))
        plan = ResearchStrategyPlan.from_dict(self._plan_dict())
        with tempfile.TemporaryDirectory() as tmp:
            session = AutosportSession(
                tmp,
                "1000",
                strategy_id=RESEARCH_STRATEGY_ID,
                research_plan=plan,
            )
            try:
                result = session.run_dataset(dataset)
                self.assertEqual(result.replay.event_count, 4)
                self.assertEqual(len(result.settled_ticket_ids), 1)
                self.assertEqual(result.balance, Decimal("990"))
                self.assertEqual(result.evaluation.net_profit, Decimal("-10"))
                self.assertEqual(session.strategy.strategy_id, RESEARCH_STRATEGY_ID)
                self.assertEqual(
                    session.strategy_id,
                    f"{RESEARCH_STRATEGY_ID}@{plan.source_sha256}",
                )

                summary = json.loads(Path(result.result_path).read_text(encoding="utf-8"))
                self.assertEqual(summary["strategy_id"], session.strategy_id)
                self.assertEqual(
                    summary["strategy_runtime"]["canonical_strategy_id"],
                    RESEARCH_STRATEGY_ID,
                )
                self.assertEqual(
                    summary["strategy_runtime"]["research_plan_sha256"],
                    plan.source_sha256,
                )
                ledger_lines = session.ledger.path.read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(ledger_lines), 1)
                envelope = json.loads(ledger_lines[0])
                self.assertEqual(
                    envelope["record"]["action"],
                    "OPEN_PAPER_RESEARCH_TICKET",
                )
                self.assertFalse(envelope["record"]["payload"]["real_money_execution"])
            finally:
                session.close()

    def test_scalar_quality_flags_fail_closed_before_policy_review(self):
        raw = self._plan_dict()
        raw["decisions"][0]["evidence"][0]["quality_flags"] = "GAP_DETECTED"
        with self.assertRaisesRegex(ValueError, "quality_flags must be a JSON array"):
            ResearchStrategyPlan.from_dict(raw)

    def test_stale_candidate_odds_fail_before_registry_or_book_mutation(self):
        dataset = load_dataset(Path("examples/tt_demo"))
        plan = ResearchStrategyPlan.from_dict(self._plan_dict(odds="1.80"))
        with tempfile.TemporaryDirectory() as tmp:
            session = AutosportSession(
                tmp,
                "1000",
                strategy_id=RESEARCH_STRATEGY_ID,
                research_plan=plan,
            )
            try:
                with self.assertRaisesRegex(ValueError, "candidate odds do not match replay state"):
                    session.run_dataset(dataset)
                self.assertEqual(RunRegistry(Path(tmp) / "run_registry.json").in_progress(), ())
                self.assertFalse((Path(tmp) / "paper_book.json").exists())
            finally:
                session.close()

    def test_plan_hash_is_part_of_research_experiment_identity(self):
        first = ResearchStrategyPlan.from_dict(self._plan_dict(probability="0.60"))
        second = ResearchStrategyPlan.from_dict(self._plan_dict(probability="0.55"))
        self.assertNotEqual(first.source_sha256, second.source_sha256)
        self.assertNotEqual(
            experiment_strategy_id(RESEARCH_STRATEGY_ID, first),
            experiment_strategy_id(RESEARCH_STRATEGY_ID, second),
        )

    def test_research_strategy_requires_plan_before_runtime_state_opens(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            with self.assertRaisesRegex(ValueError, "requires --research-plan"):
                AutosportSession(workspace, strategy_id=RESEARCH_STRATEGY_ID)
            self.assertTrue(workspace.exists())
            self.assertFalse((workspace / "market.db").exists())
            self.assertFalse((workspace / "run_registry.json").exists())



    def test_forecast_market_semantics_survives_plan_parse_and_binds_replay_event(self):
        dataset = load_dataset(Path("examples/tt_demo"))
        events = list(dataset.load_market_events())
        raw = self._plan_dict()
        decision = raw["decisions"][0]
        trigger_key = decision["trigger_quote_key"]
        decision_ts = decision["decision_ts"]
        semantics = "table-tennis:winner:v1"

        semantic_events = [
            replace(event, market_semantics_id=semantics)
            if event.quote_key == trigger_key and event.observed_ts == decision_ts
            else event
            for event in events
        ]
        latest = {}
        trigger = None
        for event in sorted(
            semantic_events,
            key=lambda item: (item.observed_ts, item.sequence, item.dedupe_key),
        ):
            latest[event.quote_key] = event
            if event.quote_key == trigger_key and event.observed_ts == decision_ts:
                trigger = event
                break
        self.assertIsNotNone(trigger)
        assert trigger is not None

        evidence_hash = market_event_evidence_hash(trigger)
        snapshot_hash = research_market_snapshot_hash(latest, [trigger_key])
        forecast_raw = decision["forecasts"][0]
        evidence_raw = decision["evidence"][0]
        forecast_raw["evidence_hashes"] = [evidence_hash]
        forecast_raw["market_snapshot_hash"] = snapshot_hash
        forecast_raw["market_semantics_id"] = semantics
        evidence_raw["content_sha256"] = evidence_hash
        evidence_raw["market_snapshot_hash"] = snapshot_hash

        matching = ResearchStrategyPlan.from_dict(raw)
        self.assertEqual(
            matching.instructions[0].forecasts[0].market_semantics_id,
            semantics,
        )
        matching.preflight(semantic_events)

        forecast_raw["market_semantics_id"] = "table-tennis:winner:v2"
        mismatched = ResearchStrategyPlan.from_dict(raw)
        with self.assertRaisesRegex(
            ValueError,
            "ForecastRecord market_semantics_id does not match replay state",
        ):
            mismatched.preflight(semantic_events)

if __name__ == "__main__":
    unittest.main()