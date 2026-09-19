import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from autosport.model_compute_router import (
    ComputeCandidate,
    ComputeRouteRequest,
    ComputeRoutingPolicy,
    ComputeTier,
    DataClassification,
    ModelComputeRouterError,
    VOCEvidenceProvenance,
    ValueOfComputationEvidence,
    route_compute,
)
from autosport.sport_domain_fitness import (
    DomainProfile,
    EvidenceProvenance,
    EvidenceState,
    MetricEvidence,
    SportDomainFitnessObservation,
)
from autosport.voc_evaluation import (
    PairedVOCEvaluation,
    VOCEvaluationError,
    VOCEvaluationProvenance,
    VOCEvaluationStore,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-01T00:00:10Z"
T2 = "2026-01-01T00:00:20Z"
T3 = "2026-01-01T00:00:30Z"
T4 = "2026-01-01T00:00:40Z"


def evaluation(**overrides):
    values = dict(
        evaluation_id="voc-eval-1",
        task_class="forecast",
        sport_id="table-tennis",
        league_id="league-1",
        regime_id="regime-1",
        baseline_candidate_id="local",
        baseline_backend_id="local-cpu",
        baseline_model_id="baseline-v1",
        baseline_config_sha256=SHA_A,
        challenger_candidate_id="cloud",
        challenger_backend_id="cloud-backend",
        challenger_model_id="challenger-v2",
        challenger_config_sha256=SHA_B,
        decision_evidence_sha256=SHA_C,
        baseline_output_sha256=SHA_A,
        challenger_output_sha256=SHA_B,
        baseline_action="WAIT",
        challenger_action="ACT",
        baseline_abstained=True,
        challenger_abstained=False,
        decision_at=T0,
        decision_deadline=T3,
        baseline_completed_at=T1,
        challenger_completed_at=T1,
        outcome_evidence_sha256=SHA_D,
        outcome_revealed_at=T2,
        evaluated_at=T2,
        scoring_rule_id="net-utility-v1",
        scoring_rule_sha256=SHA_A,
        research_protocol_id="voc-protocol-v1",
        research_protocol_sha256=SHA_B,
        holdout_access_id="holdout-1",
        multiple_comparison_control_sha256=SHA_C,
        baseline_utility=Decimal("1"),
        challenger_utility=Decimal("2"),
        compute_cost_penalty=Decimal("0.2"),
        latency_opportunity_cost_penalty=Decimal("0.1"),
        measured_compute_cost=Decimal("5"),
        paired_sample_count=20,
        effective_sample_size=12,
        support_fraction=Decimal("0.8"),
        incremental_value_interval_low=Decimal("0.2"),
        incremental_value_interval_high=Decimal("1.1"),
        provenance=VOCEvaluationProvenance.MEASURED_SHADOW,
    )
    values.update(overrides)
    return PairedVOCEvaluation(**values)


def voc(value=None):
    paired = evaluation() if value is None else value
    return ValueOfComputationEvidence(
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
        latency_opportunity_cost_penalty=paired.latency_opportunity_cost_penalty,
        measured_compute_cost=paired.measured_compute_cost,
        evaluation_sha256=paired.evaluation_sha256,
        evaluation=paired,
    )


def candidates():
    return (
        ComputeCandidate(
            candidate_id="local",
            tier=ComputeTier.LOCAL,
            backend_id="local-cpu",
            model_id="baseline-v1",
            config_sha256=SHA_A,
            capabilities=("forecast",),
            estimated_cost=Decimal("1"),
            estimated_latency_seconds=Decimal("2"),
        ),
        ComputeCandidate(
            candidate_id="cloud",
            tier=ComputeTier.CLOUD,
            backend_id="cloud-backend",
            model_id="challenger-v2",
            config_sha256=SHA_B,
            capabilities=("forecast",),
            estimated_cost=Decimal("5"),
            estimated_latency_seconds=Decimal("4"),
        ),
    )


def request():
    return ComputeRouteRequest(
        request_id="req-voc",
        created_at=T0,
        decision_deadline=T4,
        required_capability="forecast",
        data_classification=DataClassification.PUBLIC,
        allow_cloud=True,
        max_cost=Decimal("10"),
        response_ttl_seconds=Decimal("30"),
        baseline_candidate_id="local",
        cloud_candidate_id="cloud",
        decision_evidence_sha256=SHA_C,
    )


def policy(**overrides):
    values = dict(
        policy_id="policy-voc",
        policy_version=1,
        cloud_enabled=True,
        max_cloud_cost=Decimal("10"),
        voc_max_age_seconds=Decimal("30"),
        voc_min_effective_sample_size=10,
    )
    values.update(overrides)
    return ComputeRoutingPolicy(**values)


def metric(value, unit):
    return MetricEvidence(
        EvidenceState.MEASURED,
        Decimal(str(value)),
        unit,
    )


def slow_observation():
    return SportDomainFitnessObservation(
        observation_id="fitness-voc",
        sport_id="table-tennis",
        league_id="league-1",
        market_id="match",
        provider_id="provider-1",
        measured_from=T0,
        measured_until=T0,
        available_at=T0,
        evidence_sha256=SHA_C,
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
        compute_cost=metric("5", "cost"),
        compute_duration_seconds=metric("4", "seconds"),
        slow_analysis_deadline_seconds=metric("5", "seconds"),
        freshness_ttl_seconds=metric("30", "seconds"),
    )


class _FixtureCanonicalVOCResolver:
    """Test-only stand-in for a resolver backed by canonical durable authorities."""

    def __init__(self):
        self._records = {}

    def publish(self, value):
        self._records[value.evaluation_id] = value

    def resolve(self, evaluation, *, as_of):
        value = self._records.get(evaluation.evaluation_id)
        if value is None:
            return None
        return value


class PairedVOCEvaluationTests(unittest.TestCase):
    def setUp(self):
        self._router_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._router_tmp.cleanup)
        self._canonical_voc = _FixtureCanonicalVOCResolver()
        self.voc_store = VOCEvaluationStore(
            Path(self._router_tmp.name) / "router-voc.json",
            canonical_authority_resolver=self._canonical_voc,
        )

    def qualified_voc(self, value=None):
        evidence = voc(value)
        if evidence.evaluation is not None:
            self._canonical_voc.publish(evidence.evaluation)
            self.voc_store.record(evidence.evaluation)
        return evidence

    def route_compute(self, *args, **kwargs):
        kwargs.setdefault("voc_evaluation_store", self.voc_store)
        return route_compute(*args, **kwargs)

    def test_qualified_paired_evaluation_can_authorize_cloud(self):
        decision = self.route_compute(
            request(),
            candidates(),
            policy(),
            as_of=T2,
            voc_evidence=self.qualified_voc(),
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.CLOUD)

    def test_persisted_shadow_record_without_canonical_authority_cannot_mint_cloud(self):
        raw_store = VOCEvaluationStore(Path(self._router_tmp.name) / "raw-only.json")
        fake = evaluation(evaluation_id="voc-raw-only")
        evidence = voc(fake)
        raw_store.record(fake)
        decision = self.route_compute(
            request(),
            candidates(),
            policy(),
            as_of=T2,
            voc_evidence=evidence,
            domain_observation=slow_observation(),
            voc_evaluation_store=raw_store,
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertIn("missing canonical VOC authority resolver", decision.reason)

    def test_opaque_evaluation_sha_cannot_mint_cloud_authority(self):
        paired = evaluation()
        evidence = replace(self.qualified_voc(paired), evaluation=None)
        decision = self.route_compute(
            request(),
            candidates(),
            policy(),
            as_of=T2,
            voc_evidence=evidence,
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertIn("missing qualified paired outcome evaluation", decision.reason)

    def test_no_action_change_cannot_claim_positive_utility(self):
        with self.assertRaisesRegex(
            VOCEvaluationError,
            "unchanged decision cannot claim positive utility",
        ):
            evaluation(
                baseline_action="ACT",
                challenger_action="ACT",
                baseline_abstained=False,
                challenger_abstained=False,
            )

    def test_insufficient_ess_fails_closed(self):
        paired = evaluation(effective_sample_size=4)
        decision = self.route_compute(
            request(),
            candidates(),
            policy(voc_min_effective_sample_size=10),
            as_of=T2,
            voc_evidence=self.qualified_voc(paired),
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertIn("effective sample size", decision.reason)

    def test_uncertainty_interval_must_establish_positive_value(self):
        paired = evaluation(
            incremental_value_interval_low=Decimal("-0.1"),
        )
        decision = self.route_compute(
            request(),
            candidates(),
            policy(),
            as_of=T2,
            voc_evidence=self.qualified_voc(paired),
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertIn("uncertainty interval", decision.reason)

    def test_deadline_miss_fails_closed_even_with_positive_realized_utility(self):
        paired = evaluation(
            decision_deadline=T1,
            challenger_completed_at=T2,
            outcome_revealed_at=T2,
            evaluated_at=T2,
        )
        decision = self.route_compute(
            request(),
            candidates(),
            policy(),
            as_of=T2,
            voc_evidence=self.qualified_voc(paired),
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertIn("deadline", decision.reason)

    def test_router_evidence_must_match_paired_utility_and_cost(self):
        paired = evaluation()
        with self.assertRaisesRegex(
            ModelComputeRouterError,
            "utility/cost values do not match",
        ):
            replace(
                self.qualified_voc(paired),
                challenger_utility=Decimal("3"),
            )

    def test_store_round_trip_preserves_negative_and_positive_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voc.json"
            store = VOCEvaluationStore(path)
            positive = evaluation(evaluation_id="positive")
            negative = evaluation(
                evaluation_id="negative",
                challenger_utility=Decimal("0.5"),
                compute_cost_penalty=Decimal("0.2"),
                latency_opportunity_cost_penalty=Decimal("0.1"),
                incremental_value_interval_low=Decimal("-1"),
                incremental_value_interval_high=Decimal("-0.2"),
            )
            store.record(positive)
            store.record(negative)

            reopened = VOCEvaluationStore(path)
            self.assertEqual(
                {value.evaluation_id for value in reopened.values()},
                {"positive", "negative"},
            )
            self.assertLess(reopened.get("negative").net_value, Decimal("0"))
            self.assertEqual(
                reopened.require(
                    "positive",
                    evaluation_sha256=positive.evaluation_sha256,
                    as_of=T2,
                ),
                positive,
            )

    def test_store_is_idempotent_and_rejects_conflicting_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voc.json"
            store = VOCEvaluationStore(path)
            original = evaluation()
            first = store.record(original)
            second = store.record(original)
            self.assertEqual(first, second)

            changed = evaluation(
                challenger_action="ACT-OTHER",
                evaluation_id=original.evaluation_id,
            )
            with self.assertRaisesRegex(
                VOCEvaluationError,
                "conflicting immutable",
            ):
                store.record(changed)

    def test_store_state_tamper_is_detected_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voc.json"
            store = VOCEvaluationStore(path)
            store.record(evaluation())
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["evaluations"][0]["challenger_action"] = "tampered"
            path.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaisesRegex(
                VOCEvaluationError,
                "state SHA-256 mismatch",
            ):
                VOCEvaluationStore(path)

    def test_future_outcome_evaluation_is_not_causally_usable(self):
        paired = evaluation(
            outcome_revealed_at=T3,
            evaluated_at=T3,
        )
        evidence = self.qualified_voc(paired)
        decision = self.route_compute(
            request(),
            candidates(),
            policy(),
            as_of=T2,
            voc_evidence=evidence,
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertIn("not causally available", decision.reason)

    def test_simulated_evaluation_never_authorizes_cloud(self):
        paired = evaluation(
            provenance=VOCEvaluationProvenance.SIMULATED,
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
            provenance=VOCEvidenceProvenance.SIMULATED,
            baseline_utility=paired.baseline_utility,
            challenger_utility=paired.challenger_utility,
            compute_cost_penalty=paired.compute_cost_penalty,
            latency_opportunity_cost_penalty=paired.latency_opportunity_cost_penalty,
            measured_compute_cost=paired.measured_compute_cost,
            evaluation_sha256=paired.evaluation_sha256,
            evaluation=paired,
        )
        decision = self.route_compute(
            request(),
            candidates(),
            policy(),
            as_of=T2,
            voc_evidence=evidence,
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertIn("simulated", decision.reason)


if __name__ == "__main__":
    unittest.main()
