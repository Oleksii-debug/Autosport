from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.candidate_search import CandidateLeg, ParlayCandidate
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.forecasting import ForecastRecord
from autosport.paper import PaperBook
from autosport.research_pipeline import (
    ResearchDecisionPipeline,
    ResearchDecisionPolicy,
    ResearchEvidence,
)
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome


QUOTE_KEY = "match-1|winner|B"
SNAPSHOT = "a" * 64
EVIDENCE_HASH = "b" * 64


class _BombOptimizer:
    def evaluate_candidates(self, *args, **kwargs):
        raise AssertionError("optimizer must not receive an invalid research stake")


class ResearchPipelineFiniteDecimalTests(unittest.TestCase):
    def _evidence(self, odds) -> ResearchEvidence:
        return ResearchEvidence(
            evidence_id="evidence-1",
            quote_key=QUOTE_KEY,
            source_id="provider",
            observed_at="2026-09-14T10:00:00+00:00",
            available_at="2026-09-14T10:00:01+00:00",
            decimal_odds=odds,
            content_sha256=EVIDENCE_HASH,
            market_snapshot_hash=SNAPSHOT,
        )

    def _candidate(self) -> ParlayCandidate:
        leg = CandidateLeg(
            QUOTE_KEY,
            "match-1",
            Decimal("2"),
            Decimal("0.5"),
        )
        return ParlayCandidate(
            (leg,),
            Decimal("2"),
            Decimal("0.5"),
            Decimal("0"),
        )

    def _forecast(self) -> ForecastRecord:
        return ForecastRecord(
            quote_key=QUOTE_KEY,
            probability=Decimal("0.5"),
            model_id="model",
            model_version="1",
            strategy_version="research-v1",
            model_training_cutoff_ts="2026-09-14T09:00:00+00:00",
            input_cutoff_ts="2026-09-14T10:00:01+00:00",
            generated_at="2026-09-14T10:00:02+00:00",
            uncertainty=Decimal("0.1"),
            evidence_hashes=(EVIDENCE_HASH,),
            market_snapshot_hash=SNAPSHOT,
            provenance={"source": "finite-boundary-test"},
        )

    def test_evidence_rejects_non_finite_odds_deterministically(self) -> None:
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "evidence decimal odds must be a finite decimal",
                ):
                    self._evidence(value)

    def test_policy_rejects_non_finite_decimal_fields(self) -> None:
        fields = (
            "max_forecast_uncertainty",
            "minimum_ranking_risk_change",
            "minimum_standalone_expected_profit_per_unit",
        )
        for field in fields:
            for value in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, f"{field} must be a finite decimal"):
                        ResearchDecisionPolicy(**{field: value})

    def test_valid_negative_policy_thresholds_remain_supported(self) -> None:
        policy = ResearchDecisionPolicy(
            minimum_ranking_risk_change="-2.5",
            minimum_standalone_expected_profit_per_unit="-0.25",
        )
        self.assertEqual(policy.minimum_ranking_risk_change, Decimal("-2.5"))
        self.assertEqual(
            policy.minimum_standalone_expected_profit_per_unit,
            Decimal("-0.25"),
        )

    def test_non_finite_stake_is_rejected_before_optimizer_or_mutation(self) -> None:
        candidate = self._candidate()
        evidence = self._evidence("2")
        forecast = self._forecast()
        groups = [
            ScenarioGroup(
                "match-1-winner",
                (
                    ScenarioOutcome(QUOTE_KEY, Decimal("0.5")),
                    ScenarioOutcome("match-1|winner|A", Decimal("0.5")),
                ),
            )
        ]

        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                book = PaperBook("100")
                ledger = JsonlDecisionLedger(Path(tmp) / "research-decisions.jsonl")
                pipeline = ResearchDecisionPipeline(optimizer=_BombOptimizer())  # type: ignore[arg-type]

                with self.assertRaisesRegex(ValueError, "stake must be a finite decimal"):
                    pipeline.decide_and_open(
                        book=book,
                        candidate=candidate,
                        groups=groups,
                        forecasts={QUOTE_KEY: forecast},
                        evidence=(evidence,),
                        stake=value,
                        decision_ts="2026-09-14T10:00:03+00:00",
                        decision_ledger=ledger,
                        replay_run_id="finite-stake-test",
                    )

                self.assertEqual(book.tickets, {})
                self.assertFalse(ledger.path.exists())


if __name__ == "__main__":
    unittest.main()
