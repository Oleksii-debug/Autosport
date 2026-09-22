from __future__ import annotations

import unittest
from dataclasses import replace
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
    evaluate_pipeline_ablation as _evaluate_pipeline_ablation,
)


def h(char: str) -> str:
    return char * 64


class _TestAuthorityResolver:
    def __init__(self, values: dict[tuple[str, str], ResolvedAblationAuthority]) -> None:
        self.values = values

    def resolve(self, authority_sha256: str, evidence_sha256: str) -> ResolvedAblationAuthority | None:
        return self.values.get((authority_sha256, evidence_sha256))


def _authority_reference(observation: AblationObservation) -> tuple[IdentifiabilityTier, str]:
    values = (
        (IdentifiabilityTier.FACTUAL_MECHANICAL, observation.factual_evidence_sha256),
        (IdentifiabilityTier.FROZEN_REPLAY_COUNTERFACTUAL, observation.replay_authority_sha256),
        (IdentifiabilityTier.SIMULATED_COUNTERFACTUAL, observation.simulator_sha256),
        (IdentifiabilityTier.FORWARD_RANDOMIZED_OR_PAIRED, observation.assignment_evidence_sha256),
    )
    present = tuple((kind, value) for kind, value in values if value is not None)
    if len(present) != 1:
        raise AssertionError("test observation must have exactly one authority")
    return present[0]


def _resolver_for(
    protocol: AblationProtocol,
    observations: tuple[AblationObservation, ...],
) -> _TestAuthorityResolver:
    values: dict[tuple[str, str], ResolvedAblationAuthority] = {}
    for observation in observations:
        if observation.metric_value is None:
            continue
        tier, authority_sha256 = _authority_reference(observation)
        values[(authority_sha256, observation.evidence_sha256)] = ResolvedAblationAuthority(
            authority_sha256=authority_sha256,
            evidence_sha256=observation.evidence_sha256,
            identifiability_tier=tier,
            scope_id=protocol.scope_id,
            dataset_manifest_sha256=protocol.dataset_manifest_sha256,
            holdout_access_sha256=protocol.holdout_access_sha256,
            causal_cutoff=protocol.causal_cutoff,
            available_at=observation.evidence_available_at,
            execution_receipt_sha256=observation.execution_receipt_sha256,
            assumptions=observation.assumptions if tier is IdentifiabilityTier.SIMULATED_COUNTERFACTUAL else (),
        )
    return _TestAuthorityResolver(values)


def evaluate_pipeline_ablation(
    protocol: AblationProtocol,
    observations: tuple[AblationObservation, ...],
):
    return _evaluate_pipeline_ablation(
        protocol,
        observations,
        authority_resolver=_resolver_for(protocol, observations),
    )


class PipelineAblationTests(unittest.TestCase):
    def protocol(
        self,
        *components: PipelineComponent,
        claim_kind: ClaimKind = ClaimKind.FORECAST,
        direction: MetricDirection = MetricDirection.MAXIMIZE,
        scope_id: str = "table-tennis:provider-a:match-odds",
        holdout: str = h("d"),
    ) -> AblationProtocol:
        ordered = tuple(sorted(components, key=lambda item: item.value))
        bindings = tuple(
            ComponentBinding(
                component=component,
                candidate_sha256=h(chr(ord("1") + index)),
                baseline_sha256=h(chr(ord("a") + index)),
            )
            for index, component in enumerate(ordered)
        )
        return AblationProtocol(
            protocol_id="ablation-protocol-1",
            research_question_id="rq-1",
            hypothesis_id="hyp-1",
            source_sha256=h("0"),
            candidate_id="candidate-1",
            champion_id="champion-1",
            dataset_manifest_sha256=h("c"),
            holdout_access_sha256=holdout,
            causal_cutoff="2026-09-20T00:00:00Z",
            evaluation_as_of="2026-09-21T12:00:00Z",
            scope_id=scope_id,
            component_bindings=bindings,
            claim_kind=claim_kind,
            primary_metric=(
                "brier_score"
                if claim_kind is ClaimKind.FORECAST
                else "decision_quality"
                if claim_kind is ClaimKind.DECISION
                else "net_utility"
                if claim_kind is ClaimKind.ECONOMIC
                else "execution_drag"
            ),
            metric_semantics=(
                MetricSemantics.PROPER_FORECAST_SCORE
                if claim_kind is ClaimKind.FORECAST
                else MetricSemantics.DECISION_QUALITY
                if claim_kind is ClaimKind.DECISION
                else MetricSemantics.ECONOMIC_UTILITY
                if claim_kind is ClaimKind.ECONOMIC
                else MetricSemantics.EXECUTION_DRAG
            ),
            metric_direction=direction,
            practical_threshold=Decimal("0.01"),
            guardrail_sha256s=(h("e"),),
            uncertainty_method="paired-bootstrap-v1",
            missing_outcome_policy="fail-inconclusive-v1",
            multiplicity_rule="holm-v1",
            created_at="2026-09-19T12:00:00Z",
        )

    def obs(
        self,
        enabled: tuple[PipelineComponent, ...],
        value: str | None,
        *,
        authority: str = "replay",
        evidence_char: str = "f",
        available_at: str = "2026-09-21T11:00:00Z",
        eligible: int = 10,
        selected: int = 10,
        resolved: int = 10,
        pending: int = 0,
        void: int = 0,
        missing: int = 0,
        costs_complete: bool = True,
        receipt: bool = False,
        sizing_linear: bool = False,
        zero_action_semantics: bool = False,
    ) -> AblationObservation:
        kwargs = dict(
            factual_evidence_sha256=None,
            replay_authority_sha256=None,
            simulator_sha256=None,
            assignment_evidence_sha256=None,
        )
        if value is not None:
            if authority == "factual":
                kwargs["factual_evidence_sha256"] = h("8")
            elif authority == "replay":
                kwargs["replay_authority_sha256"] = h("7")
            elif authority == "simulated":
                kwargs["simulator_sha256"] = h("6")
            elif authority == "forward":
                kwargs["assignment_evidence_sha256"] = h("5")
            elif authority != "none":
                raise AssertionError(authority)
        return AblationObservation(
            enabled_components=tuple(sorted(enabled, key=lambda item: item.value)),
            metric_value=None if value is None else Decimal(value),
            uncertainty_low=None if value is None else Decimal(value) - Decimal("0.1"),
            uncertainty_high=None if value is None else Decimal(value) + Decimal("0.1"),
            evidence_sha256=h(evidence_char),
            evidence_available_at=available_at,
            eligible_count=eligible,
            selected_count=selected,
            resolved_count=resolved,
            pending_count=pending,
            void_count=void,
            missing_count=missing,
            applicable_costs_complete=costs_complete,
            execution_receipt_sha256=h("4") if receipt else None,
            sizing_linearity_proven=sizing_linear,
            zero_action_semantics_sha256=h("3") if zero_action_semantics else None,
            assumptions=("no-market-impact",) if authority == "simulated" else (),
            **kwargs,
        )

    def test_incomplete_factorial_refuses_unique_component_credit(self):
        protocol = self.protocol(PipelineComponent.MODEL, PipelineComponent.THRESHOLD_SELECTION)
        evidence = evaluate_pipeline_ablation(
            protocol,
            (
                self.obs((), "0", evidence_char="1"),
                self.obs((PipelineComponent.MODEL,), "1", evidence_char="2"),
                self.obs((PipelineComponent.THRESHOLD_SELECTION,), "2", evidence_char="3"),
            ),
        )
        self.assertFalse(evidence.complete_factorial)
        self.assertIsNone(evidence.total_effect)
        self.assertTrue(all(item.contribution is None for item in evidence.findings))
        self.assertEqual(
            evidence.missing_coalitions,
            ((PipelineComponent.MODEL, PipelineComponent.THRESHOLD_SELECTION),),
        )

    def test_complete_factorial_preserves_interaction_and_shapley_allocation(self):
        protocol = self.protocol(PipelineComponent.MODEL, PipelineComponent.THRESHOLD_SELECTION)
        evidence = evaluate_pipeline_ablation(
            protocol,
            (
                self.obs((), "0", evidence_char="1"),
                self.obs((PipelineComponent.MODEL,), "1", evidence_char="2"),
                self.obs((PipelineComponent.THRESHOLD_SELECTION,), "1", evidence_char="3"),
                self.obs((PipelineComponent.MODEL, PipelineComponent.THRESHOLD_SELECTION), "4", evidence_char="4"),
            ),
        )
        self.assertTrue(evidence.complete_factorial)
        self.assertEqual(evidence.total_effect, Decimal("4"))
        self.assertEqual(evidence.interaction_residual, Decimal("-2"))
        findings = {item.component: item for item in evidence.findings}
        self.assertEqual(findings[PipelineComponent.MODEL].contribution, Decimal("2"))
        self.assertEqual(findings[PipelineComponent.THRESHOLD_SELECTION].contribution, Decimal("2"))
        self.assertLessEqual(findings[PipelineComponent.MODEL].contribution_low, Decimal("2"))
        self.assertGreaterEqual(findings[PipelineComponent.MODEL].contribution_high, Decimal("2"))
        self.assertLessEqual(evidence.total_effect_low, evidence.total_effect)
        self.assertGreaterEqual(evidence.total_effect_high, evidence.total_effect)
        self.assertEqual(
            findings[PipelineComponent.MODEL].identifiability_tier,
            IdentifiabilityTier.FROZEN_REPLAY_COUNTERFACTUAL,
        )
        self.assertIn("protocol-defined", findings[PipelineComponent.MODEL].reason)

    def test_future_evidence_is_rejected(self):
        protocol = self.protocol(PipelineComponent.MODEL)
        with self.assertRaisesRegex(ValueError, "future evidence"):
            evaluate_pipeline_ablation(
                protocol,
                (
                    self.obs((), "0", evidence_char="1"),
                    self.obs(
                        (PipelineComponent.MODEL,),
                        "1",
                        available_at="2026-09-21T13:00:00Z",
                        evidence_char="2",
                    ),
                ),
            )

    def test_selection_laundering_denominator_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly cover selected_count"):
            self.obs((), "0", selected=10, resolved=9)

    def test_claim_kind_and_metric_are_frozen_into_protocol_identity(self):
        forecast = self.protocol(PipelineComponent.MODEL, claim_kind=ClaimKind.FORECAST)
        economic = self.protocol(PipelineComponent.MODEL, claim_kind=ClaimKind.ECONOMIC)
        self.assertNotEqual(forecast.protocol_sha256, economic.protocol_sha256)
        self.assertNotEqual(forecast.primary_metric, economic.primary_metric)

    def test_claim_kind_rejects_incompatible_metric_semantics(self):
        protocol = self.protocol(PipelineComponent.MODEL, claim_kind=ClaimKind.FORECAST)
        with self.assertRaisesRegex(ValueError, "incompatible with claim_kind"):
            replace(protocol, metric_semantics=MetricSemantics.ECONOMIC_UTILITY)

    def test_sizing_factual_credit_requires_proven_linearity(self):
        protocol = self.protocol(PipelineComponent.SIZING, claim_kind=ClaimKind.ECONOMIC)
        with self.assertRaisesRegex(ValueError, "proven linearity"):
            evaluate_pipeline_ablation(
                protocol,
                (
                    self.obs((), "0", authority="factual", evidence_char="1"),
                    self.obs((PipelineComponent.SIZING,), "1", authority="factual", evidence_char="2"),
                ),
            )

    def test_factual_execution_requires_exact_receipt(self):
        protocol = self.protocol(PipelineComponent.EXECUTION, claim_kind=ClaimKind.EXECUTION)
        with self.assertRaisesRegex(ValueError, "execution receipt"):
            evaluate_pipeline_ablation(
                protocol,
                (
                    self.obs((), "0", authority="factual", evidence_char="1", receipt=True),
                    self.obs((PipelineComponent.EXECUTION,), "1", authority="factual", evidence_char="2"),
                ),
            )

    def test_factual_execution_with_receipt_remains_factual(self):
        protocol = self.protocol(PipelineComponent.EXECUTION, claim_kind=ClaimKind.EXECUTION)
        evidence = evaluate_pipeline_ablation(
            protocol,
            (
                self.obs((), "0", authority="factual", evidence_char="1", receipt=True),
                self.obs((PipelineComponent.EXECUTION,), "1", authority="factual", evidence_char="2", receipt=True),
            ),
        )
        self.assertTrue(evidence.complete_factorial)
        self.assertEqual(evidence.findings[0].identifiability_tier, IdentifiabilityTier.FACTUAL_MECHANICAL)

    def test_simulation_authority_mechanically_downgrades_claim(self):
        protocol = self.protocol(PipelineComponent.MODEL)
        evidence = evaluate_pipeline_ablation(
            protocol,
            (
                self.obs((), "0", authority="simulated", evidence_char="1"),
                self.obs((PipelineComponent.MODEL,), "10", authority="simulated", evidence_char="2"),
            ),
        )
        self.assertEqual(evidence.findings[0].identifiability_tier, IdentifiabilityTier.SIMULATED_COUNTERFACTUAL)
        self.assertFalse(evidence.canonical_payload()["truth"]["simulation_is_factual"])

    def test_simulation_requires_explicit_assumptions(self):
        with self.assertRaisesRegex(ValueError, "explicit assumptions"):
            AblationObservation(
                enabled_components=(), metric_value=Decimal("1"), uncertainty_low=Decimal("1"), uncertainty_high=Decimal("1"),
                evidence_sha256=h("1"), factual_evidence_sha256=None, evidence_available_at="2026-09-21T11:00:00Z",
                eligible_count=1, selected_count=1, resolved_count=1, pending_count=0, void_count=0, missing_count=0,
                simulator_sha256=h("2"), assumptions=(),
            )

    def test_economic_result_requires_complete_applicable_costs(self):
        protocol = self.protocol(PipelineComponent.MODEL, claim_kind=ClaimKind.ECONOMIC)
        with self.assertRaisesRegex(ValueError, "applicable-cost"):
            evaluate_pipeline_ablation(
                protocol,
                (
                    self.obs((), "0", costs_complete=True, evidence_char="1"),
                    self.obs((PipelineComponent.MODEL,), "1", costs_complete=False, evidence_char="2"),
                ),
            )

    def test_wait_zero_action_requires_frozen_semantics(self):
        protocol = self.protocol(PipelineComponent.MODEL)
        with self.assertRaisesRegex(ValueError, "WAIT/NO_BET semantics"):
            evaluate_pipeline_ablation(
                protocol,
                (
                    self.obs((), "0", selected=0, resolved=0, evidence_char="1"),
                    self.obs((PipelineComponent.MODEL,), "1", evidence_char="2"),
                ),
            )
        evidence = evaluate_pipeline_ablation(
            protocol,
            (
                self.obs((), "0", selected=0, resolved=0, zero_action_semantics=True, evidence_char="1"),
                self.obs((PipelineComponent.MODEL,), "1", evidence_char="2"),
            ),
        )
        self.assertTrue(evidence.complete_factorial)

    def test_holdout_alias_cannot_reset_protocol_identity(self):
        first = self.protocol(PipelineComponent.MODEL, holdout=h("d"))
        same = self.protocol(PipelineComponent.MODEL, holdout=h("d"))
        alias = self.protocol(PipelineComponent.MODEL, holdout=h("9"))
        self.assertEqual(first.protocol_sha256, same.protocol_sha256)
        self.assertNotEqual(first.protocol_sha256, alias.protocol_sha256)

    def test_pending_outcomes_are_inconclusive_not_shrunk_away(self):
        protocol = self.protocol(PipelineComponent.MODEL)
        pending = self.obs(
            (PipelineComponent.MODEL,), None,
            authority="none", selected=10, resolved=9, pending=1, evidence_char="2",
        )
        evidence = evaluate_pipeline_ablation(
            protocol,
            (self.obs((), "0", evidence_char="1"), pending),
        )
        self.assertFalse(evidence.complete_factorial)
        self.assertIsNone(evidence.findings[0].contribution)

    def test_scope_change_requires_new_protocol_identity(self):
        table_tennis = self.protocol(PipelineComponent.MODEL, scope_id="table-tennis:provider-a:match-odds")
        football = self.protocol(PipelineComponent.MODEL, scope_id="football:provider-a:match-odds")
        self.assertNotEqual(table_tennis.protocol_sha256, football.protocol_sha256)

    def test_restart_replay_is_deterministic_and_deduplicable_by_hash(self):
        protocol = self.protocol(PipelineComponent.MODEL)
        observations = (
            self.obs((), "0", evidence_char="1"),
            self.obs((PipelineComponent.MODEL,), "1", evidence_char="2"),
        )
        first = evaluate_pipeline_ablation(protocol, observations)
        reopened = evaluate_pipeline_ablation(protocol, observations)
        self.assertEqual(first.evidence_sha256, reopened.evidence_sha256)
        self.assertEqual(first.canonical_payload(), reopened.canonical_payload())

    def test_post_hoc_component_change_creates_new_protocol(self):
        protocol = self.protocol(PipelineComponent.MODEL)
        changed_binding = replace(
            protocol.component_bindings[0],
            candidate_sha256=h("9"),
        )
        changed = replace(protocol, component_bindings=(changed_binding,))
        self.assertNotEqual(protocol.protocol_sha256, changed.protocol_sha256)

    def test_negative_component_result_is_retained_not_rebranded(self):
        protocol = self.protocol(PipelineComponent.MODEL)
        evidence = evaluate_pipeline_ablation(
            protocol,
            (
                self.obs((), "0", evidence_char="1"),
                self.obs((PipelineComponent.MODEL,), "-2", evidence_char="2"),
            ),
        )
        self.assertEqual(evidence.findings[0].contribution, Decimal("-2"))
        self.assertEqual(evidence.total_effect, Decimal("-2"))
        self.assertIn("-2", str(evidence.canonical_payload()))

    def test_caller_cannot_mint_identifiability_without_primary_authority(self):
        with self.assertRaisesRegex(ValueError, "exactly one identifiability authority"):
            self.obs((), "1", authority="none")

    def test_data_model_threshold_cannot_be_laundered_as_factual_mechanical(self):
        protocol = self.protocol(PipelineComponent.MODEL)
        with self.assertRaisesRegex(ValueError, "require replay, simulation, or prospective assignment"):
            evaluate_pipeline_ablation(
                protocol,
                (
                    self.obs((), "0", authority="factual", evidence_char="1"),
                    self.obs((PipelineComponent.MODEL,), "1", authority="factual", evidence_char="2"),
                ),
            )

    def test_mixed_forward_and_replay_does_not_upgrade_to_factual(self):
        protocol = self.protocol(PipelineComponent.MODEL)
        evidence = evaluate_pipeline_ablation(
            protocol,
            (
                self.obs((), "0", authority="forward", evidence_char="1"),
                self.obs((PipelineComponent.MODEL,), "1", authority="replay", evidence_char="2"),
            ),
        )
        self.assertEqual(
            evidence.findings[0].identifiability_tier,
            IdentifiabilityTier.FROZEN_REPLAY_COUNTERFACTUAL,
        )


    def test_raw_observation_cannot_self_mint_positive_identifiability(self):
        observation = self.obs((), "1", authority="replay", evidence_char="1")
        self.assertEqual(observation.identifiability_tier, IdentifiabilityTier.NOT_IDENTIFIABLE)
        self.assertEqual(
            observation.authority_kind,
            IdentifiabilityTier.FROZEN_REPLAY_COUNTERFACTUAL,
        )

    def test_unresolved_authority_digests_cannot_mint_scientific_credit(self):
        cases = (
            (
                self.protocol(PipelineComponent.MODEL),
                (
                    self.obs((), "0", authority="replay", evidence_char="1"),
                    self.obs((PipelineComponent.MODEL,), "1", authority="replay", evidence_char="2"),
                ),
            ),
            (
                self.protocol(PipelineComponent.EXECUTION, claim_kind=ClaimKind.EXECUTION),
                (
                    self.obs((), "0", authority="factual", evidence_char="1", receipt=True),
                    self.obs((PipelineComponent.EXECUTION,), "1", authority="factual", evidence_char="2", receipt=True),
                ),
            ),
            (
                self.protocol(PipelineComponent.MODEL),
                (
                    self.obs((), "0", authority="forward", evidence_char="1"),
                    self.obs((PipelineComponent.MODEL,), "1", authority="forward", evidence_char="2"),
                ),
            ),
            (
                self.protocol(PipelineComponent.MODEL),
                (
                    self.obs((), "0", authority="simulated", evidence_char="1"),
                    self.obs((PipelineComponent.MODEL,), "1", authority="simulated", evidence_char="2"),
                ),
            ),
        )
        for protocol, observations in cases:
            with self.subTest(kind=observations[0].authority_kind):
                with self.assertRaisesRegex(ValueError, "re-resolution"):
                    _evaluate_pipeline_ablation(protocol, observations)

    def test_resolver_must_bind_exact_frozen_scientific_context(self):
        protocol = self.protocol(PipelineComponent.MODEL)
        observations = (
            self.obs((), "0", authority="replay", evidence_char="1"),
            self.obs((PipelineComponent.MODEL,), "1", authority="replay", evidence_char="2"),
        )
        baseline = _resolver_for(protocol, observations)
        first = observations[0]
        key = (first.replay_authority_sha256, first.evidence_sha256)
        resolved = baseline.values[key]
        corruptions = (
            replace(
                resolved,
                identifiability_tier=IdentifiabilityTier.SIMULATED_COUNTERFACTUAL,
                assumptions=("different-assumption",),
            ),
            replace(resolved, evidence_sha256=h("9")),
            replace(resolved, scope_id="other-scope"),
            replace(resolved, dataset_manifest_sha256=h("9")),
            replace(resolved, holdout_access_sha256=h("9")),
            replace(resolved, causal_cutoff="2026-09-19T00:00:00Z"),
            replace(resolved, available_at="2026-09-21T11:30:00Z"),
        )
        for corrupted in corruptions:
            values = dict(baseline.values)
            values[key] = corrupted
            with self.subTest(corrupted=corrupted):
                with self.assertRaises(ValueError):
                    _evaluate_pipeline_ablation(
                        protocol,
                        observations,
                        authority_resolver=_TestAuthorityResolver(values),
                    )

    def test_resolver_binds_execution_receipt_and_simulator_assumptions(self):
        execution_protocol = self.protocol(
            PipelineComponent.EXECUTION,
            claim_kind=ClaimKind.EXECUTION,
        )
        execution_observations = (
            self.obs((), "0", authority="factual", evidence_char="1", receipt=True),
            self.obs((PipelineComponent.EXECUTION,), "1", authority="factual", evidence_char="2", receipt=True),
        )
        execution_resolver = _resolver_for(execution_protocol, execution_observations)
        first = execution_observations[0]
        key = (first.factual_evidence_sha256, first.evidence_sha256)
        values = dict(execution_resolver.values)
        values[key] = replace(values[key], execution_receipt_sha256=h("9"))
        with self.assertRaisesRegex(ValueError, "execution receipt"):
            _evaluate_pipeline_ablation(
                execution_protocol,
                execution_observations,
                authority_resolver=_TestAuthorityResolver(values),
            )

        simulation_protocol = self.protocol(PipelineComponent.MODEL)
        simulation_observations = (
            self.obs((), "0", authority="simulated", evidence_char="1"),
            self.obs((PipelineComponent.MODEL,), "1", authority="simulated", evidence_char="2"),
        )
        simulation_resolver = _resolver_for(simulation_protocol, simulation_observations)
        first = simulation_observations[0]
        key = (first.simulator_sha256, first.evidence_sha256)
        values = dict(simulation_resolver.values)
        values[key] = replace(values[key], assumptions=("different-assumption",))
        with self.assertRaisesRegex(ValueError, "simulator assumptions"):
            _evaluate_pipeline_ablation(
                simulation_protocol,
                simulation_observations,
                authority_resolver=_TestAuthorityResolver(values),
            )


if __name__ == "__main__":
    unittest.main()
