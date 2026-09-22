from __future__ import annotations

import unittest
from decimal import Decimal

from autosport.pipeline_ablation import (
    AblationObservation,
    AblationProtocol,
    ClaimKind,
    ComponentBinding,
    MetricDirection,
    MetricSemantics,
    PipelineComponent,
    evaluate_pipeline_ablation,
)


def h(char: str) -> str:
    return char * 64


def protocol(component: PipelineComponent) -> AblationProtocol:
    return AblationProtocol(
        protocol_id="eligible-universe-falsifier-v1",
        research_question_id="rq-eligible-universe",
        hypothesis_id="hyp-eligible-universe",
        source_sha256=h("0"),
        candidate_id="candidate-eligible-universe",
        champion_id="champion-eligible-universe",
        dataset_manifest_sha256=h("c"),
        holdout_access_sha256=h("d"),
        causal_cutoff="2026-09-20T00:00:00Z",
        evaluation_as_of="2026-09-21T12:00:00Z",
        scope_id="table-tennis:provider-a:match-odds",
        component_bindings=(
            ComponentBinding(
                component=component,
                candidate_sha256=h("1"),
                baseline_sha256=h("a"),
            ),
        ),
        claim_kind=ClaimKind.FORECAST,
        primary_metric="brier_score",
        metric_semantics=MetricSemantics.PROPER_FORECAST_SCORE,
        metric_direction=MetricDirection.MINIMIZE,
        practical_threshold=Decimal("0.01"),
        guardrail_sha256s=(h("e"),),
        uncertainty_method="paired-bootstrap-v1",
        missing_outcome_policy="fail-inconclusive-v1",
        multiplicity_rule="holm-v1",
        created_at="2026-09-19T12:00:00Z",
    )


def observation(
    enabled: tuple[PipelineComponent, ...],
    *,
    eligible: int,
    selected: int,
    evidence_char: str,
    value: str,
) -> AblationObservation:
    return AblationObservation(
        enabled_components=enabled,
        metric_value=Decimal(value),
        uncertainty_low=Decimal(value) - Decimal("0.01"),
        uncertainty_high=Decimal(value) + Decimal("0.01"),
        evidence_sha256=h(evidence_char),
        factual_evidence_sha256=None,
        evidence_available_at="2026-09-21T11:00:00Z",
        eligible_count=eligible,
        selected_count=selected,
        resolved_count=selected,
        pending_count=0,
        void_count=0,
        missing_count=0,
        replay_authority_sha256=h("7"),
    )


class PipelineAblationEligibleUniverseFalsifierTests(unittest.TestCase):
    def test_factorial_rejects_different_eligible_denominators(self) -> None:
        candidate = protocol(PipelineComponent.MODEL)

        with self.assertRaisesRegex(ValueError, "eligible.*(cohort|universe|denominator)"):
            evaluate_pipeline_ablation(
                candidate,
                (
                    observation((), eligible=100, selected=100, evidence_char="1", value="0.20"),
                    observation(
                        (PipelineComponent.MODEL,),
                        eligible=60,
                        selected=60,
                        evidence_char="2",
                        value="0.10",
                    ),
                ),
            )

    def test_threshold_arm_may_change_selection_with_same_eligible_universe(self) -> None:
        candidate = protocol(PipelineComponent.THRESHOLD_SELECTION)

        evidence = evaluate_pipeline_ablation(
            candidate,
            (
                observation((), eligible=100, selected=80, evidence_char="3", value="0.20"),
                observation(
                    (PipelineComponent.THRESHOLD_SELECTION,),
                    eligible=100,
                    selected=40,
                    evidence_char="4",
                    value="0.15",
                ),
            ),
        )

        self.assertTrue(evidence.complete_factorial)
        self.assertEqual(evidence.observations[0].eligible_count, 100)
        self.assertEqual(evidence.observations[1].eligible_count, 100)


if __name__ == "__main__":
    unittest.main()
