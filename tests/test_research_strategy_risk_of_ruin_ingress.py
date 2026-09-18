import copy
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.agents import AgentContext
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import MarketEvent
from autosport.paper import PaperBook
from autosport.research_strategy import (
    ResearchReplayAgent,
    ResearchStrategyPlan,
    market_event_evidence_hash,
    research_market_snapshot_hash,
)
from autosport.risk import RiskOfRuinEvidence


class _CapturingResearchPipeline:
    def __init__(self) -> None:
        self.calls = []

    def decide_and_open(self, **kwargs):
        self.calls.append(kwargs)
        return object()


class ResearchStrategyRiskOfRuinIngressTests(unittest.TestCase):
    def _event(self) -> MarketEvent:
        timestamp = "2026-09-18T00:00:03+00:00"
        return MarketEvent(
            event_id="event-1",
            market_id="winner",
            selection_id="away",
            decimal_odds=Decimal("2.00"),
            observed_ts=timestamp,
            source_id="fixture",
            sequence=1,
            source_ts=timestamp,
            ingest_ts=timestamp,
        )

    def _risk_payload(self, **overrides):
        payload = {
            "evidence_id": "ror-evidence-1",
            "research_protocol_sha256": "a" * 64,
            "reproducibility_bundle_sha256": "b" * 64,
            "producer_identity": "frozen-research-producer",
            "causal_cutoff": "2026-09-18T00:00:01+00:00",
            "evaluated_at": "2026-09-18T00:00:02+00:00",
            "bankroll_id": "paper-bankroll",
            "currency": "USD",
            "base_portfolio_sha256": "c" * 64,
            "candidate_sha256": "d" * 64,
            "evaluated_stake": "2.00",
            "upper_bound": "0.01",
        }
        payload.update(overrides)
        return payload

    def _plan_raw(self, *, include_risk=True, risk_payload=None):
        event = self._event()
        snapshot_hash = research_market_snapshot_hash(
            {event.quote_key: event},
            [event.quote_key],
        )
        evidence_hash = market_event_evidence_hash(event)
        decision = {
            "decision_id": "research-ror-runtime-1",
            "trigger_quote_key": event.quote_key,
            "decision_ts": event.observed_ts,
            "stake": "10",
            "candidate": {
                "legs": [
                    {
                        "quote_key": event.quote_key,
                        "decimal_odds": "2.00",
                        "probability": "0.60",
                    }
                ]
            },
            "scenario_groups": [
                {
                    "group_id": "event-1-winner",
                    "outcomes": [
                        {
                            "quote_key": event.quote_key,
                            "probability": "0.60",
                        },
                        {
                            "quote_key": "abstract-complement:event-1-winner",
                            "probability": "0.40",
                        },
                    ],
                }
            ],
            "forecasts": [
                {
                    "forecast_id": "forecast-ror-runtime-1",
                    "quote_key": event.quote_key,
                    "probability": "0.60",
                    "model_id": "typed-test-model",
                    "model_version": "1.0.0",
                    "strategy_version": "research-replay-v1",
                    "model_training_cutoff_ts": "2026-09-17T23:00:00+00:00",
                    "input_cutoff_ts": event.observed_ts,
                    "generated_at": event.observed_ts,
                    "uncertainty": "0.10",
                    "evidence_hashes": [evidence_hash],
                    "market_snapshot_hash": snapshot_hash,
                    "provenance": {"source": "unit-test"},
                }
            ],
            "evidence": [
                {
                    "evidence_id": "market-evidence-1",
                    "quote_key": event.quote_key,
                    "source_id": event.source_id,
                    "observed_at": event.observed_ts,
                    "available_at": event.observed_ts,
                    "decimal_odds": "2.00",
                    "content_sha256": evidence_hash,
                    "quality_flags": [],
                    "market_snapshot_hash": snapshot_hash,
                }
            ],
        }
        if include_risk:
            decision["risk_of_ruin_evidence"] = (
                self._risk_payload() if risk_payload is None else risk_payload
            )
        return {
            "schema_version": 1,
            "strategy_id": "research-replay-v1",
            "decisions": [decision],
        }

    def test_plan_parses_typed_ruin_witness_and_binds_it_to_plan_identity(self):
        raw = self._plan_raw()
        first = ResearchStrategyPlan.from_dict(raw)
        instruction = first.instructions[0]

        self.assertIsInstance(instruction.risk_of_ruin_evidence, RiskOfRuinEvidence)
        self.assertEqual(
            instruction.risk_of_ruin_evidence.evidence_id,
            "ror-evidence-1",
        )
        self.assertEqual(
            instruction.risk_of_ruin_evidence.evaluated_stake,
            Decimal("2.00"),
        )

        changed = copy.deepcopy(raw)
        changed["decisions"][0]["risk_of_ruin_evidence"]["evidence_id"] = (
            "ror-evidence-2"
        )
        second = ResearchStrategyPlan.from_dict(changed)
        self.assertNotEqual(first.source_sha256, second.source_sha256)

    def test_legacy_plan_without_ruin_witness_remains_backward_readable(self):
        plan = ResearchStrategyPlan.from_dict(self._plan_raw(include_risk=False))

        self.assertIsNone(plan.instructions[0].risk_of_ruin_evidence)

    def test_plan_rejects_malformed_or_future_ruin_witness(self):
        malformed = self._plan_raw(risk_payload="not-an-object")
        with self.assertRaisesRegex(
            ValueError,
            "risk_of_ruin_evidence must be an object",
        ):
            ResearchStrategyPlan.from_dict(malformed)

        future = self._plan_raw(
            risk_payload=self._risk_payload(
                causal_cutoff="2026-09-18T00:00:04+00:00",
                evaluated_at="2026-09-18T00:00:05+00:00",
            )
        )
        with self.assertRaisesRegex(ValueError, "future information"):
            ResearchStrategyPlan.from_dict(future)

    def test_replay_agent_threads_exact_plan_witness_into_pipeline(self):
        event = self._event()
        plan = ResearchStrategyPlan.from_dict(self._plan_raw())
        pipeline = _CapturingResearchPipeline()
        agent = ResearchReplayAgent(plan, pipeline=pipeline)

        with tempfile.TemporaryDirectory() as tmp:
            context = AgentContext(
                paper_book=PaperBook("100"),
                latest_quotes={event.quote_key: event},
                replay_run_id="research-ror-run",
                decision_ledger=JsonlDecisionLedger(
                    Path(tmp) / "research-decisions.jsonl"
                ),
            )
            agent.on_market_event(event, context)

        self.assertEqual(len(pipeline.calls), 1)
        self.assertIs(
            pipeline.calls[0]["risk_of_ruin_evidence"],
            plan.instructions[0].risk_of_ruin_evidence,
        )

    def test_replay_agent_threads_none_for_backward_readable_legacy_plan(self):
        event = self._event()
        plan = ResearchStrategyPlan.from_dict(self._plan_raw(include_risk=False))
        pipeline = _CapturingResearchPipeline()
        agent = ResearchReplayAgent(plan, pipeline=pipeline)

        with tempfile.TemporaryDirectory() as tmp:
            context = AgentContext(
                paper_book=PaperBook("100"),
                latest_quotes={event.quote_key: event},
                replay_run_id="research-ror-legacy-run",
                decision_ledger=JsonlDecisionLedger(
                    Path(tmp) / "research-decisions.jsonl"
                ),
            )
            agent.on_market_event(event, context)

        self.assertEqual(len(pipeline.calls), 1)
        self.assertIsNone(pipeline.calls[0]["risk_of_ruin_evidence"])


if __name__ == "__main__":
    unittest.main()
