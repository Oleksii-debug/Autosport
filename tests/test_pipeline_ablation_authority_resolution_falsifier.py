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


def protocol_for(
    component: PipelineComponent,
    *,
    claim_kind: ClaimKind,
    metric_semantics: MetricSemantics,
) -> AblationProtocol:
    return AblationProtocol(
        protocol_id=f"authority-resolution-{component.value.lower()}",
        research_question_id="rq-authority-resolution",
        hypothesis_id="hyp-authority-resolution",
        source_sha256=h("0"),
        candidate_id="candidate-authority-resolution",
        champion_id="champion-authority-resolution",
        dataset_manifest_sha256=h("d"),
        holdout_access_sha256=h("e"),
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
        claim_kind=claim_kind,
        primary_metric=(
            "execution_drag"
            if claim_kind is ClaimKind.EXECUTION
            else "net_utility"
            if claim_kind is ClaimKind.ECONOMIC
            else "decision_quality"
            if claim_kind is ClaimKind.DECISION
            else "brier_score"
        ),
        metric_semantics=metric_semantics,
        metric_direction=MetricDirection.MAXIMIZE,
        practical_threshold=Decimal("0.01"),
        guardrail_sha256s=(h("f"),),
        uncertainty_method="paired-bootstrap-v1",
        missing_outcome_policy="fail-inconclusive-v1",
        multiplicity_rule="holm-v1",
        created_at="2026-09-19T12:00:00Z",
    )


def observation(
    enabled_components: tuple[PipelineComponent, ...],
    value: str,
    *,
    evidence_sha256: str,
    factual_evidence_sha256: str | None = None,
    execution_receipt_sha256: str | None = None,
    simulator_sha256: str | None = None,
    assignment_evidence_sha256: str | None = None,
    replay_authority_sha256: str | None = None,
    assumptions: tuple[str, ...] = (),
) -> AblationObservation:
    metric = Decimal(value)
    return AblationObservation(
        enabled_components=enabled_components,
        metric_value=metric,
        uncertainty_low=metric - Decimal("0.1"),
        uncertainty_high=metric + Decimal("0.1"),
        evidence_sha256=evidence_sha256,
        factual_evidence_sha256=factual_evidence_sha256,
        evidence_available_at="2026-09-21T11:00:00Z",
        eligible_count=10,
        selected_count=10,
        resolved_count=10,
        pending_count=0,
        void_count=0,
        missing_count=0,
        applicable_costs_complete=True,
        execution_receipt_sha256=execution_receipt_sha256,
        simulator_sha256=simulator_sha256,
        assignment_evidence_sha256=assignment_evidence_sha256,
        replay_authority_sha256=replay_authority_sha256,
        assumptions=assumptions,
    )


class PipelineAblationAuthorityResolutionFalsifier(unittest.TestCase):
    """Arbitrary canonical-looking digests must not mint scientific authority."""

    def test_unattested_replay_digest_cannot_mint_counterfactual_credit(self) -> None:
        protocol = protocol_for(
            PipelineComponent.MODEL,
            claim_kind=ClaimKind.FORECAST,
            metric_semantics=MetricSemantics.PROPER_FORECAST_SCORE,
        )
        forged = h("7")
        with self.assertRaises(ValueError):
            evaluate_pipeline_ablation(
                protocol,
                (
                    observation((), "0", evidence_sha256=h("1"), replay_authority_sha256=forged),
                    observation(
                        (PipelineComponent.MODEL,),
                        "1",
                        evidence_sha256=h("2"),
                        replay_authority_sha256=forged,
                    ),
                ),
            )

    def test_unattested_factual_and_receipt_digests_cannot_mint_execution_credit(self) -> None:
        protocol = protocol_for(
            PipelineComponent.EXECUTION,
            claim_kind=ClaimKind.EXECUTION,
            metric_semantics=MetricSemantics.EXECUTION_DRAG,
        )
        forged_factual = h("8")
        forged_receipt = h("4")
        with self.assertRaises(ValueError):
            evaluate_pipeline_ablation(
                protocol,
                (
                    observation(
                        (),
                        "0",
                        evidence_sha256=h("1"),
                        factual_evidence_sha256=forged_factual,
                        execution_receipt_sha256=forged_receipt,
                    ),
                    observation(
                        (PipelineComponent.EXECUTION,),
                        "1",
                        evidence_sha256=h("2"),
                        factual_evidence_sha256=forged_factual,
                        execution_receipt_sha256=forged_receipt,
                    ),
                ),
            )

    def test_unattested_assignment_digest_cannot_mint_forward_causal_credit(self) -> None:
        protocol = protocol_for(
            PipelineComponent.MODEL,
            claim_kind=ClaimKind.FORECAST,
            metric_semantics=MetricSemantics.PROPER_FORECAST_SCORE,
        )
        forged = h("5")
        with self.assertRaises(ValueError):
            evaluate_pipeline_ablation(
                protocol,
                (
                    observation((), "0", evidence_sha256=h("1"), assignment_evidence_sha256=forged),
                    observation(
                        (PipelineComponent.MODEL,),
                        "1",
                        evidence_sha256=h("2"),
                        assignment_evidence_sha256=forged,
                    ),
                ),
            )

    def test_unattested_simulator_digest_cannot_mint_simulated_credit(self) -> None:
        protocol = protocol_for(
            PipelineComponent.MODEL,
            claim_kind=ClaimKind.FORECAST,
            metric_semantics=MetricSemantics.PROPER_FORECAST_SCORE,
        )
        forged = h("6")
        with self.assertRaises(ValueError):
            evaluate_pipeline_ablation(
                protocol,
                (
                    observation(
                        (),
                        "0",
                        evidence_sha256=h("1"),
                        simulator_sha256=forged,
                        assumptions=("no-market-impact",),
                    ),
                    observation(
                        (PipelineComponent.MODEL,),
                        "1",
                        evidence_sha256=h("2"),
                        simulator_sha256=forged,
                        assumptions=("no-market-impact",),
                    ),
                ),
            )


if __name__ == "__main__":
    unittest.main()
