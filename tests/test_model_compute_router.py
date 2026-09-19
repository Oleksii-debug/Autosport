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
    ExecutionDisposition,
    ModelComputeRouterError,
    ModelComputeRouterStore,
    VOCEvidenceProvenance,
    ValueOfComputationEvidence,
    route_compute,
)
from autosport.sport_domain_fitness import (
    DomainProfile,
    EvidenceProvenance,
    EvidenceState,
    MetricEvidence,
    RouteRecommendation,
    RouteStatus,
    SportDomainFitnessObservation,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-01T00:00:10Z"
T2 = "2026-01-01T00:00:20Z"
T3 = "2026-01-01T00:00:30Z"
T4 = "2026-01-01T00:00:40Z"


def candidate(
    candidate_id="local",
    *,
    tier=ComputeTier.LOCAL,
    backend_id="local-cpu",
    model_id="baseline-v1",
    config_sha256=SHA_A,
    capabilities=("forecast",),
    cost="1",
    latency="2",
):
    return ComputeCandidate(
        candidate_id=candidate_id,
        tier=tier,
        backend_id=backend_id,
        model_id=model_id,
        config_sha256=config_sha256,
        capabilities=capabilities,
        estimated_cost=Decimal(cost),
        estimated_latency_seconds=Decimal(latency),
    )


def request(**overrides):
    values = dict(
        request_id="req-1",
        created_at=T0,
        decision_deadline=T3,
        required_capability="forecast",
        data_classification=DataClassification.PUBLIC,
        allow_cloud=True,
        max_cost=Decimal("20"),
        response_ttl_seconds=Decimal("10"),
        baseline_candidate_id="local",
        cloud_candidate_id="cloud",
    )
    values.update(overrides)
    return ComputeRouteRequest(**values)


def policy(**overrides):
    values = dict(
        policy_id="policy-1",
        policy_version=1,
        cloud_enabled=True,
        max_cloud_cost=Decimal("10"),
        voc_max_age_seconds=Decimal("30"),
    )
    values.update(overrides)
    return ComputeRoutingPolicy(**values)


def voc(**overrides):
    values = dict(
        evidence_id="voc-1",
        baseline_candidate_id="local",
        challenger_candidate_id="cloud",
        baseline_backend_id="local-cpu",
        baseline_model_id="baseline-v1",
        baseline_config_sha256=SHA_A,
        challenger_backend_id="permitted-cloud",
        challenger_model_id="challenger-v2",
        challenger_config_sha256=SHA_B,
        measured_at=T0,
        available_at=T0,
        provenance=VOCEvidenceProvenance.MEASURED_SHADOW,
        baseline_utility=Decimal("1"),
        challenger_utility=Decimal("2"),
        compute_cost_penalty=Decimal("0.2"),
        measured_compute_cost=Decimal("5"),
        evaluation_sha256=SHA_C,
    )
    values.update(overrides)
    return ValueOfComputationEvidence(**values)


def slow_route(status=RouteStatus.ROUTE_SLOW_RESEARCH):
    return RouteRecommendation(
        status,
        "caller-constructed domain route",
        "fitness-1",
        DomainProfile.SLOW,
    )


def metric(value, unit):
    return MetricEvidence(
        EvidenceState.MEASURED,
        Decimal(str(value)),
        unit,
    )


def slow_observation(**overrides):
    values = dict(
        observation_id="fitness-1",
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
    values.update(overrides)
    return SportDomainFitnessObservation(**values)


class ModelComputeRouterTests(unittest.TestCase):
    def setUp(self):
        self.local = candidate()
        self.cloud = candidate(
            "cloud",
            tier=ComputeTier.CLOUD,
            backend_id="permitted-cloud",
            model_id="challenger-v2",
            config_sha256=SHA_B,
            cost="5",
            latency="4",
        )
        self.candidates = (self.local, self.cloud)

    def test_cloud_is_disabled_by_default_and_falls_back_local(self):
        disabled = ComputeRoutingPolicy(
            policy_id="default",
            policy_version=1,
        )
        decision = route_compute(
            request(),
            self.candidates,
            disabled,
            as_of=T1,
            voc_evidence=voc(),
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertEqual(decision.candidate_id, "local")
        self.assertIn("disabled", decision.reason)

    def test_private_data_never_escalates_to_cloud(self):
        decision = route_compute(
            request(data_classification=DataClassification.PRIVATE),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=voc(),
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertIn("non-public", decision.reason)

    def test_cloud_requires_positive_fresh_measured_paired_voc(self):
        decision = route_compute(
            request(),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=voc(),
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.CLOUD)
        self.assertEqual(decision.backend_id, "permitted-cloud")
        self.assertEqual(decision.model_id, "challenger-v2")
        self.assertEqual(decision.config_sha256, SHA_B)
        self.assertEqual(decision.domain_observation_id, "fitness-1")

        no_voc = route_compute(
            replace(request(), request_id="req-no-voc"),
            self.candidates,
            policy(),
            as_of=T1,
            domain_observation=slow_observation(),
        )
        self.assertEqual(no_voc.tier, ComputeTier.LOCAL)

        nonpositive = route_compute(
            replace(request(), request_id="req-nonpositive"),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=voc(
                evidence_id="voc-zero",
                challenger_utility=Decimal("1.5"),
                compute_cost_penalty=Decimal("0.5"),
            ),
            domain_observation=slow_observation(),
        )
        self.assertEqual(nonpositive.tier, ComputeTier.LOCAL)
        self.assertIn("non-positive", nonpositive.reason)

        simulated = route_compute(
            replace(request(), request_id="req-simulated"),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=voc(
                evidence_id="voc-simulated",
                provenance=VOCEvidenceProvenance.SIMULATED,
            ),
            domain_observation=slow_observation(),
        )
        self.assertEqual(simulated.tier, ComputeTier.LOCAL)

    def test_voc_exact_compute_identity_cannot_be_reused_under_same_candidate_ids(self):
        mismatched = voc(
            evidence_id="voc-old-compute-identity",
            challenger_backend_id="retired-cloud",
            challenger_model_id="challenger-v1",
            challenger_config_sha256=SHA_C,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router-mismatched-voc.json"
            req = request(request_id="req-voc-old-compute-identity")
            store = ModelComputeRouterStore(path)
            rejected = store.route(
                req,
                self.candidates,
                policy(),
                as_of=T1,
                voc_evidence=mismatched,
                domain_observation=slow_observation(),
            )
            self.assertEqual(rejected.tier, ComputeTier.LOCAL)
            self.assertEqual(rejected.candidate_id, "local")
            self.assertIn("exact compute identity", rejected.reason)

            reopened = ModelComputeRouterStore(path)
            readback = reopened.route(
                req,
                self.candidates,
                policy(),
                as_of=T1,
                voc_evidence=mismatched,
                domain_observation=slow_observation(),
            )
            self.assertEqual(readback, rejected)

        exact = route_compute(
            request(request_id="req-voc-exact-compute-identity"),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=voc(evidence_id="voc-exact-compute-identity"),
            domain_observation=slow_observation(),
        )
        self.assertEqual(exact.tier, ComputeTier.CLOUD)
        self.assertEqual(exact.backend_id, "permitted-cloud")
        self.assertEqual(exact.model_id, "challenger-v2")
        self.assertEqual(exact.config_sha256, SHA_B)

    def test_voc_exact_compute_identity_survives_restart_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            req = request(request_id="req-voc-identity-restart")
            evidence = voc(evidence_id="voc-identity-restart")
            store = ModelComputeRouterStore(path)
            first = store.route(
                req,
                self.candidates,
                policy(),
                as_of=T1,
                voc_evidence=evidence,
                domain_observation=slow_observation(),
            )
            self.assertEqual(first.tier, ComputeTier.CLOUD)

            reopened = ModelComputeRouterStore(path)
            readback = reopened.route(
                req,
                self.candidates,
                policy(),
                as_of=T1,
                voc_evidence=evidence,
                domain_observation=slow_observation(),
            )
            self.assertEqual(readback, first)

    def test_forged_domain_route_cannot_authorize_cloud_without_observation(self):
        decision = route_compute(
            request(request_id="req-forged-domain-route"),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=voc(evidence_id="voc-forged-domain-route"),
            domain_route=slow_route(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertEqual(decision.candidate_id, "local")
        self.assertIn("missing verified sport-domain", decision.reason)

    def test_stale_voc_and_non_slow_domain_route_fail_closed(self):
        stale_policy = policy(voc_max_age_seconds=Decimal("5"))
        stale = route_compute(
            request(request_id="req-stale"),
            self.candidates,
            stale_policy,
            as_of=T1,
            voc_evidence=voc(evidence_id="voc-stale"),
            domain_observation=slow_observation(),
        )
        self.assertEqual(stale.tier, ComputeTier.LOCAL)
        self.assertIn("stale", stale.reason)

        baseline_domain = route_compute(
            request(request_id="req-domain"),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=voc(evidence_id="voc-domain"),
            domain_observation=slow_observation(
                observation_id="fitness-baseline",
                compute_duration_seconds=metric("6", "seconds"),
            ),
        )
        self.assertEqual(baseline_domain.tier, ComputeTier.LOCAL)
        self.assertIn("sport-domain", baseline_domain.reason)

    def test_deadline_budget_and_capability_fail_closed(self):
        deadline = route_compute(
            request(
                request_id="req-deadline",
                decision_deadline=T1,
            ),
            self.candidates,
            policy(),
            as_of=T2,
        )
        self.assertEqual(deadline.tier, ComputeTier.WAIT)

        over_budget_baseline = route_compute(
            request(
                request_id="req-budget",
                max_cost=Decimal("0.5"),
            ),
            self.candidates,
            policy(),
            as_of=T1,
        )
        self.assertEqual(over_budget_baseline.tier, ComputeTier.WAIT)

        incapable = route_compute(
            request(
                request_id="req-capability",
                required_capability="ranking",
            ),
            self.candidates,
            policy(),
            as_of=T1,
        )
        self.assertEqual(incapable.tier, ComputeTier.WAIT)

    def test_deterministic_baseline_is_supported_without_model_escalation(self):
        deterministic = candidate(
            "det",
            tier=ComputeTier.DETERMINISTIC,
            backend_id="deterministic-engine",
            model_id="no-model",
            capabilities=("settlement-proof",),
            cost="0",
            latency="0",
        )
        req = ComputeRouteRequest(
            request_id="req-det",
            created_at=T0,
            decision_deadline=T1,
            required_capability="settlement-proof",
            data_classification=DataClassification.RESTRICTED,
            allow_cloud=False,
            max_cost=Decimal("0"),
            response_ttl_seconds=Decimal("10"),
            baseline_candidate_id="det",
        )
        decision = route_compute(
            req,
            (deterministic,),
            ComputeRoutingPolicy(
                policy_id="det-policy",
                policy_version=1,
            ),
            as_of=T0,
        )
        self.assertEqual(decision.tier, ComputeTier.DETERMINISTIC)
        self.assertEqual(decision.backend_id, "deterministic-engine")

    def test_restart_readback_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = ModelComputeRouterStore(path)
            decision = store.route(
                request(),
                self.candidates,
                policy(),
                as_of=T1,
                voc_evidence=voc(),
                domain_observation=slow_observation(),
            )
            reopened = ModelComputeRouterStore(path)
            self.assertEqual(
                reopened.get_decision("req-1"),
                decision,
            )

            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                raw["routes"][0]["domain_observation"]["observation_id"],
                "fitness-1",
            )
            raw["routes"][0]["decision"]["reason"] = "tampered"
            path.write_text(
                json.dumps(raw),
                encoding="utf-8",
            )
            with self.assertRaises(ModelComputeRouterError):
                ModelComputeRouterStore(path)

    def test_immutable_request_id_cannot_be_reused_with_changed_policy_or_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ModelComputeRouterStore(Path(tmp) / "router.json")
            store.route(
                request(),
                self.candidates,
                policy(),
                as_of=T1,
                voc_evidence=voc(),
                domain_observation=slow_observation(),
            )
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "immutable request id",
            ):
                store.route(
                    request(max_cost=Decimal("19")),
                    self.candidates,
                    policy(),
                    as_of=T1,
                    voc_evidence=voc(),
                    domain_observation=slow_observation(),
                )

    def test_execution_accounting_records_rejected_cost_but_never_accepts_late_stale_or_wrong_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = ModelComputeRouterStore(path)
            local_only = request(
                request_id="req-exec",
                allow_cloud=False,
            )
            decision = store.route(
                local_only,
                self.candidates,
                policy(),
                as_of=T1,
            )
            self.assertEqual(decision.tier, ComputeTier.LOCAL)

            accepted = store.record_execution(
                execution_id="exec-ok",
                request_id="req-exec",
                completed_at=T1,
                available_at=T1,
                backend_id="local-cpu",
                model_id="baseline-v1",
                config_sha256=SHA_A,
                actual_cost=Decimal("1.25"),
                actual_latency_seconds=Decimal("2"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )
            self.assertEqual(
                accepted.disposition,
                ExecutionDisposition.ACCEPTED,
            )

            availability_late_request = request(
                request_id="req-availability-late",
                allow_cloud=False,
                response_ttl_seconds=Decimal("30"),
            )
            store.route(
                availability_late_request,
                self.candidates,
                policy(),
                as_of=T1,
            )
            availability_late = store.record_execution(
                execution_id="exec-availability-late",
                request_id="req-availability-late",
                completed_at=T2,
                available_at=T4,
                backend_id="local-cpu",
                model_id="baseline-v1",
                config_sha256=SHA_A,
                actual_cost=Decimal("0"),
                actual_latency_seconds=Decimal("2"),
                evidence_sha256=SHA_C,
                as_of=T4,
            )
            self.assertEqual(
                availability_late.disposition,
                ExecutionDisposition.REJECTED_LATE,
            )

            stale = store.record_execution(
                execution_id="exec-stale",
                request_id="req-exec",
                completed_at=T1,
                available_at=T1,
                backend_id="local-cpu",
                model_id="baseline-v1",
                config_sha256=SHA_A,
                actual_cost=Decimal("0.75"),
                actual_latency_seconds=Decimal("2"),
                evidence_sha256=SHA_C,
                as_of=T3,
            )
            self.assertEqual(
                stale.disposition,
                ExecutionDisposition.REJECTED_STALE,
            )

            late = store.record_execution(
                execution_id="exec-late",
                request_id="req-exec",
                completed_at=T4,
                available_at=T4,
                backend_id="local-cpu",
                model_id="baseline-v1",
                config_sha256=SHA_A,
                actual_cost=Decimal("2"),
                actual_latency_seconds=Decimal("31"),
                evidence_sha256=SHA_C,
                as_of=T4,
            )
            self.assertEqual(
                late.disposition,
                ExecutionDisposition.REJECTED_LATE,
            )

            wrong = store.record_execution(
                execution_id="exec-wrong",
                request_id="req-exec",
                completed_at=T1,
                available_at=T1,
                backend_id="other-backend",
                model_id="baseline-v1",
                config_sha256=SHA_A,
                actual_cost=Decimal("3"),
                actual_latency_seconds=Decimal("2"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )
            self.assertEqual(
                wrong.disposition,
                ExecutionDisposition.REJECTED_IDENTITY,
            )
            self.assertEqual(
                store.total_actual_cost("req-exec"),
                Decimal("7.00"),
            )
            reopened = ModelComputeRouterStore(path)
            self.assertEqual(
                reopened.total_actual_cost("req-exec"),
                Decimal("7.00"),
            )

    def test_predecision_execution_is_rejected_but_cost_remains_accounted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = ModelComputeRouterStore(path)
            req = request(
                request_id="req-predecision-execution",
                allow_cloud=False,
            )
            decision = store.route(
                req,
                self.candidates,
                policy(),
                as_of=T1,
            )
            self.assertEqual(decision.decided_at, T1)

            predecision = store.record_execution(
                execution_id="exec-predecision",
                request_id=req.request_id,
                completed_at=T0,
                available_at=T0,
                backend_id="local-cpu",
                model_id="baseline-v1",
                config_sha256=SHA_A,
                actual_cost=Decimal("0.50"),
                actual_latency_seconds=Decimal("1"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )
            self.assertEqual(
                predecision.disposition,
                ExecutionDisposition.REJECTED_CAUSAL,
            )
            self.assertIn(
                "before routed decision",
                predecision.reason,
            )
            self.assertEqual(
                store.total_actual_cost(req.request_id),
                Decimal("0.50"),
            )

            reopened = ModelComputeRouterStore(path)
            self.assertEqual(
                reopened.total_actual_cost(req.request_id),
                Decimal("0.50"),
            )

    def test_actual_execution_cost_overruns_fail_closed_and_remain_accounted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = ModelComputeRouterStore(path)

            local_request = request(
                request_id="req-actual-request-budget",
                allow_cloud=False,
                max_cost=Decimal("2"),
            )
            local_decision = store.route(
                local_request,
                self.candidates,
                policy(),
                as_of=T1,
            )
            self.assertEqual(local_decision.tier, ComputeTier.LOCAL)
            request_overrun = store.record_execution(
                execution_id="exec-request-budget-overrun",
                request_id=local_request.request_id,
                completed_at=T1,
                available_at=T1,
                backend_id="local-cpu",
                model_id="baseline-v1",
                config_sha256=SHA_A,
                actual_cost=Decimal("2.01"),
                actual_latency_seconds=Decimal("2"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )
            self.assertEqual(
                request_overrun.disposition,
                ExecutionDisposition.REJECTED_COST,
            )
            self.assertIn("request budget", request_overrun.reason)

            cloud_request = request(
                request_id="req-actual-cloud-budget",
                max_cost=Decimal("20"),
            )
            cloud_policy = policy(max_cloud_cost=Decimal("10"))
            cloud_decision = store.route(
                cloud_request,
                self.candidates,
                cloud_policy,
                as_of=T1,
                voc_evidence=voc(evidence_id="voc-actual-cloud-budget"),
                domain_observation=slow_observation(),
            )
            self.assertEqual(cloud_decision.tier, ComputeTier.CLOUD)
            cloud_overrun = store.record_execution(
                execution_id="exec-cloud-budget-overrun",
                request_id=cloud_request.request_id,
                completed_at=T1,
                available_at=T1,
                backend_id="permitted-cloud",
                model_id="challenger-v2",
                config_sha256=SHA_B,
                actual_cost=Decimal("10.01"),
                actual_latency_seconds=Decimal("4"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )
            self.assertEqual(
                cloud_overrun.disposition,
                ExecutionDisposition.REJECTED_COST,
            )
            self.assertIn("policy cloud-cost", cloud_overrun.reason)

            reopened = ModelComputeRouterStore(path)
            self.assertEqual(
                reopened.total_actual_cost(local_request.request_id),
                Decimal("2.01"),
            )
            self.assertEqual(
                reopened.total_actual_cost(cloud_request.request_id),
                Decimal("10.01"),
            )

    def test_cumulative_actual_cost_budget_is_fail_closed_and_restart_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = ModelComputeRouterStore(path)

            local_request = request(
                request_id="req-cumulative-request-budget",
                allow_cloud=False,
                max_cost=Decimal("2"),
            )
            store.route(
                local_request,
                self.candidates,
                policy(),
                as_of=T1,
            )
            first_local = store.record_execution(
                execution_id="exec-cumulative-request-1",
                request_id=local_request.request_id,
                completed_at=T1,
                available_at=T1,
                backend_id="local-cpu",
                model_id="baseline-v1",
                config_sha256=SHA_A,
                actual_cost=Decimal("1.25"),
                actual_latency_seconds=Decimal("2"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )
            self.assertEqual(
                first_local.disposition,
                ExecutionDisposition.ACCEPTED,
            )
            second_local = store.record_execution(
                execution_id="exec-cumulative-request-2",
                request_id=local_request.request_id,
                completed_at=T1,
                available_at=T1,
                backend_id="local-cpu",
                model_id="baseline-v1",
                config_sha256=SHA_A,
                actual_cost=Decimal("0.80"),
                actual_latency_seconds=Decimal("2"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )
            self.assertEqual(
                second_local.disposition,
                ExecutionDisposition.REJECTED_COST,
            )
            self.assertIn(
                "cumulative actual execution cost",
                second_local.reason,
            )
            self.assertEqual(
                store.total_actual_cost(local_request.request_id),
                Decimal("2.05"),
            )

            cloud_request = request(
                request_id="req-cumulative-cloud-budget",
                max_cost=Decimal("20"),
            )
            cloud_policy = policy(max_cloud_cost=Decimal("10"))
            store.route(
                cloud_request,
                self.candidates,
                cloud_policy,
                as_of=T1,
                voc_evidence=voc(
                    evidence_id="voc-cumulative-cloud-budget"
                ),
                domain_observation=slow_observation(),
            )
            first_cloud = store.record_execution(
                execution_id="exec-cumulative-cloud-1",
                request_id=cloud_request.request_id,
                completed_at=T1,
                available_at=T1,
                backend_id="permitted-cloud",
                model_id="challenger-v2",
                config_sha256=SHA_B,
                actual_cost=Decimal("6"),
                actual_latency_seconds=Decimal("4"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )
            self.assertEqual(
                first_cloud.disposition,
                ExecutionDisposition.ACCEPTED,
            )
            second_cloud = store.record_execution(
                execution_id="exec-cumulative-cloud-2",
                request_id=cloud_request.request_id,
                completed_at=T1,
                available_at=T1,
                backend_id="permitted-cloud",
                model_id="challenger-v2",
                config_sha256=SHA_B,
                actual_cost=Decimal("4.01"),
                actual_latency_seconds=Decimal("4"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )
            self.assertEqual(
                second_cloud.disposition,
                ExecutionDisposition.REJECTED_COST,
            )
            self.assertIn(
                "cumulative actual cloud execution cost",
                second_cloud.reason,
            )
            self.assertEqual(
                store.total_actual_cost(cloud_request.request_id),
                Decimal("10.01"),
            )

            reopened = ModelComputeRouterStore(path)
            self.assertEqual(
                reopened.total_actual_cost(local_request.request_id),
                Decimal("2.05"),
            )
            self.assertEqual(
                reopened.total_actual_cost(cloud_request.request_id),
                Decimal("10.01"),
            )
            post_restart = reopened.record_execution(
                execution_id="exec-cumulative-request-after-restart",
                request_id=local_request.request_id,
                completed_at=T1,
                available_at=T1,
                backend_id="local-cpu",
                model_id="baseline-v1",
                config_sha256=SHA_A,
                actual_cost=Decimal("0"),
                actual_latency_seconds=Decimal("2"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )
            self.assertEqual(
                post_restart.disposition,
                ExecutionDisposition.REJECTED_COST,
            )
            self.assertEqual(
                reopened.total_actual_cost(local_request.request_id),
                Decimal("2.05"),
            )

    def test_future_voc_is_not_causally_usable(self):
        future = voc(
            evidence_id="voc-future",
            measured_at=T2,
            available_at=T2,
        )
        decision = route_compute(
            request(request_id="req-future"),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=future,
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertIn("not causally available", decision.reason)

    def test_decision_hash_binds_exact_backend_model_config_and_policy(self):
        decision = route_compute(
            request(),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=voc(),
            domain_observation=slow_observation(),
        )
        self.assertEqual(len(decision.decision_sha256), 64)
        payload = decision.payload()
        payload["backend_id"] = "tampered"
        from autosport.model_compute_router import ComputeRouteDecision

        with self.assertRaisesRegex(
            ModelComputeRouterError,
            "decision SHA-256",
        ):
            ComputeRouteDecision.from_payload(payload)

    def test_cloud_cost_limits_apply_to_estimate_and_measured_voc_cost(self):
        expensive_cloud = replace(
            self.cloud,
            estimated_cost=Decimal("11"),
        )
        estimated = route_compute(
            request(request_id="req-estimated"),
            (self.local, expensive_cloud),
            policy(max_cloud_cost=Decimal("10")),
            as_of=T1,
            voc_evidence=voc(evidence_id="voc-estimated"),
            domain_observation=slow_observation(),
        )
        self.assertEqual(estimated.tier, ComputeTier.LOCAL)

        measured = route_compute(
            request(request_id="req-measured"),
            self.candidates,
            policy(max_cloud_cost=Decimal("10")),
            as_of=T1,
            voc_evidence=voc(
                evidence_id="voc-measured",
                measured_compute_cost=Decimal("11"),
            ),
            domain_observation=slow_observation(),
        )
        self.assertEqual(measured.tier, ComputeTier.LOCAL)


if __name__ == "__main__":
    unittest.main()
