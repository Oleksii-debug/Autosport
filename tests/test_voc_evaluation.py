import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

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
from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.voc_evaluation import (
    CanonicalVOCAuthorityResolver,
    OutcomeDerivedVOCScore,
    PairedVOCEvaluation,
    VOCEvaluationError,
    VOCEvaluationProvenance,
    VOCEvaluationStore,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64
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
        urgency_id="routine",
        contradiction_state="none",
        baseline_candidate_id="local",
        baseline_backend_id="local-cpu",
        baseline_model_id="baseline-v1",
        baseline_config_sha256=SHA_A,
        challenger_candidate_id="cloud",
        challenger_backend_id="cloud-backend",
        challenger_model_id="challenger-v2",
        challenger_config_sha256=SHA_B,
        decision_input_sha256=SHA_E,
        decision_context_sha256=hashlib.sha256(
            b"default-source-voc-context"
        ).hexdigest(),
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




class FixtureCanonicalAuthorityResolver:
    """Small independent stand-in for durable Decision/Scientific/Outcome authorities."""

    def __init__(self):
        self._records = {}
        self._contexts = {}

    def register(self, value):
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

    def resolve(self, evaluation, *, as_of):
        canonical = self._records.get(evaluation.evaluation_id)
        if canonical is None or canonical.payload() != evaluation.payload():
            return None
        if canonical.evaluated_at > as_of:
            return None
        return canonical

    def resolve_decision_context(self, context_sha256, *, as_of):
        value = self._contexts.get(context_sha256)
        return None if value is None else dict(value)

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
        created_at=T2,
        decision_deadline=T4,
        required_capability="forecast",
        data_classification=DataClassification.PUBLIC,
        allow_cloud=True,
        max_cost=Decimal("10"),
        response_ttl_seconds=Decimal("30"),
        baseline_candidate_id="local",
        cloud_candidate_id="cloud",
        decision_input_sha256=SHA_F,
        decision_evidence_sha256=SHA_E,
        voc_regime_id="regime-1",
        voc_urgency_id="routine",
        voc_contradiction_state="none",
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
        return value

    def resolve_decision_context(self, context_sha256, *, as_of):
        value = self._contexts.get(context_sha256)
        return None if value is None else dict(value)


class _FixtureOutcomeScoreAuthority:
    """Test authority whose records are independent from routed evaluations."""

    def __init__(self):
        self._records = {}

    def publish(self, value):
        self._records[value.evaluation_id] = value

    def resolve(self, evaluation_id, *, as_of):
        value = self._records.get(evaluation_id)
        if value is None or value.available_at > as_of:
            return None
        return value


def outcome_score(value):
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
        incremental_value_interval_low=value.incremental_value_interval_low,
        incremental_value_interval_high=value.incremental_value_interval_high,
        source_artifact_sha256=hashlib.sha256(
            ("outcome-score:" + value.evaluation_id).encode("utf-8")
        ).hexdigest(),
    )


class PairedVOCEvaluationTests(unittest.TestCase):
    def _production_resolver_fixture(
        self,
        value=None,
        *,
        decision_recorded_at=T1,
    ):
        paired = evaluation() if value is None else value

        source_context = {
            "request_id": f"source:{paired.evaluation_id}",
            "decision_input_sha256": paired.decision_input_sha256,
            "task_class": paired.task_class,
            "sport_id": paired.sport_id,
            "league_id": paired.league_id,
            "regime_id": paired.regime_id,
            "urgency_id": paired.urgency_id,
            "contradiction_state": paired.contradiction_state,
        }
        source_context_record = DecisionRecord(
            replay_run_id="replay-voc-context",
            agent="voc-test",
            observed_ts=T0,
            action="VOC_ROUTE_CONTEXT",
            payload={"voc_current_context": source_context},
            context_hash=SHA_A,
            decision_id="decision-voc-context",
            recorded_at=T0,
        )
        ledger = JsonlDecisionLedger(
            Path(self._router_tmp.name) / "canonical-decision-ledger.jsonl"
        )
        context_digest = ledger.append(source_context_record)

        decision_payload = {
            "voc_binding": {
                "decision_input_sha256": paired.decision_input_sha256,
                "decision_context_sha256": context_digest,
                "baseline_candidate_id": paired.baseline_candidate_id,
                "baseline_backend_id": paired.baseline_backend_id,
                "baseline_model_id": paired.baseline_model_id,
                "baseline_config_sha256": paired.baseline_config_sha256,
                "baseline_output_sha256": paired.baseline_output_sha256,
                "baseline_action": paired.baseline_action,
                "baseline_abstained": paired.baseline_abstained,
                "challenger_candidate_id": paired.challenger_candidate_id,
                "challenger_backend_id": paired.challenger_backend_id,
                "challenger_model_id": paired.challenger_model_id,
                "challenger_config_sha256": paired.challenger_config_sha256,
                "challenger_output_sha256": paired.challenger_output_sha256,
                "challenger_action": paired.challenger_action,
                "challenger_abstained": paired.challenger_abstained,
                "sport_id": paired.sport_id,
                "league_id": paired.league_id,
                "regime_id": paired.regime_id,
                "urgency_id": paired.urgency_id,
                "contradiction_state": paired.contradiction_state,
            }
        }
        decision_record = DecisionRecord(
            replay_run_id="replay-voc",
            agent="voc-test",
            observed_ts=T0,
            action=paired.baseline_action,
            payload=decision_payload,
            context_hash=SHA_A,
            decision_id="decision-voc",
            recorded_at=decision_recorded_at,
        )
        decision_digest = ledger.append(decision_record)

        scoring_payload = {"kind": "net-utility-v1", "metric": "incremental_value"}
        scoring_digest = hashlib.sha256(
            json.dumps(
                scoring_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        multiple_control = "bonferroni-v1"
        multiple_control_digest = hashlib.sha256(multiple_control.encode("utf-8")).hexdigest()
        dataset_manifest = "e" * 64
        source_identity = "source-voc"
        license_identity = "license-voc"
        trial_family = "confirmation-family-v1"
        holdout_digest = hashlib.sha256(
            json.dumps(
                {
                    "schema_version": 1,
                    "research_protocol_id": paired.research_protocol_id,
                    "dataset_manifest_sha256": dataset_manifest,
                    "source_identity": source_identity,
                    "license_identity": license_identity,
                    "confirmation_trial_family_id": trial_family,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        dataset_snapshot_id = "voc-dataset-1"
        evaluator_source_sha256 = SHA_C
        cohort_id = "voc-cohort-1"
        uncertainty_method = "bootstrap-v1"
        design = {
            "task_class": paired.task_class,
            "estimand": "incremental_net_voc",
            "cohort_id": cohort_id,
            "evaluator_source_sha256": evaluator_source_sha256,
            "scope": {
                "sport_id": paired.sport_id,
                "league_id": paired.league_id,
                "regime_id": paired.regime_id,
                "urgency_id": paired.urgency_id,
                "contradiction_state": paired.contradiction_state,
            },
            "outcome_identity": {
                "event_id": "event-voc",
                "market_id": "match-voc",
                "source_id": "provider-voc",
                "market_type": "WINNER",
                "sport_id": paired.sport_id,
                "league_id": paired.league_id,
                "regime_id": paired.regime_id,
            },
            "scoring_rule": {
                "id": paired.scoring_rule_id,
                "payload": scoring_payload,
            },
            "confirmation_trial_family_id": trial_family,
        }
        protocol_payload = {
            "research_protocol_id": paired.research_protocol_id,
            "protocol_sha256": paired.research_protocol_sha256,
            "binding": {
                "evaluation_design": json.dumps(
                    design,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "uncertainty_method": uncertainty_method,
                "multiple_comparison_control": multiple_control,
            },
            "dataset_manifest_sha256": dataset_manifest,
        }
        protocol_entry = SimpleNamespace(
            available_at=T0,
            payload=protocol_payload,
        )
        snapshot_entry = SimpleNamespace(
            available_at=T0,
            payload={
                "dataset_snapshot_id": dataset_snapshot_id,
                "manifest_sha256": dataset_manifest,
                "source_identity": source_identity,
                "license_identity": license_identity,
            },
        )
        identity = SimpleNamespace(
            sport=paired.sport_id,
            event_id="event-voc",
            market_id="match-voc",
            source_id="provider-voc",
            market_type=SimpleNamespace(value="WINNER"),
        )
        outcome_authority = SimpleNamespace(
            identity=identity,
            authority_sha256=SHA_D,
            assert_available_as_of=lambda _: None,
        )
        def registry_get(record_type, record_id):
            if (
                record_type == "PairedVOCEvaluation"
                and record_id == paired.evaluation_id
            ):
                return SimpleNamespace(
                    available_at=paired.evaluated_at,
                    payload=paired.payload(),
                )
            if (
                record_type == "ResearchProtocol"
                and record_id == paired.research_protocol_id
            ):
                return protocol_entry
            if (
                record_type == "DatasetSnapshot"
                and record_id == dataset_snapshot_id
            ):
                return snapshot_entry
            if record_type == "EvaluationBundle" and record_id == "voc-bundle-1":
                return SimpleNamespace(
                    available_at=paired.evaluated_at,
                    payload={
                        "evaluation_bundle_id": "voc-bundle-1",
                        "bundle_sha256": SHA_A,
                        "evaluator_source_sha256": evaluator_source_sha256,
                        "dataset_snapshot_id": dataset_snapshot_id,
                        "protocol_sha256": paired.research_protocol_sha256,
                        "artifact_hashes": [
                            score_authority.resolve(
                                paired.evaluation_id,
                                as_of=paired.evaluated_at,
                            ).score_sha256
                        ],
                        "effective_sample_size": paired.effective_sample_size,
                        "effect_interval_low": str(
                            paired.incremental_value_interval_low
                        ),
                        "effect_interval_high": str(
                            paired.incremental_value_interval_high
                        ),
                        "practical_improvement": str(paired.net_value),
                    },
                )
            return None

        def registry_causal_records(record_type, *, as_of):
            if record_type == "DatasetSnapshot":
                return (snapshot_entry,)
            if record_type == "PromotionEvidence":
                return (
                    SimpleNamespace(
                        available_at=paired.evaluated_at,
                        payload={
                            "research_protocol_id": paired.research_protocol_id,
                            "holdout_access_id": paired.holdout_access_id,
                            "multiple_comparison_control_sha256": (
                                paired.multiple_comparison_control_sha256
                            ),
                            "confirmation_trial_family_id": trial_family,
                            "estimand": "incremental_net_voc",
                            "cohort_id": cohort_id,
                            "uncertainty_method": uncertainty_method,
                            "validity": "ELIGIBLE",
                            "guardrails_passed": True,
                            "holdout_consumed": True,
                            "effective_sample_size": paired.effective_sample_size,
                            "minimum_effective_sample_size": 8,
                            "effect_interval_low": str(
                                paired.incremental_value_interval_low
                            ),
                            "effect_interval_high": str(
                                paired.incremental_value_interval_high
                            ),
                            "practical_improvement": str(paired.net_value),
                            "evaluation_bundle_id": "voc-bundle-1",
                            "evaluation_bundle_sha256": SHA_A,
                            "dataset_snapshot_id": dataset_snapshot_id,
                        },
                    ),
                )
            return ()

        registry = SimpleNamespace(
            get=registry_get,
            causal_records=registry_causal_records,
        )
        resolver = object.__new__(CanonicalVOCAuthorityResolver)
        resolver.decision_ledger = ledger
        resolver.scientific_registry = registry
        resolver.outcome_authority = outcome_authority
        paired = replace(
            paired,
            decision_context_sha256=context_digest,
            decision_evidence_sha256=decision_digest,
            scoring_rule_sha256=scoring_digest,
            multiple_comparison_control_sha256=multiple_control_digest,
            holdout_access_id=holdout_digest,
            outcome_evidence_sha256=SHA_D,
        )
        score_authority = _FixtureOutcomeScoreAuthority()
        score_authority.publish(outcome_score(paired))
        resolver.outcome_score_authority = score_authority
        return resolver, paired

    def test_production_resolver_binds_decision_protocol_and_outcome_scope(self):
        resolver, paired = self._production_resolver_fixture()
        self.assertEqual(resolver.resolve(paired, as_of=T2), paired)

    def test_production_resolver_rejects_binding_before_paired_outputs(self):
        resolver, paired = self._production_resolver_fixture(
            decision_recorded_at=T0,
        )
        with self.assertRaisesRegex(
            VOCEvaluationError,
            "predates paired candidate outputs",
        ):
            resolver.resolve(paired, as_of=T2)

    def test_production_resolver_rejects_binding_at_outcome_reveal(self):
        resolver, paired = self._production_resolver_fixture(
            decision_recorded_at=T2,
        )
        with self.assertRaisesRegex(
            VOCEvaluationError,
            "not durably recorded before outcome reveal",
        ):
            resolver.resolve(paired, as_of=T2)

    def test_production_resolver_rejects_unbound_utility_statistics(self):
        resolver, paired = self._production_resolver_fixture()
        forged = replace(
            paired,
            baseline_utility=Decimal("1.1"),
        )
        canonical_registry = resolver.scientific_registry

        def forged_get(record_type, record_id):
            if (
                record_type == "PairedVOCEvaluation"
                and record_id == forged.evaluation_id
            ):
                return SimpleNamespace(
                    available_at=forged.evaluated_at,
                    payload=forged.payload(),
                )
            return canonical_registry.get(record_type, record_id)

        resolver.scientific_registry = SimpleNamespace(
            get=forged_get,
            causal_records=canonical_registry.causal_records,
        )
        with self.assertRaisesRegex(
            VOCEvaluationError,
            "canonical outcome-derived VOC score",
        ):
            resolver.resolve(forged, as_of=T2)

    def test_forged_registry_statistics_cannot_mint_outcome_score_authority(self):
        resolver, paired = self._production_resolver_fixture()
        forged = replace(
            paired,
            baseline_utility=Decimal("0.5"),
            challenger_utility=Decimal("3"),
            incremental_value_interval_low=Decimal("1.8"),
            incremental_value_interval_high=Decimal("2.7"),
        )
        canonical_registry = resolver.scientific_registry
        canonical_score = resolver.outcome_score_authority.resolve(
            paired.evaluation_id,
            as_of=T2,
        )

        def forged_get(record_type, record_id):
            if (
                record_type == "PairedVOCEvaluation"
                and record_id == forged.evaluation_id
            ):
                return SimpleNamespace(
                    available_at=forged.evaluated_at,
                    payload=forged.payload(),
                )
            if record_type == "EvaluationBundle" and record_id == "voc-bundle-1":
                original = canonical_registry.get(record_type, record_id)
                payload = dict(original.payload)
                payload.update(
                    {
                        "effective_sample_size": forged.effective_sample_size,
                        "effect_interval_low": str(
                            forged.incremental_value_interval_low
                        ),
                        "effect_interval_high": str(
                            forged.incremental_value_interval_high
                        ),
                        "practical_improvement": str(forged.net_value),
                        "artifact_hashes": [canonical_score.score_sha256],
                    }
                )
                return SimpleNamespace(
                    available_at=forged.evaluated_at,
                    payload=payload,
                )
            return canonical_registry.get(record_type, record_id)

        def forged_causal_records(record_type, *, as_of):
            if record_type != "PromotionEvidence":
                return canonical_registry.causal_records(record_type, as_of=as_of)
            original = canonical_registry.causal_records(
                record_type,
                as_of=as_of,
            )[0]
            payload = dict(original.payload)
            payload.update(
                {
                    "effective_sample_size": forged.effective_sample_size,
                    "effect_interval_low": str(
                        forged.incremental_value_interval_low
                    ),
                    "effect_interval_high": str(
                        forged.incremental_value_interval_high
                    ),
                    "practical_improvement": str(forged.net_value),
                }
            )
            return (
                SimpleNamespace(
                    available_at=forged.evaluated_at,
                    payload=payload,
                ),
            )

        resolver.scientific_registry = SimpleNamespace(
            get=forged_get,
            causal_records=forged_causal_records,
        )
        with self.assertRaisesRegex(
            VOCEvaluationError,
            "canonical outcome-derived VOC score",
        ):
            resolver.resolve(forged, as_of=T2)

    def test_production_resolver_rejects_mismatched_decision_binding(self):
        resolver, paired = self._production_resolver_fixture()
        records = resolver.decision_ledger.verified_records()
        source_context_record = next(
            item for item in records if item.action == "VOC_ROUTE_CONTEXT"
        )
        record = next(
            item for item in records if "voc_binding" in item.payload
        )
        binding = dict(record.payload["voc_binding"])
        binding["challenger_action"] = "WRONG"
        wrong_payload = dict(record.payload)
        wrong_payload["voc_binding"] = binding
        wrong_record = DecisionRecord(
            replay_run_id=record.replay_run_id,
            agent=record.agent,
            observed_ts=record.observed_ts,
            action=record.action,
            payload=wrong_payload,
            context_hash=record.context_hash,
            decision_id=record.decision_id,
            recorded_at=record.recorded_at,
        )
        wrong_ledger = JsonlDecisionLedger(
            Path(self._router_tmp.name) / "wrong-decision-ledger.jsonl"
        )
        wrong_ledger.append(source_context_record)
        wrong_ledger.append(wrong_record)
        resolver.decision_ledger = wrong_ledger
        with self.assertRaisesRegex(
            VOCEvaluationError,
            "canonical DecisionLedger",
        ):
            resolver.resolve(paired, as_of=T2)

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

    def canonical_request(self, value, observation, *, context_overrides=None):
        context = {
            "request_id": value.request_id,
            "decision_input_sha256": value.decision_input_sha256,
            "task_class": value.required_capability,
            "sport_id": observation.sport_id,
            "league_id": observation.league_id,
            "regime_id": value.voc_regime_id,
            "urgency_id": value.voc_urgency_id,
            "contradiction_state": value.voc_contradiction_state,
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
                value = self.canonical_request(value, observation)
                args = (value, *args[1:])
        return route_compute(*args, **kwargs)

    def test_cloud_requires_independent_canonical_authority(self):
        store = VOCEvaluationStore(
            Path(self._router_tmp.name) / "no-authority.json"
        )
        evidence = voc()
        store.record(evidence.evaluation)
        decision = route_compute(
            request(),
            candidates(),
            policy(),
            as_of=T2,
            voc_evidence=evidence,
            voc_evaluation_store=store,
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertIn("missing canonical VOC authority resolver", decision.reason)

    def test_qualified_paired_evaluation_can_authorize_cloud(self):
        resolver, paired = self._production_resolver_fixture()
        production_store = VOCEvaluationStore(
            Path(self._router_tmp.name) / "production-voc.json",
            canonical_authority_resolver=resolver,
        )
        production_store.record(paired)
        production_request = request()
        production_context = {
            "request_id": production_request.request_id,
            "decision_input_sha256": production_request.decision_input_sha256,
            "task_class": production_request.required_capability,
            "sport_id": "table-tennis",
            "league_id": "league-1",
            "regime_id": production_request.voc_regime_id,
            "urgency_id": production_request.voc_urgency_id,
            "contradiction_state": production_request.voc_contradiction_state,
        }
        production_context_record = DecisionRecord(
            replay_run_id="replay-current-voc",
            agent="voc-test",
            observed_ts=T2,
            action="VOC_ROUTE_CONTEXT",
            payload={"voc_current_context": production_context},
            context_hash=SHA_A,
            decision_id="decision-current-voc",
            recorded_at=T2,
        )
        current_context_digest = resolver.decision_ledger.append(
            production_context_record
        )
        production_request = replace(
            production_request,
            decision_evidence_sha256=current_context_digest,
        )
        decision = route_compute(
            production_request,
            candidates(),
            policy(),
            as_of=T2,
            voc_evidence=voc(paired),
            voc_evaluation_store=production_store,
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.CLOUD)

    def test_historical_voc_cannot_authorize_its_source_decision(self):
        paired = evaluation()
        evidence = self.qualified_voc(paired)
        source_request = replace(
            request(),
            request_id="req-replayed-source-input",
            decision_input_sha256=paired.decision_input_sha256,
        )
        decision = self.route_compute(
            source_request,
            candidates(),
            policy(),
            as_of=T2,
            voc_evidence=evidence,
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertIn("cannot authorize its source decision", decision.reason)

    def test_voc_routing_stratum_mismatch_fails_closed(self):
        paired = evaluation()
        evidence = self.qualified_voc(paired)
        mismatches = {
            "voc_regime_id": "different-regime",
            "voc_urgency_id": "urgent",
            "voc_contradiction_state": "contradicted",
        }
        for field, value in mismatches.items():
            with self.subTest(field=field):
                decision = self.route_compute(
                    replace(
                        request(),
                        request_id=f"req-mismatch-{field}",
                        **{field: value},
                    ),
                    candidates(),
                    policy(),
                    as_of=T2,
                    voc_evidence=evidence,
                    domain_observation=slow_observation(),
                )
                self.assertEqual(decision.tier, ComputeTier.LOCAL)
                self.assertIn("routing stratum", decision.reason)

        task_candidates = tuple(
            replace(
                candidate,
                capabilities=("forecast", "different-task"),
            )
            for candidate in candidates()
        )
        task_mismatch = self.route_compute(
            replace(
                request(),
                request_id="req-mismatch-task-class",
                required_capability="different-task",
            ),
            task_candidates,
            policy(),
            as_of=T2,
            voc_evidence=evidence,
            domain_observation=slow_observation(),
        )
        self.assertEqual(task_mismatch.tier, ComputeTier.LOCAL)
        self.assertIn("routing stratum", task_mismatch.reason)

        sport_mismatch = self.route_compute(
            request(),
            candidates(),
            policy(),
            as_of=T2,
            voc_evidence=evidence,
            domain_observation=replace(
                slow_observation(),
                sport_id="different-sport",
            ),
        )
        self.assertEqual(sport_mismatch.tier, ComputeTier.LOCAL)
        self.assertIn("routing stratum", sport_mismatch.reason)

        league_mismatch = self.route_compute(
            request(),
            candidates(),
            policy(),
            as_of=T2,
            voc_evidence=evidence,
            domain_observation=replace(
                slow_observation(),
                league_id="different-league",
            ),
        )
        self.assertEqual(league_mismatch.tier, ComputeTier.LOCAL)
        self.assertIn("routing stratum", league_mismatch.reason)

    def test_voc_history_must_exist_before_current_request_begins(self):
        paired = evaluation()
        evidence = self.qualified_voc(paired)
        decision = self.route_compute(
            replace(
                request(),
                request_id="req-predates-voc",
                created_at=T1,
            ),
            candidates(),
            policy(),
            as_of=T2,
            voc_evidence=evidence,
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertIn("not available when the current request began", decision.reason)

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

    def test_self_consistent_fake_evaluation_cannot_mint_cloud_authority(self):
        canonical = evaluation()
        self._canonical_voc.publish(canonical)
        forged = evaluation(
            evaluation_id="forged-voc",
            decision_evidence_sha256=SHA_D,
            challenger_output_sha256=SHA_C,
        )
        evidence = voc(forged)
        self.voc_store.record(forged)
        decision = self.route_compute(
            request(),
            candidates(),
            policy(),
            as_of=T2,
            voc_evidence=evidence,
            domain_observation=slow_observation(),
        )
        self.assertEqual(decision.tier, ComputeTier.LOCAL)
        self.assertIn("canonical VOC authority could not resolve", decision.reason)

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
            resolver = _FixtureCanonicalVOCResolver()
            store = VOCEvaluationStore(path, canonical_authority_resolver=resolver)
            positive = evaluation(evaluation_id="positive")
            negative = evaluation(
                evaluation_id="negative",
                challenger_utility=Decimal("0.5"),
                compute_cost_penalty=Decimal("0.2"),
                latency_opportunity_cost_penalty=Decimal("0.1"),
                incremental_value_interval_low=Decimal("-1"),
                incremental_value_interval_high=Decimal("-0.2"),
            )
            resolver.publish(positive)
            resolver.publish(negative)
            store.record(positive)
            store.record(negative)

            reopened = VOCEvaluationStore(path, canonical_authority_resolver=resolver)
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

    def test_store_schema_version_tracks_required_decision_context_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voc.json"
            VOCEvaluationStore(path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["version"], 3)

            raw["version"] = 2
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(
                VOCEvaluationError,
                "version mismatch",
            ):
                VOCEvaluationStore(path)

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
