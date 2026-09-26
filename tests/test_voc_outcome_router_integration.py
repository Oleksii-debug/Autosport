from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from decimal import Decimal

from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.model_compute_router import (
    ComputeCandidate,
    ComputeRouteRequest,
    ComputeRoutingPolicy,
    ComputeTier,
    DataClassification,
    VOCEvidenceProvenance,
    ValueOfComputationEvidence,
    route_compute,
)
from autosport.scientific_registry import ScientificRegistry
from autosport.sport_domain_fitness import (
    DomainProfile,
    EvidenceProvenance,
    EvidenceState,
    MetricEvidence,
    SportDomainFitnessObservation,
)
from autosport.voc_evaluation import VOCEvaluationStore
from autosport.voc_outcome_scoring import (
    CanonicalVOCOutcomeSource,
    build_canonical_voc_authority_resolver,
)
from test_voc_outcome_scoring import (
    CanonicalOutcomeDerivedVOCScoreAuthorityTests,
    digest,
    SHA_A,
    SHA_B,
    SHA_C,
    SHA_D,
    T_AS_OF,
    T_EVALUATED,
)

T_ROUTED = "2026-09-20T00:01:03Z"
T_ROUTE_DEADLINE = "2026-09-20T00:02:00Z"


def metric(value: str, unit: str) -> MetricEvidence:
    return MetricEvidence(EvidenceState.MEASURED, Decimal(value), unit)


def slow_observation() -> SportDomainFitnessObservation:
    return SportDomainFitnessObservation(
        observation_id="fitness-voc-derived-next",
        sport_id="table_tennis",
        league_id="league-voc",
        market_id="match",
        provider_id="provider-voc-derived",
        measured_from=T_EVALUATED,
        measured_until=T_AS_OF,
        available_at=T_AS_OF,
        evidence_sha256=SHA_D,
        provenance=EvidenceProvenance.OBSERVED,
        domain_profile=DomainProfile.SLOW,
        catalogue_coverage=metric("0.9", "fraction"),
        quote_coverage=metric("0.8", "fraction"),
        recurrence_per_hour=metric("12", "events/hour"),
        freshness_seconds=metric("1", "seconds"),
        reaction_slack_seconds=metric("10", "seconds"),
        executable_liquidity=metric("100", "units"),
        fee_fraction=metric("0.01", "fraction"),
        slippage_fraction=metric("0.01", "fraction"),
        capital_time_hours=metric("0.25", "hours"),
        data_cost=metric("0.10", "cost"),
        compute_cost=metric("0.20", "cost"),
        compute_duration_seconds=metric("4", "seconds"),
        slow_analysis_deadline_seconds=metric("5", "seconds"),
        freshness_ttl_seconds=metric("30", "seconds"),
    )


class CanonicalVOCOutcomeRouterIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CanonicalOutcomeDerivedVOCScoreAuthorityTests(
            methodName="test_production_authority_derives_score_and_restart_revalidates_it"
        )
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def test_restarted_production_score_cannot_bypass_cloud_permission(self):
        paired = self.fixture._evaluation(register_cohort=False)
        second = self.fixture._evaluation(
            evaluation_id="voc-derived-2",
            decision_input_sha256=digest({"episode": 2, "kind": "input"}),
            baseline_output_sha256=digest({"episode": 2, "kind": "baseline"}),
            challenger_output_sha256=digest({"episode": 2, "kind": "challenger"}),
            register_cohort=False,
            outcome_authority=self.fixture.second_outcome_authority,
        )
        self.fixture.registry.append(paired)
        self.fixture.registry.append(second)
        self.fixture._append_cohort(paired, second)
        score = self.fixture._authority().resolve(paired.evaluation_id, as_of=T_AS_OF)
        self.assertIsNotNone(score)
        assert score is not None
        self.assertEqual(score.effective_sample_size, 2)
        self.fixture._append_scientific_qualification(
            paired,
            score_sha256=score.score_sha256,
        )

        store_path = self.fixture.root / "voc-router-store.json"
        first_resolver = build_canonical_voc_authority_resolver(
            decision_ledger=self.fixture.ledger,
            scientific_registry=self.fixture.registry,
            outcome_authority=self.fixture.outcome_authority,
            outcome_source_root=self.fixture.root,
            source_record_file=self.fixture.outcome_file,
            source_record_sha256=self.fixture.outcome_sha256,
            additional_outcome_sources=(
                CanonicalVOCOutcomeSource(
                    authority=self.fixture.second_outcome_authority,
                    source_root=self.fixture.root,
                    source_record_file=self.fixture.second_outcome_file,
                    source_record_sha256=self.fixture.second_outcome_sha256,
                ),
            ),
        )
        first_store = VOCEvaluationStore(
            store_path,
            canonical_authority_resolver=first_resolver,
        )
        first_store.record(paired)

        restarted_ledger = JsonlDecisionLedger(self.fixture.ledger.path)
        restarted_registry = ScientificRegistry(self.fixture.registry.path)
        restarted_resolver = build_canonical_voc_authority_resolver(
            decision_ledger=restarted_ledger,
            scientific_registry=restarted_registry,
            outcome_authority=self.fixture.outcome_authority,
            outcome_source_root=self.fixture.root,
            source_record_file=self.fixture.outcome_file,
            source_record_sha256=self.fixture.outcome_sha256,
            additional_outcome_sources=(
                CanonicalVOCOutcomeSource(
                    authority=self.fixture.second_outcome_authority,
                    source_root=self.fixture.root,
                    source_record_file=self.fixture.second_outcome_file,
                    source_record_sha256=self.fixture.second_outcome_sha256,
                ),
            ),
        )
        restarted_store = VOCEvaluationStore(
            store_path,
            canonical_authority_resolver=restarted_resolver,
        )

        request = ComputeRouteRequest(
            request_id="req-voc-derived-next",
            created_at=T_AS_OF,
            decision_deadline=T_ROUTE_DEADLINE,
            required_capability="route-voc",
            data_classification=DataClassification.PUBLIC,
            allow_cloud=True,
            max_cost=Decimal("10"),
            response_ttl_seconds=Decimal("30"),
            baseline_candidate_id="baseline",
            cloud_candidate_id="challenger",
            decision_input_sha256=SHA_B,
            decision_evidence_sha256=SHA_C,
            voc_regime_id="regime-voc",
            voc_urgency_id="normal",
            voc_contradiction_state="none",
        )
        observation = slow_observation()

        candidates = (
            ComputeCandidate(
                candidate_id="baseline",
                tier=ComputeTier.LOCAL,
                backend_id="local",
                model_id="baseline-model",
                config_sha256=SHA_B,
                capabilities=("route-voc",),
                estimated_cost=Decimal("0.10"),
                estimated_latency_seconds=Decimal("2"),
            ),
            ComputeCandidate(
                candidate_id="challenger",
                tier=ComputeTier.CLOUD,
                backend_id="cloud",
                model_id="challenger-model",
                config_sha256=SHA_C,
                capabilities=("route-voc",),
                estimated_cost=Decimal("0.20"),
                estimated_latency_seconds=Decimal("4"),
            ),
        )
        policy = ComputeRoutingPolicy(
            policy_id="policy-voc-derived",
            policy_version=1,
            cloud_enabled=True,
            max_cloud_cost=Decimal("10"),
            voc_max_age_seconds=Decimal("30"),
            voc_min_effective_sample_size=2,
        )
        current_context = {
            "request_id": request.request_id,
            "decision_input_sha256": request.decision_input_sha256,
            "task_class": request.required_capability,
            "data_classification": request.data_classification.value,
            "sport_id": observation.sport_id,
            "league_id": observation.league_id,
            "regime_id": request.voc_regime_id,
            "urgency_id": request.voc_urgency_id,
            "contradiction_state": request.voc_contradiction_state,
            "routing_policy_id": policy.policy_id,
            "routing_policy_version": str(policy.policy_version),
            "routing_policy_sha256": hashlib.sha256(
                json.dumps(
                    policy.payload(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
            "cloud_permission": "ALLOW",
            "cloud_backend_id": "cloud",
        }
        current_context_sha256 = restarted_ledger.append(
            DecisionRecord(
                replay_run_id="replay-voc-derived-next",
                agent="voc-router-integration-test",
                observed_ts=T_AS_OF,
                action="VOC_ROUTE_CONTEXT",
                payload={"voc_current_context": current_context},
                context_hash=SHA_A,
                decision_id="decision-voc-derived-next-context",
                recorded_at=T_AS_OF,
            )
        )
        request = replace(
            request,
            decision_evidence_sha256=current_context_sha256,
        )
        evidence = ValueOfComputationEvidence(
            evidence_id=paired.evaluation_id,
            baseline_candidate_id=paired.baseline_candidate_id,
            challenger_candidate_id=paired.challenger_candidate_id,
            baseline_backend_id=paired.baseline_backend_id,
            baseline_model_id=paired.baseline_model_id,
            baseline_config_sha256=paired.baseline_config_sha256,
            challenger_backend_id=paired.challenger_backend_id,
            challenger_model_id=paired.challenger_model_id,
            challenger_config_sha256=paired.challenger_config_sha256,
            measured_at=paired.evaluated_at,
            available_at=paired.evaluated_at,
            provenance=VOCEvidenceProvenance.MEASURED_SHADOW,
            baseline_utility=paired.baseline_utility,
            challenger_utility=paired.challenger_utility,
            compute_cost_penalty=paired.compute_cost_penalty,
            latency_opportunity_cost_penalty=(
                paired.latency_opportunity_cost_penalty
            ),
            measured_compute_cost=paired.measured_compute_cost,
            evaluation_sha256=paired.evaluation_sha256,
            evaluation=paired,
        )

        decision = route_compute(
            request,
            candidates,
            policy,
            as_of=T_ROUTED,
            voc_evidence=evidence,
            voc_evaluation_store=restarted_store,
            domain_observation=observation,
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertEqual(decision.candidate_id, "baseline")
        self.assertIn(
            "product-issued cloud permission authority is unavailable",
            decision.reason,
        )


if __name__ == "__main__":
    unittest.main()
