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


A = "match-1|winner|A"
B = "match-1|winner|B"
CONTENT_HASH = "b" * 64
SNAPSHOT_HASH = "a" * 64


class ResearchPipelineInputIntegrityTests(unittest.TestCase):
    def _evidence_kwargs(self):
        return {
            "evidence_id": "evidence-1",
            "quote_key": B,
            "source_id": "provider",
            "observed_at": "2026-09-14T10:00:00+00:00",
            "available_at": "2026-09-14T10:00:01+00:00",
            "decimal_odds": Decimal("2.00"),
            "content_sha256": CONTENT_HASH,
            "market_snapshot_hash": SNAPSHOT_HASH,
        }

    def _approved_inputs(self):
        probability = Decimal("0.50")
        odds = Decimal("2.00")
        candidate = ParlayCandidate(
            (CandidateLeg(B, "match-1", odds, probability),),
            odds,
            probability,
            Decimal("0"),
        )
        groups = [
            ScenarioGroup(
                "match-1-winner",
                (
                    ScenarioOutcome(A, Decimal("0.50")),
                    ScenarioOutcome(B, Decimal("0.50")),
                ),
            )
        ]
        forecast = ForecastRecord(
            quote_key=B,
            probability=probability,
            model_id="tt-model",
            model_version="1.0.0",
            strategy_version="research-v1",
            model_training_cutoff_ts="2026-09-14T09:00:00+00:00",
            input_cutoff_ts="2026-09-14T10:00:01+00:00",
            generated_at="2026-09-14T10:00:02+00:00",
            uncertainty=Decimal("0.10"),
            evidence_hashes=(CONTENT_HASH,),
            market_snapshot_hash=SNAPSHOT_HASH,
            provenance={"source": "typed-test"},
        )
        evidence = ResearchEvidence(**self._evidence_kwargs())
        return candidate, groups, {B: forecast}, (evidence,)

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

    def test_research_evidence_rejects_non_utf8_hash_relevant_text(self):
        for field in ("evidence_id", "quote_key", "source_id"):
            with self.subTest(field=field):
                kwargs = self._evidence_kwargs()
                kwargs[field] = "\ud800"
                with self.assertRaisesRegex(ValueError, "valid UTF-8"):
                    ResearchEvidence(**kwargs)

        kwargs = self._evidence_kwargs()
        kwargs["quality_flags"] = ("\ud800",)
        with self.assertRaisesRegex(ValueError, "valid UTF-8"):
            ResearchEvidence(**kwargs)

    def test_research_evidence_accepts_valid_non_ascii_utf8_identity(self):
        kwargs = self._evidence_kwargs()
        kwargs["evidence_id"] = "доказ-1"
        kwargs["source_id"] = "провайдер-європа"
        kwargs["quality_flags"] = ("ЯКІСНІ_ДАНІ",)

        evidence = ResearchEvidence(**kwargs)

        self.assertEqual(evidence.evidence_id, "доказ-1")
        self.assertEqual(evidence.source_id, "провайдер-європа")
        self.assertEqual(evidence.quality_flags, ("ЯКІСНІ_ДАНІ",))

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

    def test_policy_requires_real_booleans_for_boolean_gates(self):
        for field in ("require_market_snapshot_hash", "require_worst_case_proof"):
            for value in ("false", "true", 0, 1, None):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, f"{field} must be boolean"):
                        ResearchDecisionPolicy(**{field: value})

        self.assertFalse(
            ResearchDecisionPolicy(require_market_snapshot_hash=False).require_market_snapshot_hash
        )
        self.assertTrue(
            ResearchDecisionPolicy(require_worst_case_proof=True).require_worst_case_proof
        )

    def test_policy_rejects_non_utf8_blocked_quality_flag(self):
        with self.assertRaisesRegex(ValueError, "valid UTF-8"):
            ResearchDecisionPolicy(blocked_quality_flags=frozenset({"\ud800"}))

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

    def test_pipeline_rejects_non_utf8_replay_id_before_approved_economic_path(self):
        pipeline = ResearchDecisionPipeline()
        book = PaperBook("100")
        candidate, groups, forecasts, evidence = self._approved_inputs()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlDecisionLedger(Path(tmp) / "decisions.jsonl")
            with self.assertRaisesRegex(ValueError, "replay_run_id must be valid UTF-8 text"):
                pipeline.decide_and_open(
                    book=book,
                    candidate=candidate,
                    groups=groups,
                    forecasts=forecasts,
                    evidence=evidence,
                    stake="10",
                    decision_ts="2026-09-14T10:00:03+00:00",
                    decision_ledger=ledger,
                    replay_run_id="\ud800",
                )

            self.assertEqual(book.balance, Decimal("100"))
            self.assertEqual(book.tickets, {})
            self.assertFalse(ledger.path.exists())


if __name__ == "__main__":
    unittest.main()
