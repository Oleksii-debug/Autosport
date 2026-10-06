from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import dataclass, replace
from unittest.mock import patch
from decimal import Decimal, ROUND_DOWN, localcontext
from pathlib import Path
from typing import Any

from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.model_compute_router import (
    ComputeCandidate,
    ComputeRouteRequest,
    ComputeRoutingPolicy,
    ComputeTier,
    DataClassification,
    ModelComputeRouterStore,
)
from autosport.market_outcomes import (
    OutcomeAuthorityStatus,
    assess_betfair_historical_market_definition_authority,
)
from autosport.scientific_registry import ScientificRegistry
from autosport.voc_evaluation import (
    PairedVOCEvaluation,
    VOCEvaluationError,
    VOCEvaluationProvenance,
    VOCEvaluationStore,
)
from autosport.voc_outcome_scoring import (
    CanonicalOutcomeDerivedVOCScoreAuthority,
    CanonicalVOCOutcomeSource,
    append_paired_voc_admission,
    build_canonical_voc_authority_resolver,
)


T_PROTOCOL = "2026-09-19T23:58:00Z"
T_AUTHORITY = "2026-09-19T23:59:00Z"
T_DECISION = "2026-09-20T00:00:00Z"
T_BASELINE = "2026-09-20T00:00:10Z"
T_CHALLENGER = "2026-09-20T00:00:11Z"
T_BINDING = "2026-09-20T00:00:12Z"
T_DEADLINE = "2026-09-20T00:00:30Z"
T_REVEAL = "2026-09-20T00:01:00Z"
T_EVALUATED = "2026-09-20T00:01:01Z"
T_AS_OF = "2026-09-20T00:01:02Z"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class RawScientificRecord:
    record_type: str
    record_id: str
    available_at: str
    payload: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return self.payload


class CanonicalOutcomeDerivedVOCScoreAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = JsonlDecisionLedger(self.root / "decision-ledger.jsonl")
        self.registry = ScientificRegistry.initialize_pristine(
            self.root / "scientific-registry.json"
        )

        assessment = assess_betfair_historical_market_definition_authority(
            market_id="1.23456789",
            market_definition={
                "eventId": "event-voc",
                "eventTypeId": "2593174",
                "marketType": "MATCH_ODDS",
                "status": "OPEN",
                "runners": [{"id": "101"}, {"id": "202"}],
            },
            provider_publish_at=T_AUTHORITY,
            observed_at=T_AUTHORITY,
        )
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE)
        self.assertIsNotNone(assessment.authority)
        self.outcome_authority = assessment.authority
        assert self.outcome_authority is not None

        self.quote_keys = self.outcome_authority.quote_keys
        self.outcome_record = {
            "schema_version": 2,
            "source": self.outcome_authority.identity.source_id,
            "record_id": "voc-outcome-event-voc",
            "revision_id": "voc-outcome-event-voc-r1",
            "revision": 1,
            "revision_kind": "initial",
            "recorded_at": T_REVEAL,
            "quote_outcomes": {
                self.quote_keys[0]: "loss",
                self.quote_keys[1]: "win",
            },
        }
        outcome_bytes = json.dumps(
            self.outcome_record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.outcome_file = "voc-outcome.json"
        (self.root / self.outcome_file).write_bytes(outcome_bytes)
        self.outcome_sha256 = hashlib.sha256(outcome_bytes).hexdigest()

        second_assessment = assess_betfair_historical_market_definition_authority(
            market_id="1.23456790",
            market_definition={
                "eventId": "event-voc-2",
                "eventTypeId": "2593174",
                "marketType": "MATCH_ODDS",
                "status": "OPEN",
                "runners": [{"id": "303"}, {"id": "404"}],
            },
            provider_publish_at=T_AUTHORITY,
            observed_at=T_AUTHORITY,
        )
        self.assertEqual(
            second_assessment.status,
            OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
        )
        self.assertIsNotNone(second_assessment.authority)
        self.second_outcome_authority = second_assessment.authority
        assert self.second_outcome_authority is not None
        second_quote_keys = self.second_outcome_authority.quote_keys
        second_record = {
            "schema_version": 2,
            "source": self.second_outcome_authority.identity.source_id,
            "record_id": "voc-outcome-event-voc-2",
            "revision_id": "voc-outcome-event-voc-2-r1",
            "revision": 1,
            "revision_kind": "initial",
            "recorded_at": T_REVEAL,
            "quote_outcomes": {
                second_quote_keys[0]: "loss",
                second_quote_keys[1]: "win",
            },
        }
        second_bytes = json.dumps(
            second_record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.second_outcome_file = "voc-outcome-2.json"
        (self.root / self.second_outcome_file).write_bytes(second_bytes)
        self.second_outcome_sha256 = hashlib.sha256(second_bytes).hexdigest()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _scoring_rule(self) -> dict[str, Any]:
        return {
            "kind": "paired-realized-utility-v1",
            "metric": "incremental_value",
            "utility_by_outcome": {
                "win": {"BASE": "0", "CLOUD": "2"},
                "loss": {"BASE": "0", "CLOUD": "1"},
                "void": {"BASE": "0", "CLOUD": "0"},
            },
            "abstain_utility": "0",
            "compute_cost_multiplier": "1",
            "latency_cost_per_second": "0.05",
            "uncertainty_method": "paired-range-v1",
        }

    def _append_pre_outcome_evidence(
        self,
        *,
        evaluation_id: str = "voc-derived",
        decision_input_sha256: str = SHA_A,
        baseline_output_sha256: str = SHA_D,
        challenger_output_sha256: str = SHA_E,
        sample_challenger_completed_at: str | None = None,
        quote_keys: tuple[str, ...] | None = None,
    ) -> tuple[str, str]:
        source_context = {
            "request_id": f"source:{evaluation_id}",
            "decision_input_sha256": decision_input_sha256,
            "task_class": "route-voc",
            "sport_id": "table_tennis",
            "league_id": "league-voc",
            "regime_id": "regime-voc",
            "urgency_id": "normal",
            "contradiction_state": "none",
        }
        context_record = DecisionRecord(
            replay_run_id=f"replay-{evaluation_id}-context",
            agent="voc-derived-test",
            observed_ts=T_DECISION,
            action="VOC_ROUTE_CONTEXT",
            payload={"voc_current_context": source_context},
            context_hash=decision_input_sha256,
            decision_id=f"decision-{evaluation_id}-context",
            recorded_at=T_DECISION,
        )
        context_sha = self.ledger.append(context_record)

        sample_challenger_completed_at = (
            sample_challenger_completed_at or T_CHALLENGER
        )
        quote_keys = self.quote_keys if quote_keys is None else quote_keys
        scoring_evidence = {
            "schema_version": 1,
            "evaluation_id": evaluation_id,
            "decision_input_sha256": decision_input_sha256,
            "baseline_output_sha256": baseline_output_sha256,
            "challenger_output_sha256": challenger_output_sha256,
            "baseline_action": "BASE",
            "challenger_action": "CLOUD",
            "baseline_abstained": False,
            "challenger_abstained": False,
            "samples": [
                {
                    "sample_id": f"{evaluation_id}:quote-101",
                    "quote_key": quote_keys[0],
                    "baseline_compute_cost": "0.10",
                    "challenger_compute_cost": "0.20",
                    "baseline_completed_at": T_BASELINE,
                    "challenger_completed_at": sample_challenger_completed_at,
                },
                {
                    "sample_id": f"{evaluation_id}:quote-202",
                    "quote_key": quote_keys[1],
                    "baseline_compute_cost": "0.10",
                    "challenger_compute_cost": "0.20",
                    "baseline_completed_at": T_BASELINE,
                    "challenger_completed_at": T_CHALLENGER,
                },
            ],
        }
        binding = {
            "decision_input_sha256": decision_input_sha256,
            "decision_context_sha256": context_sha,
            "baseline_candidate_id": "baseline",
            "baseline_backend_id": "local",
            "baseline_model_id": "baseline-model",
            "baseline_config_sha256": SHA_B,
            "baseline_output_sha256": baseline_output_sha256,
            "baseline_action": "BASE",
            "baseline_abstained": False,
            "challenger_candidate_id": "challenger",
            "challenger_backend_id": "cloud",
            "challenger_model_id": "challenger-model",
            "challenger_config_sha256": SHA_C,
            "challenger_output_sha256": challenger_output_sha256,
            "challenger_action": "CLOUD",
            "challenger_abstained": False,
            "sport_id": "table_tennis",
            "league_id": "league-voc",
            "regime_id": "regime-voc",
            "urgency_id": "normal",
            "contradiction_state": "none",
        }
        decision_record = DecisionRecord(
            replay_run_id=f"replay-{evaluation_id}",
            agent="voc-derived-test",
            observed_ts=T_DECISION,
            action="BASE",
            payload={
                "voc_binding": binding,
                "voc_scoring_evidence": scoring_evidence,
            },
            context_hash=decision_input_sha256,
            decision_id=f"decision-{evaluation_id}",
            recorded_at=T_BINDING,
        )
        decision_sha = self.ledger.append(decision_record)
        return context_sha, decision_sha

    def _protocol_and_holdout(self) -> tuple[str, str, str, str, str]:
        scoring_rule = self._scoring_rule()
        scoring_sha = digest(scoring_rule)
        multiple_control = "bonferroni-v1"
        multiple_sha = hashlib.sha256(multiple_control.encode("utf-8")).hexdigest()
        trial_family = "voc-confirmation-family-v1"
        dataset_id = "voc-dataset-derived"
        source_identity = "voc-dataset-source"
        license_identity = "voc-dataset-license"
        holdout = digest(
            {
                "schema_version": 1,
                "research_protocol_id": "voc-protocol-derived",
                "dataset_manifest_sha256": SHA_E,
                "source_identity": source_identity,
                "license_identity": license_identity,
                "confirmation_trial_family_id": trial_family,
            }
        )
        design = {
            "task_class": "route-voc",
            "estimand": "incremental_net_voc",
            "cohort_id": "voc-cohort-derived",
            "cohort_eligibility": {
                "kind": "decision-ledger-window-v1",
                "decision_recorded_from": T_DECISION,
                "decision_recorded_through": T_BINDING,
            },
            "outcome_cluster_rule": "provider-independent-market-v1",
            "evaluator_source_sha256": SHA_D,
            "scope": {
                "sport_id": "table_tennis",
                "league_id": "league-voc",
                "regime_id": "regime-voc",
                "urgency_id": "normal",
                "contradiction_state": "none",
            },
            "outcome_identity": {
                "event_id": self.outcome_authority.identity.event_id,
                "market_id": self.outcome_authority.identity.market_id,
                "source_id": self.outcome_authority.identity.source_id,
                "market_type": self.outcome_authority.identity.market_type.value,
                "sport_id": "table_tennis",
                "league_id": "league-voc",
                "regime_id": "regime-voc",
            },
            "scoring_rule": {
                "id": "voc-realized-v1",
                "payload": scoring_rule,
            },
            "confirmation_trial_family_id": trial_family,
        }
        protocol_payload = {
            "research_protocol_id": "voc-protocol-derived",
            "protocol_sha256": SHA_C,
            "binding": {
                "evaluation_design": json.dumps(
                    design,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "uncertainty_method": "paired-range-v1",
                "multiple_comparison_control": multiple_control,
            },
            "dataset_manifest_sha256": SHA_E,
        }
        if self.registry.get("ResearchProtocol", "voc-protocol-derived") is None:
            self.registry.append(
                RawScientificRecord(
                    record_type="ResearchProtocol",
                    record_id="voc-protocol-derived",
                    available_at=T_PROTOCOL,
                    payload=protocol_payload,
                )
            )
        if self.registry.get("DatasetSnapshot", dataset_id) is None:
            self.registry.append(
                RawScientificRecord(
                    record_type="DatasetSnapshot",
                    record_id=dataset_id,
                    available_at=T_PROTOCOL,
                    payload={
                        "dataset_snapshot_id": dataset_id,
                        "manifest_sha256": SHA_E,
                        "source_identity": source_identity,
                        "license_identity": license_identity,
                    },
                )
            )
        return scoring_sha, multiple_sha, holdout, trial_family, dataset_id

    def _evaluation(
        self,
        *,
        evaluation_id: str = "voc-derived",
        decision_input_sha256: str = SHA_A,
        baseline_output_sha256: str = SHA_D,
        challenger_output_sha256: str = SHA_E,
        forged_claim: bool = False,
        sample_challenger_completed_at: str | None = None,
        register_cohort: bool = True,
        outcome_authority=None,
    ) -> PairedVOCEvaluation:
        authority = self.outcome_authority if outcome_authority is None else outcome_authority
        context_sha, decision_sha = self._append_pre_outcome_evidence(
            evaluation_id=evaluation_id,
            decision_input_sha256=decision_input_sha256,
            baseline_output_sha256=baseline_output_sha256,
            challenger_output_sha256=challenger_output_sha256,
            sample_challenger_completed_at=sample_challenger_completed_at,
            quote_keys=authority.quote_keys,
        )
        scoring_sha, multiple_sha, holdout, _, _ = self._protocol_and_holdout()
        if forged_claim:
            challenger_utility = Decimal("9")
            compute_penalty = Decimal("0")
            latency_penalty = Decimal("0")
            measured_compute_cost = Decimal("0")
            interval_low = Decimal("8")
            interval_high = Decimal("10")
        else:
            challenger_utility = Decimal("1.5")
            compute_penalty = Decimal("0.1")
            latency_penalty = Decimal("0.05")
            measured_compute_cost = Decimal("0.1")
            interval_low = Decimal("1.35")
            interval_high = Decimal("1.35")
        paired = PairedVOCEvaluation(
            evaluation_id=evaluation_id,
            task_class="route-voc",
            sport_id="table_tennis",
            league_id="league-voc",
            regime_id="regime-voc",
            urgency_id="normal",
            contradiction_state="none",
            baseline_candidate_id="baseline",
            baseline_backend_id="local",
            baseline_model_id="baseline-model",
            baseline_config_sha256=SHA_B,
            challenger_candidate_id="challenger",
            challenger_backend_id="cloud",
            challenger_model_id="challenger-model",
            challenger_config_sha256=SHA_C,
            decision_input_sha256=decision_input_sha256,
            decision_context_sha256=context_sha,
            decision_evidence_sha256=decision_sha,
            baseline_output_sha256=baseline_output_sha256,
            challenger_output_sha256=challenger_output_sha256,
            baseline_action="BASE",
            challenger_action="CLOUD",
            baseline_abstained=False,
            challenger_abstained=False,
            decision_at=T_DECISION,
            decision_deadline=T_DEADLINE,
            baseline_completed_at=T_BASELINE,
            challenger_completed_at=T_CHALLENGER,
            outcome_evidence_sha256=authority.authority_sha256,
            outcome_revealed_at=T_REVEAL,
            evaluated_at=T_EVALUATED,
            scoring_rule_id="voc-realized-v1",
            scoring_rule_sha256=scoring_sha,
            research_protocol_id="voc-protocol-derived",
            research_protocol_sha256=SHA_C,
            holdout_access_id=holdout,
            multiple_comparison_control_sha256=multiple_sha,
            baseline_utility=Decimal("0"),
            challenger_utility=challenger_utility,
            compute_cost_penalty=compute_penalty,
            latency_opportunity_cost_penalty=latency_penalty,
            measured_compute_cost=measured_compute_cost,
            paired_sample_count=1,
            effective_sample_size=1,
            support_fraction=Decimal("1"),
            incremental_value_interval_low=interval_low,
            incremental_value_interval_high=interval_high,
            provenance=VOCEvaluationProvenance.MEASURED_SHADOW,
        )
        if register_cohort:
            self._append_cohort(paired)
        return paired

    def _append_cohort(self, *members: PairedVOCEvaluation) -> None:
        ordered = sorted(members, key=lambda item: item.evaluation_id)
        self.registry.append(
            RawScientificRecord(
                record_type="VOCCohort",
                record_id="voc-cohort-derived",
                available_at=max(item.evaluated_at for item in ordered),
                payload={
                    "cohort_id": "voc-cohort-derived",
                    "denominator": len(ordered),
                    "research_protocol_id": ordered[0].research_protocol_id,
                    "research_protocol_sha256": ordered[0].research_protocol_sha256,
                    "scoring_rule_sha256": ordered[0].scoring_rule_sha256,
                    "holdout_access_id": ordered[0].holdout_access_id,
                    "multiple_comparison_control_sha256": (
                        ordered[0].multiple_comparison_control_sha256
                    ),
                    "task_class": ordered[0].task_class,
                    "scope": {
                        "sport_id": ordered[0].sport_id,
                        "league_id": ordered[0].league_id,
                        "regime_id": ordered[0].regime_id,
                        "urgency_id": ordered[0].urgency_id,
                        "contradiction_state": ordered[0].contradiction_state,
                    },
                    "baseline_compute_identity": {
                        "candidate_id": ordered[0].baseline_candidate_id,
                        "backend_id": ordered[0].baseline_backend_id,
                        "model_id": ordered[0].baseline_model_id,
                        "config_sha256": ordered[0].baseline_config_sha256,
                    },
                    "challenger_compute_identity": {
                        "candidate_id": ordered[0].challenger_candidate_id,
                        "backend_id": ordered[0].challenger_backend_id,
                        "model_id": ordered[0].challenger_model_id,
                        "config_sha256": ordered[0].challenger_config_sha256,
                    },
                    "eligibility": {
                        "kind": "decision-ledger-window-v1",
                        "decision_recorded_from": T_DECISION,
                        "decision_recorded_through": T_BINDING,
                    },
                    "members": [
                        {
                            "evaluation_id": item.evaluation_id,
                            "evaluation_sha256": item.evaluation_sha256,
                            "decision_context_sha256": item.decision_context_sha256,
                            "decision_evidence_sha256": item.decision_evidence_sha256,
                        }
                        for item in ordered
                    ],
                },
            )
        )

    def _precommit_router(
        self,
        paired: PairedVOCEvaluation,
        *,
        protocol_id: str | None = None,
        cohort_id: str = "voc-cohort-derived",
        record_shadows: bool = True,
        authority_recorded_at: str | None = None,
        include_omitted_request: bool = False,
    ) -> ModelComputeRouterStore:
        router = ModelComputeRouterStore(
            self.root / f"router-precommit-{paired.evaluation_id}-{protocol_id or paired.research_protocol_id}-{cohort_id}.json"
        )
        request_id = f"source:{paired.evaluation_id}"
        request = ComputeRouteRequest(
            request_id=request_id,
            created_at=paired.decision_at,
            decision_deadline=paired.decision_deadline,
            required_capability=paired.task_class,
            data_classification=DataClassification.PUBLIC,
            allow_cloud=False,
            max_cost=Decimal("10"),
            response_ttl_seconds=Decimal("30"),
            baseline_candidate_id=paired.baseline_candidate_id,
            cloud_candidate_id=paired.challenger_candidate_id,
            decision_input_sha256=paired.decision_input_sha256,
            decision_evidence_sha256=paired.decision_context_sha256,
            voc_regime_id=paired.regime_id,
            voc_urgency_id=paired.urgency_id,
            voc_contradiction_state=paired.contradiction_state,
        )
        candidates = (
            ComputeCandidate(
                candidate_id=paired.baseline_candidate_id,
                tier=ComputeTier.LOCAL,
                backend_id=paired.baseline_backend_id,
                model_id=paired.baseline_model_id,
                config_sha256=paired.baseline_config_sha256,
                capabilities=(paired.task_class,),
                estimated_cost=Decimal("0.10"),
                estimated_latency_seconds=Decimal("1"),
            ),
            ComputeCandidate(
                candidate_id=paired.challenger_candidate_id,
                tier=ComputeTier.CLOUD,
                backend_id=paired.challenger_backend_id,
                model_id=paired.challenger_model_id,
                config_sha256=paired.challenger_config_sha256,
                capabilities=(paired.task_class,),
                estimated_cost=Decimal("0.20"),
                estimated_latency_seconds=Decimal("2"),
            ),
        )
        policy = ComputeRoutingPolicy(
            policy_id="voc-precompute-test-policy",
            policy_version=1,
            cloud_enabled=False,
        )
        authority_times = [
            authority_recorded_at or paired.decision_at,
        ]
        if record_shadows:
            authority_times.extend(
                [
                    paired.baseline_completed_at,
                    paired.challenger_completed_at,
                ]
            )
        with patch(
            "autosport.model_compute_router._authority_now",
            side_effect=authority_times,
        ):
            router.route(
                request,
                candidates,
                policy,
                as_of=paired.decision_at,
                voc_precompute_admission={
                    "admission_id": f"explicit:{paired.evaluation_id}",
                    "research_protocol_id": (
                        protocol_id or paired.research_protocol_id
                    ),
                    "cohort_id": cohort_id,
                    "baseline_candidate_id": paired.baseline_candidate_id,
                    "challenger_candidate_id": paired.challenger_candidate_id,
                    "sport_id": paired.sport_id,
                    "league_id": paired.league_id,
                },
            )
            if record_shadows:
                router.record_voc_shadow_execution(
                    request_id=request_id,
                    role="baseline",
                    output_sha256=paired.baseline_output_sha256,
                    action=paired.baseline_action,
                    abstained=paired.baseline_abstained,
                    completed_at=paired.baseline_completed_at,
                    available_at=paired.baseline_completed_at,
                    actual_cost=Decimal("0.10"),
                    evidence_sha256=paired.baseline_output_sha256,
                )
                router.record_voc_shadow_execution(
                    request_id=request_id,
                    role="challenger",
                    output_sha256=paired.challenger_output_sha256,
                    action=paired.challenger_action,
                    abstained=paired.challenger_abstained,
                    completed_at=paired.challenger_completed_at,
                    available_at=paired.challenger_completed_at,
                    actual_cost=Decimal("0.20"),
                    evidence_sha256=paired.challenger_output_sha256,
                )
        if include_omitted_request:
            omitted = ComputeRouteRequest(
                request_id=f"omitted:{paired.evaluation_id}",
                created_at=paired.decision_at,
                decision_deadline=paired.decision_deadline,
                required_capability=paired.task_class,
                data_classification=DataClassification.PUBLIC,
                allow_cloud=False,
                max_cost=Decimal("10"),
                response_ttl_seconds=Decimal("30"),
                baseline_candidate_id=paired.baseline_candidate_id,
                cloud_candidate_id=paired.challenger_candidate_id,
                decision_input_sha256=digest(
                    {"omitted": paired.evaluation_id, "kind": "input"}
                ),
                decision_evidence_sha256=digest(
                    {"omitted": paired.evaluation_id, "kind": "context"}
                ),
                voc_regime_id=paired.regime_id,
                voc_urgency_id=paired.urgency_id,
                voc_contradiction_state=paired.contradiction_state,
            )
            with patch(
                "autosport.model_compute_router._authority_now",
                return_value=paired.decision_at,
            ):
                router.route(
                    omitted,
                    candidates,
                    policy,
                    as_of=paired.decision_at,
                    voc_precompute_admission={
                        "admission_id": f"omitted:{paired.evaluation_id}",
                        "research_protocol_id": (
                            protocol_id or paired.research_protocol_id
                        ),
                        "cohort_id": cohort_id,
                        "baseline_candidate_id": paired.baseline_candidate_id,
                        "challenger_candidate_id": paired.challenger_candidate_id,
                        "sport_id": paired.sport_id,
                        "league_id": paired.league_id,
                    },
                )
        return router

    def _authority(
        self,
        *,
        compute_execution_store: ModelComputeRouterStore | None = None,
    ) -> CanonicalOutcomeDerivedVOCScoreAuthority:
        return CanonicalOutcomeDerivedVOCScoreAuthority(
            decision_ledger=self.ledger,
            scientific_registry=self.registry,
            outcome_authority=self.outcome_authority,
            outcome_source_root=self.root,
            source_record_file=self.outcome_file,
            source_record_sha256=self.outcome_sha256,
            additional_outcome_sources=(
                CanonicalVOCOutcomeSource(
                    authority=self.second_outcome_authority,
                    source_root=self.root,
                    source_record_file=self.second_outcome_file,
                    source_record_sha256=self.second_outcome_sha256,
                ),
            ),
            compute_execution_store=compute_execution_store,
        )

    def _append_scientific_qualification(
        self,
        paired: PairedVOCEvaluation,
        *,
        score_sha256: str,
        minimum_effective_sample_size: int = 1,
    ) -> None:
        score = self._authority().resolve(paired.evaluation_id, as_of=T_AS_OF)
        assert score is not None
        if score.score_sha256 != score_sha256:
            raise AssertionError("qualification score digest does not match canonical score")
        trial_family = "voc-confirmation-family-v1"
        dataset_id = "voc-dataset-derived"
        bundle_id = "voc-bundle-derived"
        experiment_id = "voc-experiment-derived"
        bundle_sha = SHA_B
        self.registry.append(
            RawScientificRecord(
                record_type="EvaluationBundle",
                record_id=bundle_id,
                available_at=T_EVALUATED,
                payload={
                    "evaluation_bundle_id": bundle_id,
                    "bundle_sha256": bundle_sha,
                    "evaluator_source_sha256": SHA_D,
                    "dataset_snapshot_id": dataset_id,
                    "protocol_sha256": paired.research_protocol_sha256,
                    "artifact_hashes": [score_sha256],
                    "effective_sample_size": score.effective_sample_size,
                    "effect_interval_low": str(score.incremental_value_interval_low),
                    "effect_interval_high": str(score.incremental_value_interval_high),
                    "practical_improvement": str(score.net_value),
                },
            )
        )
        self.registry.append(
            RawScientificRecord(
                record_type="Experiment",
                record_id=experiment_id,
                available_at=T_EVALUATED,
                payload={
                    "fingerprint": SHA_A,
                    "evaluation_bundle_id": bundle_id,
                },
            )
        )
        self.registry.append(
            RawScientificRecord(
                record_type="PromotionEvidence",
                record_id="voc-promotion-evidence-derived",
                available_at=T_EVALUATED,
                payload={
                    "research_protocol_id": paired.research_protocol_id,
                    "holdout_access_id": paired.holdout_access_id,
                    "multiple_comparison_control_sha256": (
                        paired.multiple_comparison_control_sha256
                    ),
                    "confirmation_trial_family_id": trial_family,
                    "estimand": "incremental_net_voc",
                    "cohort_id": "voc-cohort-derived",
                    "uncertainty_method": "paired-range-v1",
                    "validity": "ELIGIBLE",
                    "guardrails_passed": True,
                    "holdout_consumed": True,
                    "effective_sample_size": score.effective_sample_size,
                    "minimum_effective_sample_size": minimum_effective_sample_size,
                    "effect_interval_low": str(score.incremental_value_interval_low),
                    "effect_interval_high": str(score.incremental_value_interval_high),
                    "practical_improvement": str(score.net_value),
                    "evaluation_bundle_id": bundle_id,
                    "evaluation_bundle_sha256": bundle_sha,
                    "dataset_snapshot_id": dataset_id,
                    "experiment_id": experiment_id,
                },
            )
        )

    def test_production_authority_derives_score_and_restart_revalidates_it(self):
        paired = self._evaluation()
        self.registry.append(paired)

        authority = self._authority()
        score = authority.resolve(paired.evaluation_id, as_of=T_AS_OF)
        self.assertIsNotNone(score)
        assert score is not None
        self.assertEqual(score.baseline_utility, Decimal("0"))
        self.assertEqual(score.challenger_utility, Decimal("1.5"))
        self.assertEqual(score.measured_compute_cost, Decimal("0.10"))
        self.assertEqual(score.compute_cost_penalty, Decimal("0.10"))
        self.assertEqual(score.latency_opportunity_cost_penalty, Decimal("0.05"))
        self.assertEqual(score.paired_sample_count, 1)
        self.assertEqual(score.effective_sample_size, 1)
        self.assertEqual(score.support_fraction, Decimal("1"))
        self.assertEqual(score.incremental_value_interval_low, Decimal("1.35"))
        self.assertEqual(score.incremental_value_interval_high, Decimal("1.35"))
        self.assertEqual(score.net_value, Decimal("1.35"))

        self._append_scientific_qualification(
            paired,
            score_sha256=score.score_sha256,
        )
        resolver = build_canonical_voc_authority_resolver(
            decision_ledger=self.ledger,
            scientific_registry=self.registry,
            outcome_authority=self.outcome_authority,
            outcome_source_root=self.root,
            source_record_file=self.outcome_file,
            source_record_sha256=self.outcome_sha256,
            additional_outcome_sources=(
                CanonicalVOCOutcomeSource(
                    authority=self.second_outcome_authority,
                    source_root=self.root,
                    source_record_file=self.second_outcome_file,
                    source_record_sha256=self.second_outcome_sha256,
                ),
            ),
        )
        self.assertEqual(resolver.resolve(paired, as_of=T_AS_OF), paired)

        store_path = self.root / "voc-store.json"
        store = VOCEvaluationStore(
            store_path,
            canonical_authority_resolver=resolver,
        )
        store.record(paired)

        restarted_registry = ScientificRegistry(self.registry.path)
        restarted_resolver = build_canonical_voc_authority_resolver(
            decision_ledger=JsonlDecisionLedger(self.ledger.path),
            scientific_registry=restarted_registry,
            outcome_authority=self.outcome_authority,
            outcome_source_root=self.root,
            source_record_file=self.outcome_file,
            source_record_sha256=self.outcome_sha256,
            additional_outcome_sources=(
                CanonicalVOCOutcomeSource(
                    authority=self.second_outcome_authority,
                    source_root=self.root,
                    source_record_file=self.second_outcome_file,
                    source_record_sha256=self.second_outcome_sha256,
                ),
            ),
        )
        restarted_store = VOCEvaluationStore(
            store_path,
            canonical_authority_resolver=restarted_resolver,
        )
        self.assertEqual(
            restarted_store.require(
                paired.evaluation_id,
                evaluation_sha256=paired.evaluation_sha256,
                as_of=T_AS_OF,
            ),
            paired,
        )

    def test_multiple_quote_legs_count_as_one_paired_compute_decision(self):
        paired = self._evaluation()
        self.registry.append(paired)

        score = self._authority().resolve(paired.evaluation_id, as_of=T_AS_OF)
        self.assertIsNotNone(score)
        assert score is not None
        self.assertEqual(len(self.quote_keys), 2)
        self.assertEqual(score.paired_sample_count, 1)
        self.assertEqual(score.effective_sample_size, 1)
        self.assertEqual(score.support_fraction, Decimal("1"))

    def test_scoring_sample_must_be_frozen_by_decision_record(self):
        paired = self._evaluation(
            sample_challenger_completed_at="2026-09-20T00:00:13Z",
        )
        self.registry.append(paired)

        with self.assertRaisesRegex(
            VOCEvaluationError,
            "scoring sample completion was not frozen by DecisionRecord",
        ):
            self._authority().resolve(paired.evaluation_id, as_of=T_AS_OF)

    def test_same_market_decisions_do_not_inflate_outcome_cluster_ess(self):
        first = self._evaluation(register_cohort=False)
        second = self._evaluation(
            evaluation_id="voc-derived-2",
            decision_input_sha256=digest({"episode": 2, "kind": "input"}),
            baseline_output_sha256=digest({"episode": 2, "kind": "baseline"}),
            challenger_output_sha256=digest({"episode": 2, "kind": "challenger"}),
            register_cohort=False,
        )
        self.registry.append(first)
        self.registry.append(second)
        self._append_cohort(first, second)
        score = self._authority().resolve(first.evaluation_id, as_of=T_AS_OF)
        self.assertIsNotNone(score)
        assert score is not None
        self.assertEqual(score.paired_sample_count, 2)
        self.assertEqual(score.effective_sample_size, 1)
        self.assertEqual(score.incremental_value_interval_low, Decimal("1.35"))
        self.assertEqual(score.incremental_value_interval_high, Decimal("1.35"))

    def test_independent_markets_contribute_independent_outcome_cluster_support(self):
        first = self._evaluation(register_cohort=False)
        second = self._evaluation(
            evaluation_id="voc-derived-2",
            decision_input_sha256=digest({"episode": 2, "kind": "input"}),
            baseline_output_sha256=digest({"episode": 2, "kind": "baseline"}),
            challenger_output_sha256=digest({"episode": 2, "kind": "challenger"}),
            register_cohort=False,
            outcome_authority=self.second_outcome_authority,
        )
        self.registry.append(first)
        self.registry.append(second)
        self._append_cohort(first, second)
        score = self._authority().resolve(first.evaluation_id, as_of=T_AS_OF)
        self.assertIsNotNone(score)
        assert score is not None
        self.assertEqual(score.paired_sample_count, 2)
        self.assertEqual(score.effective_sample_size, 2)

    def test_positive_voc_rejects_physically_post_outcome_precompute(self):
        paired = self._evaluation()
        self.registry.append(paired)
        router = self._precommit_router(
            paired,
            record_shadows=False,
            authority_recorded_at=T_AS_OF,
        )
        with self.assertRaisesRegex(
            VOCEvaluationError,
            "precompute authority was not physically recorded before outcome reveal",
        ):
            self._authority(compute_execution_store=router).resolve(
                paired.evaluation_id,
                as_of=T_AS_OF,
            )

    def test_positive_voc_requires_exact_router_shadow_execution(self):
        paired = self._evaluation()
        self.registry.append(paired)
        router = self._precommit_router(paired, record_shadows=False)
        with self.assertRaisesRegex(
            VOCEvaluationError,
            "positive VOC score lacks canonical router shadow execution",
        ):
            self._authority(compute_execution_store=router).resolve(
                paired.evaluation_id,
                as_of=T_AS_OF,
            )

    def test_positive_voc_accepts_exact_preoutcome_router_shadow_execution(self):
        paired = self._evaluation(register_cohort=False)
        successful = next(
            record
            for record in self.ledger.verified_records()
            if getattr(record, "decision_id", None)
            == f"decision-{paired.evaluation_id}"
        )
        router = self._precommit_router(paired)
        append_paired_voc_admission(
            self.ledger,
            admission_id=f"explicit:{paired.evaluation_id}",
            decision_context_sha256=paired.decision_context_sha256,
            decision_input_sha256=paired.decision_input_sha256,
            decision_deadline=paired.decision_deadline,
            research_protocol_id=paired.research_protocol_id,
            cohort_id="voc-cohort-derived",
            task_class=paired.task_class,
            scope={
                "sport_id": paired.sport_id,
                "league_id": paired.league_id,
                "regime_id": paired.regime_id,
                "urgency_id": paired.urgency_id,
                "contradiction_state": paired.contradiction_state,
            },
            baseline_compute_identity={
                "candidate_id": paired.baseline_candidate_id,
                "backend_id": paired.baseline_backend_id,
                "model_id": paired.baseline_model_id,
                "config_sha256": paired.baseline_config_sha256,
            },
            challenger_compute_identity={
                "candidate_id": paired.challenger_candidate_id,
                "backend_id": paired.challenger_backend_id,
                "model_id": paired.challenger_model_id,
                "config_sha256": paired.challenger_config_sha256,
            },
            replay_run_id="replay-voc-derived-explicit-admission",
            agent="voc-derived-test",
            recorded_at=T_DECISION,
        )
        terminal_sha = self.ledger.append(
            DecisionRecord(
                replay_run_id="replay-voc-derived-explicit-terminal",
                agent="voc-derived-test",
                observed_ts=T_DECISION,
                action=successful.action,
                payload=successful.to_dict()["payload"],
                context_hash=paired.decision_input_sha256,
                decision_id=f"decision-{paired.evaluation_id}-explicit-terminal",
                recorded_at=T_BINDING,
            )
        )
        paired = replace(paired, decision_evidence_sha256=terminal_sha)
        self.registry.append(paired)
        self._append_cohort(paired)

        score = self._authority(compute_execution_store=router).resolve(
            paired.evaluation_id,
            as_of=T_AS_OF,
        )

        self.assertIsNotNone(score)
        assert score is not None
        self.assertEqual(score.evaluation_id, paired.evaluation_id)
        self.assertEqual(score.measured_compute_cost, Decimal("0.10"))
        self.assertEqual(score.net_value, Decimal("1.35"))

    def test_cohort_rejects_omitted_eligible_episode(self):
        first = self._evaluation(register_cohort=False)
        second = self._evaluation(
            evaluation_id="voc-derived-2",
            decision_input_sha256=digest({"episode": 2, "kind": "input"}),
            baseline_output_sha256=digest({"episode": 2, "kind": "baseline"}),
            challenger_output_sha256=digest({"episode": 2, "kind": "challenger"}),
            register_cohort=False,
        )
        self.registry.append(first)
        self.registry.append(second)
        self._append_cohort(first)
        with self.assertRaisesRegex(
            VOCEvaluationError,
            "does not equal precommitted eligible DecisionLedger population",
        ):
            self._authority().resolve(first.evaluation_id, as_of=T_AS_OF)

    def test_cohort_rejects_member_registered_after_cohort_freeze(self):
        first = self._evaluation(register_cohort=False)
        second = self._evaluation(
            evaluation_id="voc-derived-2",
            decision_input_sha256=digest({"episode": 2, "kind": "input"}),
            baseline_output_sha256=digest({"episode": 2, "kind": "baseline"}),
            challenger_output_sha256=digest({"episode": 2, "kind": "challenger"}),
            register_cohort=False,
        )
        self.registry.append(first)
        self.registry.append(
            RawScientificRecord(
                record_type="PairedVOCEvaluation",
                record_id=second.evaluation_id,
                available_at=T_AS_OF,
                payload=second.to_payload(),
            )
        )
        self._append_cohort(first, second)
        with self.assertRaisesRegex(
            VOCEvaluationError,
            "registered after cohort freeze",
        ):
            self._authority().resolve(first.evaluation_id, as_of=T_AS_OF)

    def test_cohort_rejects_duplicate_decision_episode_identity(self):
        first = self._evaluation(register_cohort=False)
        second = replace(first, evaluation_id="voc-derived-duplicate")
        self.registry.append(first)
        self.registry.append(second)
        self._append_cohort(first, second)
        with self.assertRaisesRegex(
            VOCEvaluationError,
            "reuses one paired compute-decision episode",
        ):
            self._authority().resolve(first.evaluation_id, as_of=T_AS_OF)

    def test_score_digest_is_independent_of_ambient_decimal_context(self):
        paired = self._evaluation()
        self.registry.append(paired)
        baseline = self._authority().resolve(paired.evaluation_id, as_of=T_AS_OF)
        self.assertIsNotNone(baseline)
        assert baseline is not None

        with localcontext() as context:
            context.prec = 1
            context.rounding = ROUND_DOWN
            constrained = self._authority().resolve(
                paired.evaluation_id,
                as_of=T_AS_OF,
            )
            self.assertIsNotNone(constrained)
            assert constrained is not None
            self.assertEqual(constrained.score_sha256, baseline.score_sha256)
            self.assertEqual(constrained.net_value, baseline.net_value)

    def test_caller_supplied_positive_utility_cannot_mint_voc(self):
        forged = self._evaluation(forged_claim=True)
        self.registry.append(forged)
        authority = self._authority()
        score = authority.resolve(forged.evaluation_id, as_of=T_AS_OF)
        self.assertIsNotNone(score)
        assert score is not None
        self.assertEqual(score.challenger_utility, Decimal("1.5"))
        self.assertNotEqual(score.challenger_utility, forged.challenger_utility)
        self.assertEqual(score.net_value, Decimal("1.35"))
        self.assertNotEqual(score.net_value, forged.net_value)

        resolver = build_canonical_voc_authority_resolver(
            decision_ledger=self.ledger,
            scientific_registry=self.registry,
            outcome_authority=self.outcome_authority,
            outcome_source_root=self.root,
            source_record_file=self.outcome_file,
            source_record_sha256=self.outcome_sha256,
            additional_outcome_sources=(
                CanonicalVOCOutcomeSource(
                    authority=self.second_outcome_authority,
                    source_root=self.root,
                    source_record_file=self.second_outcome_file,
                    source_record_sha256=self.second_outcome_sha256,
                ),
            ),
        )
        with self.assertRaisesRegex(
            VOCEvaluationError,
            "canonical outcome-derived VOC score does not match routed evaluation",
        ):
            resolver.resolve(forged, as_of=T_AS_OF)


if __name__ == "__main__":
    unittest.main()
