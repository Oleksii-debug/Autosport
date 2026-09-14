import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.decision_ledger import JsonlDecisionLedger
from autosport.paper import PaperBook
from autosport.research_pipeline import (
    ResearchDecisionPipeline,
    ResearchDecisionPolicy,
    ResearchEvidence,
)


CONTENT_HASH = "b" * 64
SNAPSHOT_HASH = "a" * 64


class ResearchPipelineInputIntegrityTests(unittest.TestCase):
    def _evidence_kwargs(self):
        return {
            "evidence_id": "evidence-1",
            "quote_key": "match-1|winner|A",
            "source_id": "provider",
            "observed_at": "2026-09-14T10:00:00+00:00",
            "available_at": "2026-09-14T10:00:01+00:00",
            "decimal_odds": Decimal("2.00"),
            "content_sha256": CONTENT_HASH,
            "market_snapshot_hash": SNAPSHOT_HASH,
        }

    def test_research_evidence_rejects_nonfinite_and_invalid_decimal_odds(self):
        for value in ("NaN", "Infinity", "-Infinity", "not-a-decimal"):
            with self.subTest(value=value):
                kwargs = self._evidence_kwargs()
                kwargs["decimal_odds"] = value
                with self.assertRaisesRegex(
                    ValueError,
                    "evidence decimal odds must be a finite decimal",
                ):
                    ResearchEvidence(**kwargs)

    def test_research_evidence_requires_canonical_string_identity_and_timestamps(self):
        for field, value in (
            ("evidence_id", " evidence-1"),
            ("quote_key", "match-1|winner|A "),
            ("source_id", "\t"),
            ("observed_at", 123),
            ("available_at", " "),
        ):
            with self.subTest(field=field, value=value):
                kwargs = self._evidence_kwargs()
                kwargs[field] = value
                with self.assertRaisesRegex(ValueError, field):
                    ResearchEvidence(**kwargs)

    def test_policy_rejects_nonfinite_numeric_configuration(self):
        for field in (
            "max_forecast_uncertainty",
            "minimum_ranking_risk_change",
            "minimum_standalone_expected_profit_per_unit",
        ):
            for value in ("NaN", "Infinity", "-Infinity", "not-a-decimal"):
                with self.subTest(field=field, value=value):
                    kwargs = {field: value}
                    with self.assertRaisesRegex(ValueError, field):
                        ResearchDecisionPolicy(**kwargs)

    def test_policy_requires_positive_integer_evidence_count(self):
        for value in (True, False, 0, -1, 1.5, Decimal("2"), "2"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "minimum_evidence_per_leg must be a positive integer",
                ):
                    ResearchDecisionPolicy(minimum_evidence_per_leg=value)

        policy = ResearchDecisionPolicy(minimum_evidence_per_leg=2)
        self.assertEqual(policy.minimum_evidence_per_leg, 2)

    def test_pipeline_rejects_nonfinite_stake_before_touching_candidate_or_ledger(self):
        pipeline = ResearchDecisionPipeline()
        book = PaperBook("100")
        for stake in ("NaN", "Infinity", "-Infinity", "not-a-decimal"):
            with self.subTest(stake=stake), tempfile.TemporaryDirectory() as tmp:
                ledger = JsonlDecisionLedger(Path(tmp) / "decisions.jsonl")
                with self.assertRaisesRegex(ValueError, "stake must be a finite decimal"):
                    pipeline.decide_and_open(
                        book=book,
                        candidate=None,
                        groups=[],
                        forecasts={},
                        evidence=(),
                        stake=stake,
                        decision_ts="2026-09-14T10:00:00+00:00",
                        decision_ledger=ledger,
                        replay_run_id="research-run",
                    )
                self.assertEqual(book.balance, Decimal("100"))
                self.assertEqual(book.tickets, {})
                self.assertFalse(ledger.path.exists())

    def test_pipeline_rejects_noncanonical_audit_identity_before_economic_work(self):
        pipeline = ResearchDecisionPipeline()
        book = PaperBook("100")
        for replay_run_id in ("", " ", " research-run", 123):
            with self.subTest(replay_run_id=replay_run_id), tempfile.TemporaryDirectory() as tmp:
                ledger = JsonlDecisionLedger(Path(tmp) / "decisions.jsonl")
                with self.assertRaisesRegex(ValueError, "replay_run_id"):
                    pipeline.decide_and_open(
                        book=book,
                        candidate=None,
                        groups=[],
                        forecasts={},
                        evidence=(),
                        stake="10",
                        decision_ts="2026-09-14T10:00:00+00:00",
                        decision_ledger=ledger,
                        replay_run_id=replay_run_id,
                    )
                self.assertEqual(book.balance, Decimal("100"))
                self.assertEqual(book.tickets, {})
                self.assertFalse(ledger.path.exists())


if __name__ == "__main__":
    unittest.main()
