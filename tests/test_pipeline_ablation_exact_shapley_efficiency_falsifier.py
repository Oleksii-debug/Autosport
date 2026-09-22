from __future__ import annotations

import itertools
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


def protocol() -> AblationProtocol:
    components = (
        PipelineComponent.DATA,
        PipelineComponent.MODEL,
        PipelineComponent.THRESHOLD_SELECTION,
    )
    return AblationProtocol(
        protocol_id="shapley-exact-efficiency",
        research_question_id="rq-shapley-exact-efficiency",
        hypothesis_id="hyp-shapley-exact-efficiency",
        source_sha256=h("0"),
        candidate_id="candidate-shapley-exact-efficiency",
        champion_id="champion-shapley-exact-efficiency",
        dataset_manifest_sha256=h("d"),
        holdout_access_sha256=h("e"),
        causal_cutoff="2026-09-20T00:00:00Z",
        evaluation_as_of="2026-09-21T12:00:00Z",
        scope_id="table-tennis:provider-a:match-odds",
        component_bindings=(
            ComponentBinding(PipelineComponent.DATA, h("1"), h("a")),
            ComponentBinding(PipelineComponent.MODEL, h("2"), h("b")),
            ComponentBinding(PipelineComponent.THRESHOLD_SELECTION, h("3"), h("c")),
        ),
        claim_kind=ClaimKind.FORECAST,
        primary_metric="brier_score",
        metric_semantics=MetricSemantics.PROPER_FORECAST_SCORE,
        metric_direction=MetricDirection.MAXIMIZE,
        practical_threshold=Decimal("0.01"),
        guardrail_sha256s=(h("f"),),
        uncertainty_method="paired-bootstrap-v1",
        missing_outcome_policy="fail-inconclusive-v1",
        multiplicity_rule="holm-v1",
        created_at="2026-09-19T12:00:00Z",
    )


def observation(
    enabled: tuple[PipelineComponent, ...],
    value: Decimal,
    *,
    evidence_sha256: str,
) -> AblationObservation:
    return AblationObservation(
        enabled_components=enabled,
        metric_value=value,
        uncertainty_low=value,
        uncertainty_high=value,
        evidence_sha256=evidence_sha256,
        factual_evidence_sha256=None,
        evidence_available_at="2026-09-21T11:00:00Z",
        eligible_count=10,
        selected_count=10,
        resolved_count=10,
        pending_count=0,
        void_count=0,
        missing_count=0,
        applicable_costs_complete=True,
        replay_authority_sha256=h("9"),
    )


class PipelineAblationExactShapleyEfficiencyFalsifier(unittest.TestCase):
    def test_symmetric_three_component_allocation_sums_exactly_to_total_effect(self) -> None:
        frozen = protocol()
        components = frozen.components
        coalitions = tuple(
            tuple(items)
            for size in range(len(components) + 1)
            for items in itertools.combinations(components, size)
        )
        observations = tuple(
            observation(
                coalition,
                Decimal("1") if coalition == components else Decimal("0"),
                evidence_sha256=h(format(index + 1, "x")),
            )
            for index, coalition in enumerate(coalitions)
        )

        evidence = evaluate_pipeline_ablation(frozen, observations)

        self.assertTrue(evidence.complete_factorial)
        self.assertEqual(evidence.total_effect, Decimal("1"))
        contributions = tuple(item.contribution for item in evidence.findings)
        self.assertTrue(all(value is not None for value in contributions))
        allocated = sum(
            (value for value in contributions if value is not None),
            Decimal(0),
        )
        self.assertEqual(
            allocated,
            evidence.total_effect,
            "A value advertised as a Shapley allocation must satisfy exact efficiency; "
            "finite Decimal 1/3 weights must not create or lose scientific effect mass",
        )


if __name__ == "__main__":
    unittest.main()
