from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from autosport.agents import AgentContext
from autosport.candidate_search import CandidateLeg, ParlayCandidate
from autosport.dataset import load_dataset
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.forecasting import ForecastRecord
from autosport.paper import PaperBook
from autosport.research_pipeline import ResearchEvidence
from autosport.research_strategy import (
    RESEARCH_STRATEGY_ID,
    ResearchReplayAgent,
    ResearchReplayInstruction,
    ResearchStrategyPlan,
    market_event_evidence_hash,
    research_market_snapshot_hash,
)
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome


class _FailIfCalledPipeline:
    def __init__(self) -> None:
        self.called = False

    def decide_and_open(self, **_kwargs):
        self.called = True
        raise AssertionError("economic research pipeline must not run for a non-open market")


def _suspended_plan():
    dataset = load_dataset(Path("examples/tt_demo"))
    base_event = sorted(
        dataset.load_market_events(),
        key=lambda item: (item.observed_ts, item.sequence, item.dedupe_key),
    )[0]
    event = replace(base_event, status="suspended")
    latest = {event.quote_key: event}
    snapshot_hash = research_market_snapshot_hash(latest, [event.quote_key])
    evidence_hash = market_event_evidence_hash(event)
    probability = Decimal("0.90")
    leg = CandidateLeg(event.quote_key, event.event_id, event.decimal_odds, probability)
    candidate = ParlayCandidate(
        (leg,),
        event.decimal_odds,
        probability,
        probability * event.decimal_odds - Decimal("1"),
    )
    forecast = ForecastRecord(
        quote_key=event.quote_key,
        probability=probability,
        model_id="suspended-market-test",
        model_version="1.0.0",
        strategy_version=RESEARCH_STRATEGY_ID,
        model_training_cutoff_ts="2020-01-01T00:00:00+00:00",
        input_cutoff_ts=event.observed_ts,
        generated_at=event.observed_ts,
        uncertainty=Decimal("0.01"),
        evidence_hashes=(evidence_hash,),
        market_snapshot_hash=snapshot_hash,
        provenance={"source": "causal-test"},
        forecast_id="forecast-suspended-market",
    )
    evidence = ResearchEvidence(
        evidence_id="evidence-suspended-market",
        quote_key=event.quote_key,
        source_id=event.source_id,
        observed_at=event.observed_ts,
        available_at=event.observed_ts,
        decimal_odds=event.decimal_odds,
        content_sha256=evidence_hash,
        market_snapshot_hash=snapshot_hash,
    )
    group = ScenarioGroup(
        "binary-test-group",
        (
            ScenarioOutcome(event.quote_key, probability),
            ScenarioOutcome("synthetic-opposite|winner|other", Decimal("0.10")),
        ),
    )
    instruction = ResearchReplayInstruction(
        decision_id="decision-suspended-market",
        trigger_quote_key=event.quote_key,
        decision_ts=event.observed_ts,
        stake=Decimal("10"),
        candidate=candidate,
        groups=(group,),
        forecasts=(forecast,),
        evidence=(evidence,),
    )
    return event, latest, ResearchStrategyPlan((instruction,), "0" * 64)


class ResearchMarketStatusTruthTests(unittest.TestCase):
    def test_preflight_rejects_suspended_candidate(self) -> None:
        event, _latest, plan = _suspended_plan()
        with self.assertRaisesRegex(ValueError, "not open market state"):
            plan.preflight([event])

    def test_runtime_rejects_suspended_candidate_before_economic_mutation(self) -> None:
        event, latest, plan = _suspended_plan()
        pipeline = _FailIfCalledPipeline()
        agent = ResearchReplayAgent(plan, pipeline=pipeline)

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "decisions.jsonl"
            book = PaperBook("1000")
            context = AgentContext(
                paper_book=book,
                latest_quotes=latest,
                replay_run_id="suspended-market-test",
                decision_ledger=JsonlDecisionLedger(ledger_path),
            )

            with self.assertRaisesRegex(ValueError, "not open market state"):
                agent.on_market_event(event, context)

            self.assertFalse(pipeline.called)
            self.assertEqual(book.balance, Decimal("1000"))
            self.assertEqual(book.tickets, {})
            self.assertFalse(ledger_path.exists())


if __name__ == "__main__":
    unittest.main()
