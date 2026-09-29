from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.voc_evaluation import (
    PairedVOCEvaluation,
    VOCEvaluationProvenance,
    VOCEvaluationStore,
)
from autosport.voc_production_orchestrator import causal_matching_voc_history

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


class VOCCausalHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = VOCEvaluationStore(
            Path(self.tempdir.name) / "voc-evaluations.json"
        )
        self.admission = {
            "task_class": "forecast",
            "research_protocol_id": "protocol-1",
            "scope": {
                "sport_id": "table-tennis",
                "league_id": "league-1",
                "regime_id": "regime-1",
                "urgency_id": "routine",
                "contradiction_state": "none",
            },
            "baseline_compute_identity": {
                "candidate_id": "baseline",
                "backend_id": "local-backend",
                "model_id": "baseline-model",
                "config_sha256": SHA_A,
            },
            "challenger_compute_identity": {
                "candidate_id": "challenger",
                "backend_id": "cloud-backend",
                "model_id": "challenger-model",
                "config_sha256": SHA_B,
            },
        }

    @staticmethod
    def evaluation(
        evaluation_id: str,
        *,
        evaluated_at: str,
        regime_id: str = "regime-1",
        challenger_utility: str = "0.5",
    ) -> PairedVOCEvaluation:
        decision_at = "2026-09-20T00:00:00Z"
        completed = "2026-09-20T00:00:01Z"
        outcome_at = "2026-09-20T00:00:02Z"
        net = Decimal(challenger_utility) - Decimal("1") - Decimal("0.1")
        return PairedVOCEvaluation(
            evaluation_id=evaluation_id,
            task_class="forecast",
            sport_id="table-tennis",
            league_id="league-1",
            regime_id=regime_id,
            urgency_id="routine",
            contradiction_state="none",
            baseline_candidate_id="baseline",
            baseline_backend_id="local-backend",
            baseline_model_id="baseline-model",
            baseline_config_sha256=SHA_A,
            challenger_candidate_id="challenger",
            challenger_backend_id="cloud-backend",
            challenger_model_id="challenger-model",
            challenger_config_sha256=SHA_B,
            decision_input_sha256=SHA_C,
            decision_context_sha256=SHA_D,
            decision_evidence_sha256=SHA_E,
            baseline_output_sha256=SHA_A,
            challenger_output_sha256=SHA_B,
            baseline_action="LOCAL_ACTION",
            challenger_action="CLOUD_ACTION",
            baseline_abstained=False,
            challenger_abstained=False,
            decision_at=decision_at,
            decision_deadline="2026-09-20T00:00:03Z",
            baseline_completed_at=completed,
            challenger_completed_at=completed,
            outcome_evidence_sha256=SHA_C,
            outcome_revealed_at=outcome_at,
            evaluated_at=evaluated_at,
            scoring_rule_id="score-v1",
            scoring_rule_sha256=SHA_D,
            research_protocol_id="protocol-1",
            research_protocol_sha256=SHA_E,
            holdout_access_id=f"holdout-{evaluation_id}",
            multiple_comparison_control_sha256=SHA_C,
            baseline_utility=Decimal("1"),
            challenger_utility=Decimal(challenger_utility),
            compute_cost_penalty=Decimal("0.1"),
            latency_opportunity_cost_penalty=Decimal("0"),
            measured_compute_cost=Decimal("0.1"),
            paired_sample_count=1,
            effective_sample_size=1,
            support_fraction=Decimal("1"),
            incremental_value_interval_low=net,
            incremental_value_interval_high=net,
            provenance=VOCEvaluationProvenance.MEASURED_SHADOW,
        )

    def test_history_excludes_future_and_regime_mixing_but_preserves_harmful_result(self) -> None:
        harmful = self.evaluation(
            "harmful-prior",
            evaluated_at="2026-09-20T00:00:05Z",
            challenger_utility="0.5",
        )
        future = self.evaluation(
            "future-positive",
            evaluated_at="2026-09-20T00:00:20Z",
            challenger_utility="2",
        )
        wrong_regime = self.evaluation(
            "wrong-regime",
            evaluated_at="2026-09-20T00:00:04Z",
            regime_id="regime-2",
            challenger_utility="2",
        )
        for value in (harmful, future, wrong_regime):
            self.store.record(value)

        history = causal_matching_voc_history(
            self.store,
            precompute_admission=self.admission,
            as_of="2026-09-20T00:00:10Z",
            require_canonical=False,
        )
        self.assertEqual([item.evaluation_id for item in history], ["harmful-prior"])
        self.assertLess(history[0].net_value, Decimal("0"))

    def test_equal_cutoff_is_not_visible_to_same_boundary(self) -> None:
        value = self.evaluation(
            "same-boundary",
            evaluated_at="2026-09-20T00:00:10Z",
        )
        self.store.record(value)
        self.assertEqual(
            causal_matching_voc_history(
                self.store,
                precompute_admission=self.admission,
                as_of="2026-09-20T00:00:10Z",
                require_canonical=False,
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()
