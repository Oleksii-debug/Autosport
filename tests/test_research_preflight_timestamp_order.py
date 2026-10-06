import unittest
from decimal import Decimal

from autosport.candidate_search import CandidateLeg, ParlayCandidate
from autosport.domain import MarketEvent
from autosport.forecasting import ForecastRecord
from autosport.research_pipeline import ResearchEvidence
from autosport.research_strategy import (
    ResearchReplayInstruction,
    ResearchStrategyPlan,
    market_event_evidence_hash,
    research_market_snapshot_hash,
)
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome


class ResearchPreflightTimestampOrderTests(unittest.TestCase):
    @staticmethod
    def _event(event_id: str, observed_ts: str, sequence: int) -> MarketEvent:
        return MarketEvent.from_dict(
            {
                "event_id": event_id,
                "market_id": "winner",
                "selection_id": "yes",
                "decimal_odds": "2.0",
                "observed_ts": observed_ts,
                "source_id": "source",
                "sequence": sequence,
            }
        )

    def test_preflight_uses_same_absolute_chronology_as_replay(self):
        earlier = self._event("earlier", "2026-01-01T01:00:00+01:00", 1)
        trigger = self._event("trigger", "2026-01-01T00:30:00+00:00", 2)
        latest = {earlier.quote_key: earlier, trigger.quote_key: trigger}
        snapshot_hash = research_market_snapshot_hash(latest, latest)

        legs = tuple(
            CandidateLeg(event.quote_key, event.event_id, event.decimal_odds, Decimal("0.50"))
            for event in (earlier, trigger)
        )
        candidate = ParlayCandidate(
            legs,
            Decimal("4.0"),
            Decimal("0.25"),
            Decimal("0"),
        )

        forecasts = tuple(
            ForecastRecord(
                quote_key=event.quote_key,
                probability=Decimal("0.50"),
                model_id="model",
                model_version="1",
                strategy_version="research-replay-v1",
                model_training_cutoff_ts="2025-12-31T23:00:00+00:00",
                input_cutoff_ts=trigger.observed_ts,
                generated_at=trigger.observed_ts,
                evidence_hashes=(market_event_evidence_hash(event),),
                market_snapshot_hash=snapshot_hash,
                forecast_id=f"forecast-{event.event_id}",
            )
            for event in (earlier, trigger)
        )
        evidence = tuple(
            ResearchEvidence(
                evidence_id=f"evidence-{event.event_id}",
                quote_key=event.quote_key,
                source_id=event.source_id,
                observed_at=event.observed_ts,
                available_at=event.observed_ts,
                decimal_odds=event.decimal_odds,
                content_sha256=market_event_evidence_hash(event),
                market_snapshot_hash=snapshot_hash,
            )
            for event in (earlier, trigger)
        )
        group = ScenarioGroup(
            "placeholder",
            (
                ScenarioOutcome("outcome-a", Decimal("0.5")),
                ScenarioOutcome("outcome-b", Decimal("0.5")),
            ),
        )
        instruction = ResearchReplayInstruction(
            decision_id="decision",
            trigger_quote_key=trigger.quote_key,
            decision_ts=trigger.observed_ts,
            stake=Decimal("1"),
            candidate=candidate,
            groups=(group,),
            forecasts=forecasts,
            evidence=evidence,
        )
        plan = ResearchStrategyPlan((instruction,), "0" * 64)

        plan.preflight([trigger, earlier])


if __name__ == "__main__":
    unittest.main()
