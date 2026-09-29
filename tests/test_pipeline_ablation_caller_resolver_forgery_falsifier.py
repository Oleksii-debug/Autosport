from __future__ import annotations

import unittest
from decimal import Decimal

from autosport.pipeline_ablation import (
    AblationObservation,
    AblationProtocol,
    ClaimKind,
    ComponentBinding,
    IdentifiabilityTier,
    MetricDirection,
    MetricSemantics,
    PipelineComponent,
    ResolvedAblationAuthority,
    evaluate_pipeline_ablation,
)


def h(char: str) -> str:
    return char * 64


class _CallerForgedResolver:
    """Deliberately caller-constructed resolver with no product-owned provenance."""

    def __init__(
        self,
        protocol: AblationProtocol,
        observations: tuple[AblationObservation, ...],
    ) -> None:
        self._values: dict[tuple[str, str], ResolvedAblationAuthority] = {}
        for observation in observations:
            authority_sha256 = observation.replay_authority_sha256
            if authority_sha256 is None:
                raise AssertionError("test requires replay authority references")
            self._values[(authority_sha256, observation.evidence_sha256)] = ResolvedAblationAuthority(
                authority_sha256=authority_sha256,
                evidence_sha256=observation.evidence_sha256,
                identifiability_tier=IdentifiabilityTier.FROZEN_REPLAY_COUNTERFACTUAL,
                scope_id=protocol.scope_id,
                dataset_manifest_sha256=protocol.dataset_manifest_sha256,
                holdout_access_sha256=protocol.holdout_access_sha256,
                causal_cutoff=protocol.causal_cutoff,
                available_at=observation.evidence_available_at,
            )

    def resolve(
        self,
        authority_sha256: str,
        evidence_sha256: str,
    ) -> ResolvedAblationAuthority | None:
        return self._values.get((authority_sha256, evidence_sha256))


class PipelineAblationCallerResolverForgeryFalsifier(unittest.TestCase):
    def test_caller_constructed_resolver_cannot_mint_positive_ablation_authority(self) -> None:
        protocol = AblationProtocol(
            protocol_id="caller-resolver-forgery",
            research_question_id="rq-caller-resolver-forgery",
            hypothesis_id="hyp-caller-resolver-forgery",
            source_sha256=h("0"),
            candidate_id="candidate",
            champion_id="champion",
            dataset_manifest_sha256=h("c"),
            holdout_access_sha256=h("d"),
            causal_cutoff="2026-09-20T00:00:00Z",
            evaluation_as_of="2026-09-21T12:00:00Z",
            scope_id="table-tennis:provider-a:match-odds",
            component_bindings=(
                ComponentBinding(
                    component=PipelineComponent.MODEL,
                    candidate_sha256=h("1"),
                    baseline_sha256=h("a"),
                ),
            ),
            claim_kind=ClaimKind.FORECAST,
            primary_metric="brier_score",
            metric_semantics=MetricSemantics.PROPER_FORECAST_SCORE,
            metric_direction=MetricDirection.MAXIMIZE,
            practical_threshold=Decimal("0.01"),
            guardrail_sha256s=(h("e"),),
            uncertainty_method="paired-bootstrap-v1",
            missing_outcome_policy="fail-inconclusive-v1",
            multiplicity_rule="holm-v1",
            created_at="2026-09-19T12:00:00Z",
        )

        observations = (
            AblationObservation(
                enabled_components=(),
                metric_value=Decimal("0"),
                uncertainty_low=Decimal("-0.1"),
                uncertainty_high=Decimal("0.1"),
                evidence_sha256=h("1"),
                factual_evidence_sha256=None,
                evidence_available_at="2026-09-21T11:00:00Z",
                eligible_count=10,
                selected_count=10,
                resolved_count=10,
                pending_count=0,
                void_count=0,
                missing_count=0,
                applicable_costs_complete=True,
                replay_authority_sha256=h("7"),
            ),
            AblationObservation(
                enabled_components=(PipelineComponent.MODEL,),
                metric_value=Decimal("1"),
                uncertainty_low=Decimal("0.9"),
                uncertainty_high=Decimal("1.1"),
                evidence_sha256=h("2"),
                factual_evidence_sha256=None,
                evidence_available_at="2026-09-21T11:00:00Z",
                eligible_count=10,
                selected_count=10,
                resolved_count=10,
                pending_count=0,
                void_count=0,
                missing_count=0,
                applicable_costs_complete=True,
                replay_authority_sha256=h("7"),
            ),
        )

        forged = _CallerForgedResolver(protocol, observations)

        # A caller-authored Protocol implementation plus a caller-constructed
        # ResolvedAblationAuthority DTO is not product provenance. Positive
        # scientific credit must require a product-owned resolver/issuer whose
        # authority can be independently re-resolved.
        with self.assertRaises(ValueError):
            evaluate_pipeline_ablation(
                protocol,
                observations,
                authority_resolver=forged,
            )


if __name__ == "__main__":
    unittest.main()
