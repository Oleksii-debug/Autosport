import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from autosport.model_compute_router import (
    ComputeCandidate,
    ComputeRouteDecision,
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
from autosport.voc_evaluation import (
    OutcomeDerivedVOCScore,
    PairedVOCEvaluation,
    VOCEvaluationProvenance,
    VOCEvaluationStore,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-01T00:00:10Z"
T2 = "2026-01-01T00:00:20Z"
T3 = "2026-01-01T00:00:30Z"
T4 = "2026-01-01T00:00:40Z"


def rewrite_store_with_valid_state_hash(path, raw):
    body = {
        key: raw[key]
        for key in (
            "schema",
            "version",
            "routes",
            "executions",
            "execution_heads",
        )
    }
    raw["state_sha256"] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    path.write_text(json.dumps(raw), encoding="utf-8")


def rewrite_execution_heads_from_surviving_history(path, raw):
    executions_by_decision = {}
    for execution in raw["executions"]:
        executions_by_decision.setdefault(
            execution["decision_id"], []
        ).append(execution)

    heads = []
    for route in raw["routes"]:
        decision_id = route["decision"]["decision_id"]
        history = sorted(
            executions_by_decision.get(decision_id, []),
            key=lambda item: item["execution_sequence"],
        )
        record_sha256s = [
            item["execution_record_sha256"] for item in history
        ]
        cumulative = sum(
            (Decimal(item["actual_cost"]) for item in history),
            Decimal("0"),
        )
        unsigned = {
            "decision_id": decision_id,
            "terminal_sequence": len(history),
            "cumulative_incurred_cost": str(cumulative),
            "terminal_execution_record_sha256": (
                None if not history else record_sha256s[-1]
            ),
            "history_sha256": hashlib.sha256(
                json.dumps(
                    {
                        "decision_id": decision_id,
                        "execution_record_sha256s": record_sha256s,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
        }
        heads.append(
            {
                **unsigned,
                "head_sha256": hashlib.sha256(
                    json.dumps(
                        unsigned,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )
    raw["execution_heads"] = heads
    rewrite_store_with_valid_state_hash(path, raw)


def rewrite_route_with_valid_hashes(path, raw, route):
    unsigned = {
        key: route[key]
        for key in (
            "request",
            "policy",
            "candidates",
            "decision",
            "voc_evidence",
            "domain_observation",
            "domain_route",
        )
    }
    route["record_sha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    rewrite_store_with_valid_state_hash(path, raw)


def rewrite_execution_with_valid_record_hash(path, raw, execution):
    unsigned = {
        key: execution[key]
        for key in (
            "execution_id",
            "decision_id",
            "execution_sequence",
            "prior_incurred_cost",
            "completed_at",
            "available_at",
            "observed_at",
            "backend_id",
            "model_id",
            "config_sha256",
            "actual_cost",
            "actual_latency_seconds",
            "disposition",
            "reason",
            "evidence_sha256",
        )
    }
    execution["execution_record_sha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    rewrite_store_with_valid_state_hash(path, raw)


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
        decision_input_sha256=SHA_D,
        decision_evidence_sha256=SHA_D,
        voc_regime_id="regime-1",
        voc_urgency_id="routine",
        voc_contradiction_state="none",
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
        latency_opportunity_cost_penalty=Decimal("0.1"),
        measured_compute_cost=Decimal("5"),
    )
    explicit_evaluation = overrides.pop("evaluation", None)
    explicit_evaluation_sha256 = overrides.pop("evaluation_sha256", None)
    values.update(overrides)

    if explicit_evaluation is None:
        net_value = (
            values["challenger_utility"]
            - values["baseline_utility"]
            - values["compute_cost_penalty"]
            - values["latency_opportunity_cost_penalty"]
        )
        if net_value > Decimal("0"):
            interval_low = net_value / Decimal("2")
            interval_high = net_value + (net_value / Decimal("2"))
        elif net_value < Decimal("0"):
            interval_low = net_value + (net_value / Decimal("2"))
            interval_high = net_value / Decimal("2")
        else:
            interval_low = Decimal("0")
            interval_high = Decimal("0")
        evaluation = PairedVOCEvaluation(
            evaluation_id=values["evidence_id"],
            task_class="forecast",
            sport_id="table-tennis",
            league_id="league-1",
            regime_id="regime-1",
            urgency_id="routine",
            contradiction_state="none",
            baseline_candidate_id=values["baseline_candidate_id"],
            baseline_backend_id=values["baseline_backend_id"],
            baseline_model_id=values["baseline_model_id"],
            baseline_config_sha256=values["baseline_config_sha256"],
            challenger_candidate_id=values["challenger_candidate_id"],
            challenger_backend_id=values["challenger_backend_id"],
            challenger_model_id=values["challenger_model_id"],
            challenger_config_sha256=values["challenger_config_sha256"],
            decision_input_sha256=SHA_E,
            decision_context_sha256=hashlib.sha256(
                ("source-context:" + values["evidence_id"]).encode("utf-8")
            ).hexdigest(),
            decision_evidence_sha256=SHA_C,
            baseline_output_sha256=SHA_A,
            challenger_output_sha256=SHA_B,
            baseline_action="baseline-action",
            challenger_action="challenger-action",
            baseline_abstained=False,
            challenger_abstained=False,
            decision_at=T0,
            decision_deadline=T3,
            baseline_completed_at=values["measured_at"],
            challenger_completed_at=values["measured_at"],
            outcome_evidence_sha256=SHA_C,
            outcome_revealed_at=values["measured_at"],
            evaluated_at=values["measured_at"],
            scoring_rule_id="frozen-utility-v1",
            scoring_rule_sha256=SHA_A,
            research_protocol_id="voc-protocol-v1",
            research_protocol_sha256=SHA_B,
            holdout_access_id=f"{values['evidence_id']}:holdout",
            multiple_comparison_control_sha256=SHA_C,
            baseline_utility=values["baseline_utility"],
            challenger_utility=values["challenger_utility"],
            compute_cost_penalty=values["compute_cost_penalty"],
            latency_opportunity_cost_penalty=values[
                "latency_opportunity_cost_penalty"
            ],
            measured_compute_cost=values["measured_compute_cost"],
            paired_sample_count=4,
            effective_sample_size=4,
            support_fraction=Decimal("1"),
            incremental_value_interval_low=interval_low,
            incremental_value_interval_high=interval_high,
            provenance=(
                VOCEvaluationProvenance.MEASURED_SHADOW
                if values["provenance"]
                is VOCEvidenceProvenance.MEASURED_SHADOW
                else VOCEvaluationProvenance.SIMULATED
            ),
        )
    else:
        evaluation = explicit_evaluation

    values["evaluation"] = evaluation
    values["evaluation_sha256"] = (
        evaluation.evaluation_sha256
        if explicit_evaluation_sha256 is None
        else explicit_evaluation_sha256
    )
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


class _FixtureCanonicalVOCResolver:
    """Test-only canonical-evidence registry for router integration tests."""

    def __init__(self):
        self._records = {}
        self._contexts = {}

    def publish(self, value):
        self._records[value.evaluation_id] = value
        self._contexts[value.decision_context_sha256] = {
            "request_id": f"source:{value.evaluation_id}",
            "decision_input_sha256": value.decision_input_sha256,
            "task_class": value.task_class,
            "sport_id": value.sport_id,
            "league_id": value.league_id,
            "regime_id": value.regime_id,
            "urgency_id": value.urgency_id,
            "contradiction_state": value.contradiction_state,
        }

    def publish_context(self, context_sha256, context):
        self._contexts[context_sha256] = dict(context)

    def resolve(self, evaluation, *, as_of):
        value = self._records.get(evaluation.evaluation_id)
        if value is None:
            return None
        if value.payload() != evaluation.payload():
            return None
        return value

    def resolve_decision_context(self, context_sha256, *, as_of):
        value = self._contexts.get(context_sha256)
        return None if value is None else dict(value)

    def resolve_score(self, evaluation, *, as_of):
        value = self.resolve(evaluation, as_of=as_of)
        if value is None:
            return None
        return OutcomeDerivedVOCScore(
            evaluation_id=value.evaluation_id,
            available_at=value.evaluated_at,
            outcome_evidence_sha256=value.outcome_evidence_sha256,
            scoring_rule_sha256=value.scoring_rule_sha256,
            research_protocol_sha256=value.research_protocol_sha256,
            holdout_access_id=value.holdout_access_id,
            multiple_comparison_control_sha256=(
                value.multiple_comparison_control_sha256
            ),
            baseline_utility=value.baseline_utility,
            challenger_utility=value.challenger_utility,
            compute_cost_penalty=value.compute_cost_penalty,
            latency_opportunity_cost_penalty=(
                value.latency_opportunity_cost_penalty
            ),
            measured_compute_cost=value.measured_compute_cost,
            paired_sample_count=value.paired_sample_count,
            effective_sample_size=value.effective_sample_size,
            support_fraction=value.support_fraction,
            incremental_value_interval_low=(
                value.incremental_value_interval_low
            ),
            incremental_value_interval_high=(
                value.incremental_value_interval_high
            ),
            source_artifact_sha256=SHA_D,
        )


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
        self._voc_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._voc_tmp.cleanup)
        self._canonical_voc = _FixtureCanonicalVOCResolver()
        self.voc_store = VOCEvaluationStore(
            Path(self._voc_tmp.name) / "voc-evaluations.json",
            canonical_authority_resolver=self._canonical_voc,
        )

    def qualified_voc(self, **overrides):
        evidence = voc(**overrides)
        if evidence.evaluation is not None:
            self._canonical_voc.publish(evidence.evaluation)
            self.voc_store.record(evidence.evaluation)
        return evidence

    def canonical_request(
        self,
        value,
        observation,
        *,
        policy_value=None,
        candidate_values=None,
        context_overrides=None,
        publish=True,
    ):
        active_policy = policy() if policy_value is None else policy_value
        active_candidates = (
            self.candidates
            if candidate_values is None
            else tuple(candidate_values)
        )
        cloud_backend_id = next(
            (
                item.backend_id
                for item in active_candidates
                if item.candidate_id == value.cloud_candidate_id
                and item.tier is ComputeTier.CLOUD
            ),
            "NONE",
        )
        context = {
            "request_id": value.request_id,
            "decision_input_sha256": value.decision_input_sha256,
            "task_class": value.required_capability,
            "data_classification": value.data_classification.value,
            "sport_id": observation.sport_id,
            "league_id": observation.league_id,
            "regime_id": value.voc_regime_id,
            "urgency_id": value.voc_urgency_id,
            "contradiction_state": value.voc_contradiction_state,
            "routing_policy_id": active_policy.policy_id,
            "routing_policy_version": str(active_policy.policy_version),
            "routing_policy_sha256": hashlib.sha256(
                json.dumps(
                    active_policy.payload(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
            "cloud_permission": (
                "ALLOW" if active_policy.cloud_enabled else "DENY"
            ),
            "cloud_backend_id": cloud_backend_id,
        }
        if context_overrides:
            context.update(context_overrides)
        digest = hashlib.sha256(
            json.dumps(
                context,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if publish:
            self._canonical_voc.publish_context(digest, context)
        return replace(value, decision_evidence_sha256=digest)

    def route_compute(self, *args, **kwargs):
        bind_current_context = kwargs.pop("bind_current_context", True)
        kwargs.setdefault("voc_evaluation_store", self.voc_store)
        if args and bind_current_context:
            value = args[0]
            observation = kwargs.get("domain_observation")
            if (
                isinstance(value, ComputeRouteRequest)
                and value.decision_evidence_sha256 is not None
                and isinstance(observation, SportDomainFitnessObservation)
            ):
                value = self.canonical_request(
                    value,
                    observation,
                    policy_value=(
                        args[2]
                        if len(args) > 2
                        and isinstance(args[2], ComputeRoutingPolicy)
                        else None
                    ),
                    candidate_values=(
                        args[1] if len(args) > 1 else None
                    ),
                )
                args = (value, *args[1:])
        return route_compute(*args, **kwargs)

    def store_route(self, store, value, candidates, policy_value, **kwargs):
        bind_current_context = kwargs.pop("bind_current_context", True)
        observation = kwargs.get("domain_observation")
        if (
            bind_current_context
            and value.decision_evidence_sha256 is not None
            and isinstance(observation, SportDomainFitnessObservation)
        ):
            value = self.canonical_request(
                value,
                observation,
                policy_value=policy_value,
                candidate_values=candidates,
            )
        return store.route(value, candidates, policy_value, **kwargs)

    def router_store(self, path):
        return ModelComputeRouterStore(
            path,
            voc_evaluation_store=self.voc_store,
        )

    def test_cloud_is_disabled_by_default_and_falls_back_local(self):
        disabled = ComputeRoutingPolicy(
            policy_id="default",
            policy_version=1,
        )
        decision = self.route_compute(
            request(),
            self.candidates,
            disabled,
            as_of=T1,
            voc_evidence=self.qualified_voc(),
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertEqual(decision.candidate_id, "local")
        self.assertIn("disabled", decision.reason)

    def test_private_data_never_escalates_to_cloud(self):
        decision = self.route_compute(
            request(data_classification=DataClassification.PRIVATE),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=self.qualified_voc(),
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertIn("non-public", decision.reason)

    def test_cloud_requires_product_owned_matching_data_classification(self):
        observation = slow_observation()
        evidence = self.qualified_voc(evidence_id="voc-data-classification")

        mismatched_request = request(request_id="req-classification-mismatch")
        mismatched_request = self.canonical_request(
            mismatched_request,
            observation,
            context_overrides={
                "data_classification": DataClassification.PRIVATE.value,
            },
        )
        mismatched = self.route_compute(
            mismatched_request,
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=evidence,
            domain_observation=observation,
            bind_current_context=False,
        )
        self.assertEqual(mismatched.tier, ComputeTier.LOCAL)
        self.assertIn("data classification does not match", mismatched.reason)

        legacy_request = request(request_id="req-classification-legacy")
        legacy_context = {
            "request_id": legacy_request.request_id,
            "decision_input_sha256": legacy_request.decision_input_sha256,
            "task_class": legacy_request.required_capability,
            "sport_id": observation.sport_id,
            "league_id": observation.league_id,
            "regime_id": legacy_request.voc_regime_id,
            "urgency_id": legacy_request.voc_urgency_id,
            "contradiction_state": legacy_request.voc_contradiction_state,
        }
        legacy_digest = hashlib.sha256(
            json.dumps(
                legacy_context,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self._canonical_voc.publish_context(legacy_digest, legacy_context)
        legacy_request = replace(
            legacy_request,
            decision_evidence_sha256=legacy_digest,
        )
        legacy = self.route_compute(
            legacy_request,
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=evidence,
            domain_observation=observation,
            bind_current_context=False,
        )
        self.assertEqual(legacy.tier, ComputeTier.LOCAL)
        self.assertIn("lacks data classification", legacy.reason)

    def test_cloud_requires_product_owned_permission_scope(self):
        observation = slow_observation()
        evidence = self.qualified_voc(evidence_id="voc-cloud-permission-scope")

        denied_request = request(request_id="req-cloud-permission-deny")
        denied_request = self.canonical_request(
            denied_request,
            observation,
            context_overrides={"cloud_permission": "DENY"},
        )
        denied = self.route_compute(
            denied_request,
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=evidence,
            domain_observation=observation,
            bind_current_context=False,
        )
        self.assertEqual(denied.tier, ComputeTier.LOCAL)
        self.assertIn("does not allow cloud", denied.reason)

        wrong_backend_request = request(
            request_id="req-cloud-permission-wrong-backend"
        )
        wrong_backend_request = self.canonical_request(
            wrong_backend_request,
            observation,
            context_overrides={"cloud_backend_id": "other-cloud"},
        )
        wrong_backend = self.route_compute(
            wrong_backend_request,
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=evidence,
            domain_observation=observation,
            bind_current_context=False,
        )
        self.assertEqual(wrong_backend.tier, ComputeTier.LOCAL)
        self.assertIn("cloud backend", wrong_backend.reason)

        wrong_policy_request = request(
            request_id="req-cloud-permission-wrong-policy"
        )
        wrong_policy_request = self.canonical_request(
            wrong_policy_request,
            observation,
            context_overrides={"routing_policy_version": "999"},
        )
        wrong_policy = self.route_compute(
            wrong_policy_request,
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=evidence,
            domain_observation=observation,
            bind_current_context=False,
        )
        self.assertEqual(wrong_policy.tier, ComputeTier.LOCAL)
        self.assertIn("routing policy", wrong_policy.reason)

        canonical_policy = policy()
        altered_policy = policy(max_cloud_cost=Decimal("999"))
        digest_request = request(
            request_id="req-cloud-permission-policy-digest"
        )
        digest_request = self.canonical_request(
            digest_request,
            observation,
            policy_value=canonical_policy,
        )
        digest_mismatch = self.route_compute(
            digest_request,
            self.candidates,
            altered_policy,
            as_of=T1,
            voc_evidence=evidence,
            domain_observation=observation,
            bind_current_context=False,
        )
        self.assertEqual(digest_mismatch.tier, ComputeTier.LOCAL)
        self.assertIn("routing policy", digest_mismatch.reason)

    def test_v2_context_is_readable_but_cannot_mint_cloud_permission(self):
        observation = slow_observation()
        evidence = self.qualified_voc(evidence_id="voc-v2-permission")
        value = request(request_id="req-v2-permission")
        context = {
            "request_id": value.request_id,
            "decision_input_sha256": value.decision_input_sha256,
            "task_class": value.required_capability,
            "data_classification": value.data_classification.value,
            "sport_id": observation.sport_id,
            "league_id": observation.league_id,
            "regime_id": value.voc_regime_id,
            "urgency_id": value.voc_urgency_id,
            "contradiction_state": value.voc_contradiction_state,
        }
        digest = hashlib.sha256(
            json.dumps(
                context,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self._canonical_voc.publish_context(digest, context)
        value = replace(value, decision_evidence_sha256=digest)

        decision = self.route_compute(
            value,
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=evidence,
            domain_observation=observation,
            bind_current_context=False,
        )

        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertIn("lacks product cloud permission", decision.reason)

    def test_positive_voc_still_needs_product_issued_cloud_permission(self):
        decision = self.route_compute(
            request(),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=self.qualified_voc(),
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertEqual(decision.backend_id, "local-cpu")
        self.assertEqual(decision.model_id, "baseline-v1")
        self.assertEqual(decision.config_sha256, SHA_A)
        self.assertEqual(decision.domain_observation_id, "fitness-1")
        self.assertIn(
            "product-issued cloud permission authority is unavailable",
            decision.reason,
        )

        no_voc = self.route_compute(
            replace(request(), request_id="req-no-voc"),
            self.candidates,
            policy(),
            as_of=T1,
            domain_observation=slow_observation(),
        )
        self.assertEqual(no_voc.tier, ComputeTier.LOCAL)

        nonpositive = self.route_compute(
            replace(request(), request_id="req-nonpositive"),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=self.qualified_voc(
                evidence_id="voc-zero",
                challenger_utility=Decimal("1.5"),
                compute_cost_penalty=Decimal("0.5"),
            ),
            domain_observation=slow_observation(),
        )
        self.assertEqual(nonpositive.tier, ComputeTier.LOCAL)
        self.assertIn("non-positive", nonpositive.reason)

        latency_nonpositive = self.route_compute(
            replace(request(), request_id="req-latency-nonpositive"),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=self.qualified_voc(
                evidence_id="voc-latency-nonpositive",
                compute_cost_penalty=Decimal("0.2"),
                latency_opportunity_cost_penalty=Decimal("0.8"),
            ),
            domain_observation=slow_observation(),
        )
        self.assertEqual(latency_nonpositive.tier, ComputeTier.LOCAL)
        self.assertIn("non-positive", latency_nonpositive.reason)

        with self.assertRaisesRegex(
            ValueError,
            "latency_opportunity_cost_penalty must be non-negative",
        ):
            self.qualified_voc(
                evidence_id="voc-negative-latency-cost",
                latency_opportunity_cost_penalty=Decimal("-0.01"),
            )

        simulated = self.route_compute(
            replace(request(), request_id="req-simulated"),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=self.qualified_voc(
                evidence_id="voc-simulated",
                provenance=VOCEvidenceProvenance.SIMULATED,
            ),
            domain_observation=slow_observation(),
        )
        self.assertEqual(simulated.tier, ComputeTier.LOCAL)

    def test_voc_history_requires_distinct_current_identity_and_matching_stratum(self):
        evidence = self.qualified_voc(evidence_id="voc-route-scope")

        current = self.route_compute(
            request(request_id="req-current-context"),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=evidence,
            domain_observation=slow_observation(),
        )
        self.assertEqual(current.tier, ComputeTier.LOCAL)
        self.assertIn(
            "product-issued cloud permission authority is unavailable",
            current.reason,
        )

        source_decision = self.route_compute(
            request(
                request_id="req-replayed-source-input",
                decision_input_sha256=evidence.evaluation.decision_input_sha256,
            ),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=evidence,
            domain_observation=slow_observation(),
        )
        self.assertEqual(source_decision.tier, ComputeTier.LOCAL)
        self.assertIn("cannot authorize its source decision", source_decision.reason)

        wrong_stratum = self.route_compute(
            request(
                request_id="req-wrong-stratum",
                voc_urgency_id="urgent",
            ),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=evidence,
            domain_observation=slow_observation(),
        )
        self.assertEqual(wrong_stratum.tier, ComputeTier.LOCAL)
        self.assertIn("routing stratum", wrong_stratum.reason)

        missing_context = self.route_compute(
            request(
                request_id="req-missing-context",
                decision_evidence_sha256=None,
            ),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=evidence,
            domain_observation=slow_observation(),
        )
        self.assertEqual(missing_context.tier, ComputeTier.LOCAL)
        self.assertIn("missing canonical current decision-context identity", missing_context.reason)

        missing_input = self.route_compute(
            request(
                request_id="req-missing-input",
                decision_input_sha256=None,
            ),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=evidence,
            domain_observation=slow_observation(),
        )
        self.assertEqual(missing_input.tier, ComputeTier.LOCAL)
        self.assertIn("missing canonical current decision-input identity", missing_input.reason)

        caller_only = self.route_compute(
            request(
                request_id="req-caller-only-context",
                decision_evidence_sha256="e" * 64,
            ),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=evidence,
            domain_observation=slow_observation(),
            bind_current_context=False,
        )
        self.assertEqual(caller_only.tier, ComputeTier.LOCAL)
        self.assertIn("canonical current VOC decision context", caller_only.reason)

        mismatched_request = request(request_id="req-canonical-context-mismatch")
        observation = slow_observation()
        mismatched_request = self.canonical_request(
            mismatched_request,
            observation,
            context_overrides={"urgency_id": "urgent"},
        )
        canonical_mismatch = self.route_compute(
            mismatched_request,
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=evidence,
            domain_observation=observation,
            bind_current_context=False,
        )
        self.assertEqual(canonical_mismatch.tier, ComputeTier.LOCAL)
        self.assertIn("does not match the current request", canonical_mismatch.reason)

    def test_voc_exact_compute_identity_cannot_be_reused_under_same_candidate_ids(self):
        mismatched = self.qualified_voc(
            evidence_id="voc-old-compute-identity",
            challenger_backend_id="retired-cloud",
            challenger_model_id="challenger-v1",
            challenger_config_sha256=SHA_C,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router-mismatched-voc.json"
            req = request(request_id="req-voc-old-compute-identity")
            store = self.router_store(path)
            rejected = self.store_route(store, 
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

            reopened = self.router_store(path)
            readback = self.store_route(reopened, 
                req,
                self.candidates,
                policy(),
                as_of=T1,
                voc_evidence=mismatched,
                domain_observation=slow_observation(),
            )
            self.assertEqual(readback, rejected)

        exact = self.route_compute(
            request(request_id="req-voc-exact-compute-identity"),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=self.qualified_voc(evidence_id="voc-exact-compute-identity"),
            domain_observation=slow_observation(),
        )
        self.assertEqual(exact.tier, ComputeTier.LOCAL)
        self.assertEqual(exact.backend_id, "local-cpu")
        self.assertEqual(exact.model_id, "baseline-v1")
        self.assertEqual(exact.config_sha256, SHA_A)
        self.assertIn(
            "product-issued cloud permission authority is unavailable",
            exact.reason,
        )

    def test_voc_exact_compute_identity_survives_restart_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            req = request(request_id="req-voc-identity-restart")
            evidence = self.qualified_voc(evidence_id="voc-identity-restart")
            store = self.router_store(path)
            first = self.store_route(store, 
                req,
                self.candidates,
                policy(),
                as_of=T1,
                voc_evidence=evidence,
                domain_observation=slow_observation(),
            )
            self.assertEqual(first.tier, ComputeTier.LOCAL)
            self.assertIn(
                "product-issued cloud permission authority is unavailable",
                first.reason,
            )

            reopened = self.router_store(path)
            readback = self.store_route(reopened, 
                req,
                self.candidates,
                policy(),
                as_of=T1,
                voc_evidence=evidence,
                domain_observation=slow_observation(),
            )
            self.assertEqual(readback, first)

            changed_latency_cost = voc(
                evidence_id="voc-identity-restart",
                latency_opportunity_cost_penalty=Decimal("0.2"),
            )
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "immutable request id conflicts",
            ):
                self.store_route(reopened, 
                    req,
                    self.candidates,
                    policy(),
                    as_of=T1,
                    voc_evidence=changed_latency_cost,
                    domain_observation=slow_observation(),
                )

    def test_forged_domain_route_cannot_authorize_cloud_without_observation(self):
        decision = self.route_compute(
            request(request_id="req-forged-domain-route"),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=self.qualified_voc(evidence_id="voc-forged-domain-route"),
            domain_route=slow_route(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertEqual(decision.candidate_id, "local")
        self.assertIn("missing verified sport-domain", decision.reason)

    def test_stale_voc_and_non_slow_domain_route_fail_closed(self):
        stale_policy = policy(voc_max_age_seconds=Decimal("5"))
        stale = self.route_compute(
            request(request_id="req-stale"),
            self.candidates,
            stale_policy,
            as_of=T1,
            voc_evidence=self.qualified_voc(evidence_id="voc-stale"),
            domain_observation=slow_observation(),
        )
        self.assertEqual(stale.tier, ComputeTier.LOCAL)
        self.assertIn("stale", stale.reason)

        baseline_domain = self.route_compute(
            request(request_id="req-domain"),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=self.qualified_voc(evidence_id="voc-domain"),
            domain_observation=slow_observation(
                observation_id="fitness-baseline",
                compute_duration_seconds=metric("6", "seconds"),
            ),
        )
        self.assertEqual(baseline_domain.tier, ComputeTier.LOCAL)
        self.assertIn("sport-domain", baseline_domain.reason)

    def test_deadline_budget_and_capability_fail_closed(self):
        deadline = self.route_compute(
            request(
                request_id="req-deadline",
                decision_deadline=T1,
            ),
            self.candidates,
            policy(),
            as_of=T2,
        )
        self.assertEqual(deadline.tier, ComputeTier.WAIT)

        exact_deadline = self.route_compute(
            request(
                request_id="req-deadline-exact",
                decision_deadline=T1,
            ),
            self.candidates,
            policy(),
            as_of=T1,
        )
        self.assertEqual(exact_deadline.tier, ComputeTier.WAIT)
        self.assertIn("deadline", exact_deadline.reason)

        boundary_candidate = candidate(
            "deadline-boundary-local",
            backend_id="local-cpu",
            model_id="baseline-v1",
            config_sha256=SHA_A,
            cost="0",
            latency="10",
        )
        exact_estimated_completion = self.route_compute(
            request(
                request_id="req-deadline-estimate-exact",
                decision_deadline=T1,
                max_cost=Decimal("1"),
                baseline_candidate_id="deadline-boundary-local",
                cloud_candidate_id=None,
            ),
            (boundary_candidate,),
            policy(),
            as_of=T0,
        )
        self.assertEqual(
            exact_estimated_completion.tier,
            ComputeTier.WAIT,
        )

        over_budget_baseline = self.route_compute(
            request(
                request_id="req-budget",
                max_cost=Decimal("0.5"),
            ),
            self.candidates,
            policy(),
            as_of=T1,
        )
        self.assertEqual(over_budget_baseline.tier, ComputeTier.WAIT)

        incapable = self.route_compute(
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
        decision = self.route_compute(
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
            store = self.router_store(path)
            decision = self.store_route(store, 
                request(),
                self.candidates,
                policy(),
                as_of=T1,
                voc_evidence=self.qualified_voc(),
                domain_observation=slow_observation(),
            )
            reopened = self.router_store(path)
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
                self.router_store(path)

    def test_restart_rejects_semantically_cross_linked_route_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = self.router_store(path)

            cloud_request = request(request_id="req-route-cloud")
            cloud_decision = self.store_route(store, 
                cloud_request,
                self.candidates,
                policy(),
                as_of=T1,
                voc_evidence=self.qualified_voc(evidence_id="voc-route-cloud"),
                domain_observation=slow_observation(),
            )
            self.assertEqual(cloud_decision.tier, ComputeTier.LOCAL)
            self.assertIn(
                "product-issued cloud permission authority is unavailable",
                cloud_decision.reason,
            )

            local_request = request(
                request_id="req-route-local",
                allow_cloud=False,
                cloud_candidate_id=None,
            )
            local_decision = self.store_route(store, 
                local_request,
                self.candidates,
                policy(),
                as_of=T1,
            )
            self.assertEqual(local_decision.tier, ComputeTier.LOCAL)

            original = path.read_text(encoding="utf-8")

            def route_record(raw, request_id):
                return next(
                    item
                    for item in raw["routes"]
                    if item["request"]["request_id"] == request_id
                )

            raw = json.loads(original)
            forged = route_record(raw, "req-route-cloud")
            forged["request"]["request_id"] = "forged-request"
            rewrite_route_with_valid_hashes(path, raw, forged)
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "decision request does not match persisted request",
            ):
                self.router_store(path)

            raw = json.loads(original)
            forged = route_record(raw, "req-route-cloud")
            forged["policy"]["policy_version"] = 2
            rewrite_route_with_valid_hashes(path, raw, forged)
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "decision policy does not match persisted policy",
            ):
                self.router_store(path)

            raw = json.loads(original)
            forged = route_record(raw, "req-route-local")
            next(
                item
                for item in forged["candidates"]
                if item["candidate_id"] == "local"
            )["backend_id"] = "forged-local-backend"
            rewrite_route_with_valid_hashes(path, raw, forged)
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "decision compute identity does not match persisted candidate",
            ):
                self.router_store(path)

            raw = json.loads(original)
            forged = route_record(raw, "req-route-cloud")
            forged["voc_evidence"]["evidence_id"] = "voc-cross-linked"
            rewrite_route_with_valid_hashes(path, raw, forged)
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "VOC evaluation identity does not match evidence_id|"
                "decision VOC evidence does not match persisted evidence",
            ):
                self.router_store(path)

            raw = json.loads(original)
            forged = route_record(raw, "req-route-cloud")
            forged["domain_observation"]["observation_id"] = (
                "fitness-cross-linked"
            )
            rewrite_route_with_valid_hashes(path, raw, forged)
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "decision domain observation does not match persisted evidence",
            ):
                self.router_store(path)

    def test_restart_rejects_semantically_unauthorized_cloud_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = self.router_store(path)
            cloud_request = request(request_id="req-cloud-authority")
            decision = self.store_route(store, 
                cloud_request,
                self.candidates,
                policy(),
                as_of=T1,
                voc_evidence=self.qualified_voc(evidence_id="voc-cloud-authority"),
                domain_observation=slow_observation(),
            )
            self.assertEqual(decision.tier, ComputeTier.LOCAL)
            self.assertIn(
                "product-issued cloud permission authority is unavailable",
                decision.reason,
            )
            original = path.read_text(encoding="utf-8")

            def assert_semantic_forgery_rejected(mutate):
                raw = json.loads(original)
                route = next(
                    item
                    for item in raw["routes"]
                    if item["request"]["request_id"]
                    == "req-cloud-authority"
                )
                mutate(route)
                rewrite_route_with_valid_hashes(path, raw, route)
                with self.assertRaisesRegex(
                    ModelComputeRouterError,
                    "persisted LOCAL decision is not authorized "
                    "by persisted route inputs|"
                    "VOC evaluation utility/cost values do not match evidence",
                ):
                    self.router_store(path)

            assert_semantic_forgery_rejected(
                lambda route: route["request"].__setitem__(
                    "allow_cloud", False
                )
            )
            assert_semantic_forgery_rejected(
                lambda route: route["policy"].__setitem__(
                    "cloud_enabled", False
                )
            )
            assert_semantic_forgery_rejected(
                lambda route: route["request"].__setitem__(
                    "data_classification", DataClassification.PRIVATE.value
                )
            )
            assert_semantic_forgery_rejected(
                lambda route: route["voc_evidence"].__setitem__(
                    "challenger_utility", "1.1"
                )
            )

            def revoke_domain_authorization(route):
                route["domain_observation"][
                    "compute_duration_seconds"
                ]["value"] = "6"
                route["domain_route"] = {
                    "status": RouteStatus.ROUTE_BASELINE.value,
                    "reason": (
                        "slow-analysis budget does not fit measured "
                        "reaction slack; stay on baseline route"
                    ),
                    "observation_id": "fitness-1",
                    "domain_profile": DomainProfile.SLOW.value,
                }

            assert_semantic_forgery_rejected(
                revoke_domain_authorization
            )

    def test_restart_rejects_rehashed_ineligible_wait_to_local_forgery(self):
        scenarios = (
            (
                "capability",
                {
                    "required_capability": "ranking",
                    "max_cost": Decimal("20"),
                    "decision_deadline": T3,
                },
            ),
            (
                "budget",
                {
                    "required_capability": "forecast",
                    "max_cost": Decimal("0.5"),
                    "decision_deadline": T3,
                },
            ),
            (
                "deadline",
                {
                    "required_capability": "forecast",
                    "max_cost": Decimal("20"),
                    "decision_deadline": T1,
                },
            ),
        )
        for name, constraints in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "router.json"
                store = self.router_store(path)
                req = request(
                    request_id=f"req-wait-forgery-{name}",
                    allow_cloud=False,
                    cloud_candidate_id=None,
                    **constraints,
                )
                route_policy = policy()
                decision = self.store_route(store, 
                    req,
                    self.candidates,
                    route_policy,
                    as_of=T1,
                )
                self.assertEqual(decision.tier, ComputeTier.WAIT)

                raw = json.loads(path.read_text(encoding="utf-8"))
                route = raw["routes"][0]
                route["decision"] = ComputeRouteDecision.build(
                    decision_id=f"{req.request_id}:local",
                    request_id=req.request_id,
                    decided_at=T1,
                    policy=route_policy,
                    tier=ComputeTier.LOCAL,
                    candidate=self.candidates[0],
                    reason=(
                        "bounded baseline route selected; "
                        "no cloud candidate requested"
                    ),
                    voc_evidence_id=None,
                    domain_observation_id=None,
                ).payload()
                rewrite_route_with_valid_hashes(path, raw, route)

                with self.assertRaisesRegex(
                    ModelComputeRouterError,
                    "persisted LOCAL decision is not authorized "
                    "by persisted route inputs",
                ):
                    self.router_store(path)

    def test_restart_rejects_semantically_forged_execution_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = self.router_store(path)

            local_request = request(
                request_id="req-load-local",
                allow_cloud=False,
                cloud_candidate_id=None,
                max_cost=Decimal("2"),
            )
            local_decision = self.store_route(store, 
                local_request,
                self.candidates,
                policy(),
                as_of=T1,
            )
            self.assertEqual(local_decision.tier, ComputeTier.LOCAL)
            local_execution = store.record_execution(
                execution_id="exec-load-local",
                request_id=local_request.request_id,
                completed_at=T1,
                available_at=T1,
                backend_id="local-cpu",
                model_id="baseline-v1",
                config_sha256=SHA_A,
                actual_cost=Decimal("1"),
                actual_latency_seconds=Decimal("2"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )
            self.assertEqual(
                local_execution.disposition,
                ExecutionDisposition.ACCEPTED,
            )

            cloud_request = request(
                request_id="req-load-cloud",
                max_cost=Decimal("20"),
            )
            cloud_policy = policy(max_cloud_cost=Decimal("10"))
            cloud_decision = self.store_route(store, 
                cloud_request,
                self.candidates,
                cloud_policy,
                as_of=T1,
                voc_evidence=self.qualified_voc(evidence_id="voc-load-cloud"),
                domain_observation=slow_observation(),
            )
            self.assertEqual(cloud_decision.tier, ComputeTier.CLOUD)
            cloud_execution = store.record_execution(
                execution_id="exec-load-cloud",
                request_id=cloud_request.request_id,
                completed_at=T1,
                available_at=T1,
                backend_id="permitted-cloud",
                model_id="challenger-v2",
                config_sha256=SHA_B,
                actual_cost=Decimal("5"),
                actual_latency_seconds=Decimal("4"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )
            self.assertEqual(
                cloud_execution.disposition,
                ExecutionDisposition.ACCEPTED,
            )

            original = path.read_text(encoding="utf-8")

            raw = json.loads(original)
            next(
                item
                for item in raw["executions"]
                if item["execution_id"] == "exec-load-local"
            )["decision_id"] = "orphan:decision"
            rewrite_store_with_valid_state_hash(path, raw)
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "unknown route decision",
            ):
                self.router_store(path)

            raw = json.loads(original)
            next(
                item
                for item in raw["executions"]
                if item["execution_id"] == "exec-load-local"
            )["backend_id"] = "forged-local"
            rewrite_store_with_valid_state_hash(path, raw)
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "ACCEPTED execution identity",
            ):
                self.router_store(path)

            raw = json.loads(original)
            next(
                item
                for item in raw["executions"]
                if item["execution_id"] == "exec-load-local"
            )["actual_cost"] = "2.01"
            rewrite_store_with_valid_state_hash(path, raw)
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "accepted execution cost exceeds request budget",
            ):
                self.router_store(path)

            raw = json.loads(original)
            next(
                item
                for item in raw["executions"]
                if item["execution_id"] == "exec-load-cloud"
            )["actual_cost"] = "10.01"
            rewrite_store_with_valid_state_hash(path, raw)
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "accepted cloud execution cost exceeds policy",
            ):
                self.router_store(path)

    def test_restart_rejects_rehashed_rejected_execution_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = self.router_store(path)
            req = request(
                request_id="req-rejected-tamper",
                allow_cloud=False,
                cloud_candidate_id=None,
                max_cost=Decimal("2"),
            )
            self.store_route(store, 
                req,
                self.candidates,
                policy(),
                as_of=T1,
            )
            accepted = store.record_execution(
                execution_id="exec-rejected-tamper-1",
                request_id=req.request_id,
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
            rejected = store.record_execution(
                execution_id="exec-rejected-tamper-2",
                request_id=req.request_id,
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
                accepted.disposition,
                ExecutionDisposition.ACCEPTED,
            )
            self.assertEqual(
                rejected.disposition,
                ExecutionDisposition.REJECTED_COST,
            )

            stale_req = request(
                request_id="req-rejected-stale-tamper",
                allow_cloud=False,
                cloud_candidate_id=None,
                max_cost=Decimal("2"),
                response_ttl_seconds=Decimal("5"),
            )
            self.store_route(store, 
                stale_req,
                self.candidates,
                policy(),
                as_of=T1,
            )
            stale = store.record_execution(
                execution_id="exec-rejected-stale-tamper",
                request_id=stale_req.request_id,
                completed_at=T1,
                available_at=T1,
                backend_id="local-cpu",
                model_id="baseline-v1",
                config_sha256=SHA_A,
                actual_cost=Decimal("0.10"),
                actual_latency_seconds=Decimal("2"),
                evidence_sha256=SHA_C,
                as_of=T3,
            )
            self.assertEqual(
                stale.disposition,
                ExecutionDisposition.REJECTED_STALE,
            )
            original = path.read_text(encoding="utf-8")

            for field, value, expected_error in (
                (
                    "actual_cost",
                    "0.70",
                    "execution record SHA-256",
                ),
                (
                    "disposition",
                    ExecutionDisposition.ACCEPTED.value,
                    (
                        "execution record SHA-256|persisted accepted "
                        "execution cost exceeds request budget"
                    ),
                ),
                (
                    "reason",
                    "forged rejected evidence reason",
                    "execution record SHA-256",
                ),
            ):
                raw = json.loads(original)
                execution = next(
                    item
                    for item in raw["executions"]
                    if item["execution_id"]
                    == "exec-rejected-tamper-2"
                )
                execution[field] = value
                rewrite_store_with_valid_state_hash(path, raw)
                with self.assertRaisesRegex(
                    ModelComputeRouterError,
                    expected_error,
                ):
                    self.router_store(path)

            raw = json.loads(original)
            execution = next(
                item
                for item in raw["executions"]
                if item["execution_id"]
                == "exec-rejected-tamper-2"
            )
            execution["prior_incurred_cost"] = "0"
            rewrite_execution_with_valid_record_hash(
                path, raw, execution
            )
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "prior incurred cost does not match durable history",
            ):
                self.router_store(path)

            raw = json.loads(original)
            execution = next(
                item
                for item in raw["executions"]
                if item["execution_id"]
                == "exec-rejected-stale-tamper"
            )
            execution["disposition"] = (
                ExecutionDisposition.ACCEPTED.value
            )
            execution["reason"] = (
                "execution identity, deadline, availability, "
                "freshness and actual cost are valid"
            )
            rewrite_execution_with_valid_record_hash(
                path, raw, execution
            )
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "disposition/reason is not reproducible",
            ):
                self.router_store(path)


    def test_separate_authority_recovers_self_consistent_store_tail_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = self.router_store(path)
            req = request(
                request_id="req-authority-tail-request",
                allow_cloud=False,
                cloud_candidate_id=None,
                max_cost=Decimal("2"),
            )
            self.store_route(store, 
                req,
                self.candidates,
                policy(),
                as_of=T1,
            )
            first = store.record_execution(
                execution_id="exec-authority-request-1",
                request_id=req.request_id,
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
            second = store.record_execution(
                execution_id="exec-authority-request-2",
                request_id=req.request_id,
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
                first.disposition,
                ExecutionDisposition.ACCEPTED,
            )
            self.assertEqual(
                second.disposition,
                ExecutionDisposition.REJECTED_COST,
            )
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["executions"] = [
                item
                for item in raw["executions"]
                if item["execution_id"]
                != "exec-authority-request-2"
            ]
            rewrite_execution_heads_from_surviving_history(
                path, raw
            )
            reopened = self.router_store(path)
            self.assertEqual(
                reopened.total_actual_cost(req.request_id),
                Decimal("2.05"),
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = self.router_store(path)
            req = request(
                request_id="req-authority-tail-cloud",
                max_cost=Decimal("20"),
            )
            route_policy = policy(
                max_cloud_cost=Decimal("10")
            )
            self.store_route(store, 
                req,
                self.candidates,
                route_policy,
                as_of=T1,
                voc_evidence=self.qualified_voc(
                    evidence_id="voc-authority-tail-cloud"
                ),
                domain_observation=slow_observation(),
            )
            first = store.record_execution(
                execution_id="exec-authority-cloud-1",
                request_id=req.request_id,
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
            second = store.record_execution(
                execution_id="exec-authority-cloud-2",
                request_id=req.request_id,
                completed_at=T1,
                available_at=T1,
                backend_id="permitted-cloud",
                model_id="challenger-v2",
                config_sha256=SHA_B,
                actual_cost=Decimal("4"),
                actual_latency_seconds=Decimal("4"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )
            self.assertEqual(
                first.disposition,
                ExecutionDisposition.ACCEPTED,
            )
            self.assertEqual(
                second.disposition,
                ExecutionDisposition.ACCEPTED,
            )
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["executions"] = [
                item
                for item in raw["executions"]
                if item["execution_id"]
                != "exec-authority-cloud-2"
            ]
            rewrite_execution_heads_from_surviving_history(
                path, raw
            )
            reopened = self.router_store(path)
            self.assertEqual(
                reopened.total_actual_cost(req.request_id),
                Decimal("10"),
            )


    def test_restart_freezes_loaded_request_but_preserves_idempotent_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = self.router_store(path)
            req = request(
                request_id="req-restart-freeze",
                allow_cloud=False,
                cloud_candidate_id=None,
                max_cost=Decimal("3"),
            )
            decision = self.store_route(store, 
                req,
                self.candidates,
                policy(),
                as_of=T1,
            )
            first = store.record_execution(
                execution_id="exec-restart-freeze-1",
                request_id=req.request_id,
                completed_at=T1,
                available_at=T1,
                backend_id="local-cpu",
                model_id="baseline-v1",
                config_sha256=SHA_A,
                actual_cost=Decimal("1"),
                actual_latency_seconds=Decimal("2"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )

            reopened = self.router_store(path)
            self.assertEqual(
                self.store_route(reopened, 
                    req,
                    self.candidates,
                    policy(),
                    as_of=T1,
                ),
                decision,
            )
            replayed = reopened.record_execution(
                execution_id=first.execution_id,
                request_id=req.request_id,
                completed_at=T1,
                available_at=T1,
                backend_id="local-cpu",
                model_id="baseline-v1",
                config_sha256=SHA_A,
                actual_cost=Decimal("1"),
                actual_latency_seconds=Decimal("2"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )
            self.assertEqual(replayed, first)
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "execution-frozen after restart",
            ):
                reopened.record_execution(
                    execution_id="exec-restart-freeze-2",
                    request_id=req.request_id,
                    completed_at=T1,
                    available_at=T1,
                    backend_id="local-cpu",
                    model_id="baseline-v1",
                    config_sha256=SHA_A,
                    actual_cost=Decimal("1"),
                    actual_latency_seconds=Decimal("2"),
                    evidence_sha256=SHA_C,
                    as_of=T1,
                )

            fresh = request(
                request_id="req-restart-fresh",
                allow_cloud=False,
                cloud_candidate_id=None,
                max_cost=Decimal("3"),
            )
            self.store_route(reopened, fresh, self.candidates, policy(), as_of=T1)
            accepted = reopened.record_execution(
                execution_id="exec-restart-fresh-1",
                request_id=fresh.request_id,
                completed_at=T1,
                available_at=T1,
                backend_id="local-cpu",
                model_id="baseline-v1",
                config_sha256=SHA_A,
                actual_cost=Decimal("1"),
                actual_latency_seconds=Decimal("2"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )
            self.assertEqual(
                accepted.disposition,
                ExecutionDisposition.ACCEPTED,
            )

    def test_joint_store_and_authority_rollback_cannot_refund_loaded_request(self):
        cases = (
            {
                "name": "request",
                "request": request(
                    request_id="req-joint-rollback-request",
                    allow_cloud=False,
                    cloud_candidate_id=None,
                    max_cost=Decimal("2"),
                ),
                "policy": policy(),
                "voc": None,
                "domain": None,
                "backend_id": "local-cpu",
                "model_id": "baseline-v1",
                "config_sha256": SHA_A,
                "first_cost": Decimal("1.25"),
                "second_cost": Decimal("0.80"),
                "retry_cost": Decimal("0.70"),
            },
            {
                "name": "cloud",
                "request": request(
                    request_id="req-joint-rollback-cloud",
                    max_cost=Decimal("20"),
                ),
                "policy": policy(max_cloud_cost=Decimal("10")),
                "voc": self.qualified_voc(evidence_id="voc-joint-rollback-cloud"),
                "domain": slow_observation(),
                "backend_id": "permitted-cloud",
                "model_id": "challenger-v2",
                "config_sha256": SHA_B,
                "first_cost": Decimal("6"),
                "second_cost": Decimal("4.01"),
                "retry_cost": Decimal("4"),
            },
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "router.json"
                    store = self.router_store(path)
                    self.store_route(store, 
                        case["request"],
                        self.candidates,
                        case["policy"],
                        as_of=T1,
                        voc_evidence=case["voc"],
                        domain_observation=case["domain"],
                    )
                    first = store.record_execution(
                        execution_id=f'exec-joint-{case["name"]}-1',
                        request_id=case["request"].request_id,
                        completed_at=T1,
                        available_at=T1,
                        backend_id=case["backend_id"],
                        model_id=case["model_id"],
                        config_sha256=case["config_sha256"],
                        actual_cost=case["first_cost"],
                        actual_latency_seconds=Decimal("2"),
                        evidence_sha256=SHA_C,
                        as_of=T1,
                    )
                    second = store.record_execution(
                        execution_id=f'exec-joint-{case["name"]}-2',
                        request_id=case["request"].request_id,
                        completed_at=T1,
                        available_at=T1,
                        backend_id=case["backend_id"],
                        model_id=case["model_id"],
                        config_sha256=case["config_sha256"],
                        actual_cost=case["second_cost"],
                        actual_latency_seconds=Decimal("2"),
                        evidence_sha256=SHA_C,
                        as_of=T1,
                    )
                    self.assertEqual(
                        first.disposition,
                        ExecutionDisposition.ACCEPTED,
                    )
                    self.assertEqual(
                        second.disposition,
                        ExecutionDisposition.REJECTED_COST,
                    )

                    raw = json.loads(path.read_text(encoding="utf-8"))
                    raw["executions"] = [
                        item
                        for item in raw["executions"]
                        if item["execution_id"] != second.execution_id
                    ]
                    rewrite_execution_heads_from_surviving_history(path, raw)
                    authority_path = path.with_name(
                        f"{path.name}.execution-authority.jsonl"
                    )
                    authority_lines = authority_path.read_text(
                        encoding="utf-8"
                    ).splitlines()
                    self.assertEqual(len(authority_lines), 2)
                    authority_path.write_text(
                        authority_lines[0] + "\n",
                        encoding="utf-8",
                    )

                    reopened = self.router_store(path)
                    self.assertEqual(
                        reopened.total_actual_cost(
                            case["request"].request_id
                        ),
                        case["first_cost"],
                    )
                    with self.assertRaisesRegex(
                        ModelComputeRouterError,
                        "execution-frozen after restart",
                    ):
                        reopened.record_execution(
                            execution_id=f'exec-joint-{case["name"]}-retry',
                            request_id=case["request"].request_id,
                            completed_at=T1,
                            available_at=T1,
                            backend_id=case["backend_id"],
                            model_id=case["model_id"],
                            config_sha256=case["config_sha256"],
                            actual_cost=case["retry_cost"],
                            actual_latency_seconds=Decimal("2"),
                            evidence_sha256=SHA_C,
                            as_of=T1,
                        )

    def test_journal_first_publication_recovers_one_missing_store_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = self.router_store(path)
            req = request(
                request_id="req-journal-first-recovery",
                allow_cloud=False,
                cloud_candidate_id=None,
                max_cost=Decimal("3"),
            )
            self.store_route(store, req, self.candidates, policy(), as_of=T1)
            original_persist = store._persist

            def interrupted_persist(*args, **kwargs):
                raise OSError("simulated interrupted routing-state publish")

            store._persist = interrupted_persist
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "reopen to recover",
            ):
                store.record_execution(
                    execution_id="exec-journal-first-recovery",
                    request_id=req.request_id,
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
                store.total_actual_cost(req.request_id),
                Decimal("1.25"),
            )
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "interrupted execution publication",
            ):
                store.record_execution(
                    execution_id="exec-journal-first-recovery-2",
                    request_id=req.request_id,
                    completed_at=T1,
                    available_at=T1,
                    backend_id="local-cpu",
                    model_id="baseline-v1",
                    config_sha256=SHA_A,
                    actual_cost=Decimal("0.25"),
                    actual_latency_seconds=Decimal("2"),
                    evidence_sha256=SHA_C,
                    as_of=T1,
                )
            store._persist = original_persist

            reopened = self.router_store(path)
            self.assertEqual(
                reopened.total_actual_cost(req.request_id),
                Decimal("1.25"),
            )
            recovered = reopened.record_execution(
                execution_id="exec-journal-first-recovery",
                request_id=req.request_id,
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
                recovered.disposition,
                ExecutionDisposition.ACCEPTED,
            )


    def test_authority_partial_tail_and_state_ahead_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = self.router_store(path)
            req = request(
                request_id="req-authority-corrupt-tail",
                allow_cloud=False,
                cloud_candidate_id=None,
            )
            self.store_route(store, req, self.candidates, policy(), as_of=T1)
            store.record_execution(
                execution_id="exec-authority-corrupt-tail",
                request_id=req.request_id,
                completed_at=T1,
                available_at=T1,
                backend_id="local-cpu",
                model_id="baseline-v1",
                config_sha256=SHA_A,
                actual_cost=Decimal("1"),
                actual_latency_seconds=Decimal("2"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )
            authority_path = path.with_name(
                f"{path.name}.execution-authority.jsonl"
            )
            original_authority = authority_path.read_text(
                encoding="utf-8"
            )

            authority_path.write_text(
                original_authority + "{",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "execution authority journal contains invalid JSON",
            ):
                self.router_store(path)

            authority_path.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "execution authority journal is missing",
            ):
                self.router_store(path)

    def test_immutable_request_id_cannot_be_reused_with_changed_policy_or_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.router_store(Path(tmp) / "router.json")
            self.store_route(store, 
                request(),
                self.candidates,
                policy(),
                as_of=T1,
                voc_evidence=self.qualified_voc(),
                domain_observation=slow_observation(),
            )
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "immutable request id",
            ):
                self.store_route(store, 
                    request(max_cost=Decimal("19")),
                    self.candidates,
                    policy(),
                    as_of=T1,
                    voc_evidence=self.qualified_voc(),
                    domain_observation=slow_observation(),
                )

    def test_execution_accounting_records_rejected_cost_but_never_accepts_late_stale_or_wrong_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = self.router_store(path)
            local_only = request(
                request_id="req-exec",
                allow_cloud=False,
            )
            decision = self.store_route(store, 
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
            self.store_route(store, 
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

            exact_deadline = store.record_execution(
                execution_id="exec-deadline-exact",
                request_id="req-exec",
                completed_at=T2,
                available_at=T3,
                backend_id="local-cpu",
                model_id="baseline-v1",
                config_sha256=SHA_A,
                actual_cost=Decimal("0"),
                actual_latency_seconds=Decimal("10"),
                evidence_sha256=SHA_C,
                as_of=T3,
            )
            self.assertEqual(
                exact_deadline.disposition,
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
            reopened = self.router_store(path)
            self.assertEqual(
                reopened.total_actual_cost("req-exec"),
                Decimal("7.00"),
            )

    def test_predecision_execution_is_rejected_but_cost_remains_accounted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = self.router_store(path)
            req = request(
                request_id="req-predecision-execution",
                allow_cloud=False,
            )
            decision = self.store_route(store, 
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

            reopened = self.router_store(path)
            self.assertEqual(
                reopened.total_actual_cost(req.request_id),
                Decimal("0.50"),
            )

    def test_actual_execution_cost_overruns_fail_closed_and_remain_accounted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = self.router_store(path)

            local_request = request(
                request_id="req-actual-request-budget",
                allow_cloud=False,
                max_cost=Decimal("2"),
            )
            local_decision = self.store_route(store, 
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
            cloud_decision = self.store_route(store, 
                cloud_request,
                self.candidates,
                cloud_policy,
                as_of=T1,
                voc_evidence=self.qualified_voc(evidence_id="voc-actual-cloud-budget"),
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

            reopened = self.router_store(path)
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
            store = self.router_store(path)

            local_request = request(
                request_id="req-cumulative-request-budget",
                allow_cloud=False,
                max_cost=Decimal("2"),
            )
            self.store_route(store, 
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
            self.store_route(store, 
                cloud_request,
                self.candidates,
                cloud_policy,
                as_of=T1,
                voc_evidence=self.qualified_voc(
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

            reopened = self.router_store(path)
            self.assertEqual(
                reopened.total_actual_cost(local_request.request_id),
                Decimal("2.05"),
            )
            self.assertEqual(
                reopened.total_actual_cost(cloud_request.request_id),
                Decimal("10.01"),
            )
            for execution_id, request_id, backend_id, model_id, config_sha256 in (
                (
                    "exec-cumulative-request-after-restart",
                    local_request.request_id,
                    "local-cpu",
                    "baseline-v1",
                    SHA_A,
                ),
                (
                    "exec-cumulative-cloud-after-restart",
                    cloud_request.request_id,
                    "permitted-cloud",
                    "challenger-v2",
                    SHA_B,
                ),
            ):
                with self.assertRaisesRegex(
                    ModelComputeRouterError,
                    "execution-frozen after restart",
                ):
                    reopened.record_execution(
                        execution_id=execution_id,
                        request_id=request_id,
                        completed_at=T1,
                        available_at=T1,
                        backend_id=backend_id,
                        model_id=model_id,
                        config_sha256=config_sha256,
                        actual_cost=Decimal("0.01"),
                        actual_latency_seconds=Decimal("2"),
                        evidence_sha256=SHA_C,
                        as_of=T1,
                    )
            self.assertEqual(
                reopened.total_actual_cost(local_request.request_id),
                Decimal("2.05"),
            )
            self.assertEqual(
                reopened.total_actual_cost(cloud_request.request_id),
                Decimal("10.01"),
            )

    def test_restart_rejects_rehashed_execution_tail_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = self.router_store(path)

            local_request = request(
                request_id="req-truncated-rejected-tail",
                allow_cloud=False,
                max_cost=Decimal("2"),
            )
            self.store_route(store, 
                local_request,
                self.candidates,
                policy(),
                as_of=T1,
            )
            store.record_execution(
                execution_id="exec-truncated-local-accepted",
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
            rejected_tail = store.record_execution(
                execution_id="exec-truncated-local-rejected",
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
                rejected_tail.disposition,
                ExecutionDisposition.REJECTED_COST,
            )

            cloud_request = request(
                request_id="req-truncated-cloud-tail",
                max_cost=Decimal("20"),
            )
            self.store_route(store, 
                cloud_request,
                self.candidates,
                policy(max_cloud_cost=Decimal("10")),
                as_of=T1,
                voc_evidence=self.qualified_voc(
                    evidence_id="voc-truncated-cloud-tail"
                ),
                domain_observation=slow_observation(),
            )
            store.record_execution(
                execution_id="exec-truncated-cloud-first",
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
            accepted_cloud_tail = store.record_execution(
                execution_id="exec-truncated-cloud-accepted-tail",
                request_id=cloud_request.request_id,
                completed_at=T1,
                available_at=T1,
                backend_id="permitted-cloud",
                model_id="challenger-v2",
                config_sha256=SHA_B,
                actual_cost=Decimal("2"),
                actual_latency_seconds=Decimal("4"),
                evidence_sha256=SHA_C,
                as_of=T1,
            )
            self.assertEqual(
                accepted_cloud_tail.disposition,
                ExecutionDisposition.ACCEPTED,
            )
            original = path.read_text(encoding="utf-8")

            for execution_id in (
                "exec-truncated-local-rejected",
                "exec-truncated-cloud-accepted-tail",
            ):
                raw = json.loads(original)
                raw["executions"] = [
                    item
                    for item in raw["executions"]
                    if item["execution_id"] != execution_id
                ]
                rewrite_store_with_valid_state_hash(path, raw)
                with self.assertRaisesRegex(
                    ModelComputeRouterError,
                    "execution authority (?:prefix does not match routing state|"
                    "recovery prior head mismatch)",
                ):
                    self.router_store(path)

    def test_restart_rejects_malformed_execution_head_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.json"
            store = self.router_store(path)
            self.store_route(store, 
                request(request_id="req-malformed-execution-head"),
                self.candidates,
                policy(),
                as_of=T1,
            )
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["execution_heads"][0][
                "cumulative_incurred_cost"
            ] = {"not": "a decimal"}
            rewrite_store_with_valid_state_hash(path, raw)

            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "cumulative_incurred_cost must be a non-negative Decimal",
            ):
                self.router_store(path)

    def test_future_voc_is_not_causally_usable(self):
        future = self.qualified_voc(
            evidence_id="voc-future",
            measured_at=T2,
            available_at=T2,
        )
        decision = self.route_compute(
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
        decision = self.route_compute(
            request(),
            self.candidates,
            policy(),
            as_of=T1,
            voc_evidence=self.qualified_voc(),
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
        estimated = self.route_compute(
            request(request_id="req-estimated"),
            (self.local, expensive_cloud),
            policy(max_cloud_cost=Decimal("10")),
            as_of=T1,
            voc_evidence=self.qualified_voc(evidence_id="voc-estimated"),
            domain_observation=slow_observation(),
        )
        self.assertEqual(estimated.tier, ComputeTier.LOCAL)

        affordable_cloud = replace(
            self.cloud,
            estimated_cost=Decimal("3"),
        )
        measured_request = self.route_compute(
            request(
                request_id="req-measured-request",
                max_cost=Decimal("4"),
            ),
            (self.local, affordable_cloud),
            policy(max_cloud_cost=Decimal("10")),
            as_of=T1,
            voc_evidence=self.qualified_voc(
                evidence_id="voc-measured-request",
                measured_compute_cost=Decimal("5"),
            ),
            domain_observation=slow_observation(),
        )
        self.assertEqual(measured_request.tier, ComputeTier.LOCAL)
        self.assertIn("request budget", measured_request.reason)

        measured = self.route_compute(
            request(request_id="req-measured"),
            self.candidates,
            policy(max_cloud_cost=Decimal("10")),
            as_of=T1,
            voc_evidence=self.qualified_voc(
                evidence_id="voc-measured",
                measured_compute_cost=Decimal("11"),
            ),
            domain_observation=slow_observation(),
        )
        self.assertEqual(measured.tier, ComputeTier.LOCAL)


if __name__ == "__main__":
    unittest.main()
