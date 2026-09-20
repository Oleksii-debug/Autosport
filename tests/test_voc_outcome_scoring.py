from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
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
        sample_challenger_completed_at: str | None = None,
    ) -> tuple[str, str]:
        source_context = {
            "request_id": "source:voc-derived",
            "decision_input_sha256": SHA_A,
            "task_class": "route-voc",
            "sport_id": "table_tennis",
            "league_id": "league-voc",
            "regime_id": "regime-voc",
            "urgency_id": "normal",
            "contradiction_state": "none",
        }
        context_record = DecisionRecord(
            replay_run_id="replay-voc-derived-context",
            agent="voc-derived-test",
            observed_ts=T_DECISION,
            action="VOC_ROUTE_CONTEXT",
            payload={"voc_current_context": source_context},
            context_hash=SHA_A,
            decision_id="decision-voc-derived-context",
            recorded_at=T_DECISION,
        )
        context_sha = self.ledger.append(context_record)

        sample_challenger_completed_at = (
            sample_challenger_completed_at or T_CHALLENGER
        )
        scoring_evidence = {
            "schema_version": 1,
            "evaluation_id": "voc-derived",
            "decision_input_sha256": SHA_A,
            "baseline_output_sha256": SHA_D,
            "challenger_output_sha256": SHA_E,
            "baseline_action": "BASE",
            "challenger_action": "CLOUD",
            "baseline_abstained": False,
            "challenger_abstained": False,
            "samples": [
                {
                    "sample_id": "quote-101",
                    "quote_key": self.quote_keys[0],
                    "baseline_compute_cost": "0.10",
                    "challenger_compute_cost": "0.20",
                    "baseline_completed_at": T_BASELINE,
                    "challenger_completed_at": sample_challenger_completed_at,
                },
                {
                    "sample_id": "quote-202",
                    "quote_key": self.quote_keys[1],
                    "baseline_compute_cost": "0.10",
                    "challenger_compute_cost": "0.20",
                    "baseline_completed_at": T_BASELINE,
                    "challenger_completed_at": T_CHALLENGER,
                },
            ],
        }
        binding = {
            "decision_input_sha256": SHA_A,
            "decision_context_sha256": context_sha,
            "baseline_candidate_id": "baseline",
            "baseline_backend_id": "local",
            "baseline_model_id": "baseline-model",
            "baseline_config_sha256": SHA_B,
            "baseline_output_sha256": SHA_D,
            "baseline_action": "BASE",
            "baseline_abstained": False,
            "challenger_candidate_id": "challenger",
            "challenger_backend_id": "cloud",
            "challenger_model_id": "challenger-model",
            "challenger_config_sha256": SHA_C,
            "challenger_output_sha256": SHA_E,
            "challenger_action": "CLOUD",
            "challenger_abstained": False,
            "sport_id": "table_tennis",
            "league_id": "league-voc",
            "regime_id": "regime-voc",
            "urgency_id": "normal",
            "contradiction_state": "none",
        }
        decision_record = DecisionRecord(
            replay_run_id="replay-voc-derived",
            agent="voc-derived-test",
            observed_ts=T_DECISION,
            action="BASE",
            payload={
                "voc_binding": binding,
                "voc_scoring_evidence": scoring_evidence,
            },
            context_hash=SHA_A,
            decision_id="decision-voc-derived",
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
        self.registry.append(
            RawScientificRecord(
                record_type="ResearchProtocol",
                record_id="voc-protocol-derived",
                available_at=T_PROTOCOL,
                payload=protocol_payload,
            )
        )
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
        forged_claim: bool = False,
        sample_challenger_completed_at: str | None = None,
    ) -> PairedVOCEvaluation:
        context_sha, decision_sha = self._append_pre_outcome_evidence(
            sample_challenger_completed_at=sample_challenger_completed_at,
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
            interval_low = Decimal("0.85")
            interval_high = Decimal("1.85")
        return PairedVOCEvaluation(
            evaluation_id="voc-derived",
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
            decision_input_sha256=SHA_A,
            decision_context_sha256=context_sha,
            decision_evidence_sha256=decision_sha,
            baseline_output_sha256=SHA_D,
            challenger_output_sha256=SHA_E,
            baseline_action="BASE",
            challenger_action="CLOUD",
            baseline_abstained=False,
            challenger_abstained=False,
            decision_at=T_DECISION,
            decision_deadline=T_DEADLINE,
            baseline_completed_at=T_BASELINE,
            challenger_completed_at=T_CHALLENGER,
            outcome_evidence_sha256=self.outcome_authority.authority_sha256,
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
            paired_sample_count=2,
            effective_sample_size=2,
            support_fraction=Decimal("1"),
            incremental_value_interval_low=interval_low,
            incremental_value_interval_high=interval_high,
            provenance=VOCEvaluationProvenance.MEASURED_SHADOW,
        )

    def _authority(self) -> CanonicalOutcomeDerivedVOCScoreAuthority:
        return CanonicalOutcomeDerivedVOCScoreAuthority(
            decision_ledger=self.ledger,
            scientific_registry=self.registry,
            outcome_authority=self.outcome_authority,
            outcome_source_root=self.root,
            source_record_file=self.outcome_file,
            source_record_sha256=self.outcome_sha256,
        )

    def _append_scientific_qualification(
        self,
        paired: PairedVOCEvaluation,
        *,
        score_sha256: str,
    ) -> None:
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
                    "effective_sample_size": paired.effective_sample_size,
                    "effect_interval_low": str(paired.incremental_value_interval_low),
                    "effect_interval_high": str(paired.incremental_value_interval_high),
                    "practical_improvement": str(paired.net_value),
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
                    "effective_sample_size": paired.effective_sample_size,
                    "minimum_effective_sample_size": 2,
                    "effect_interval_low": str(paired.incremental_value_interval_low),
                    "effect_interval_high": str(paired.incremental_value_interval_high),
                    "practical_improvement": str(paired.net_value),
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
        self.assertEqual(score.effective_sample_size, 2)
        self.assertEqual(score.support_fraction, Decimal("1"))
        self.assertEqual(score.incremental_value_interval_low, Decimal("0.85"))
        self.assertEqual(score.incremental_value_interval_high, Decimal("1.85"))
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
        )
        with self.assertRaisesRegex(
            VOCEvaluationError,
            "canonical outcome-derived VOC score does not match routed evaluation",
        ):
            resolver.resolve(forged, as_of=T_AS_OF)


if __name__ == "__main__":
    unittest.main()
