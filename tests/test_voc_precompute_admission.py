from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch
from pathlib import Path

from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.model_compute_router import (
    ComputeCandidate,
    ComputeRouteRequest,
    ComputeRoutingPolicy,
    ComputeTier,
    DataClassification,
    ModelComputeRouterError,
    ModelComputeRouterStore,
)
from autosport.voc_evaluation import VOCEvaluationError
from autosport.voc_outcome_scoring import (
    append_paired_voc_admission,
    append_paired_voc_terminal,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
T_DECISION = "2026-09-20T00:00:00Z"
T_DEADLINE = "2026-09-20T00:00:10Z"
T_TIMEOUT = "2026-09-20T00:00:11Z"


class VOCPrecomputeAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.path = root / "decision-ledger.jsonl"
        self.ledger = JsonlDecisionLedger(self.path)
        self.scope = {
            "sport_id": "table_tennis",
            "league_id": "league-voc",
            "regime_id": "regime-voc",
            "urgency_id": "normal",
            "contradiction_state": "none",
        }
        self.baseline = {
            "candidate_id": "baseline",
            "backend_id": "baseline-backend",
            "model_id": "baseline-model",
            "config_sha256": SHA_B,
        }
        self.challenger = {
            "candidate_id": "challenger",
            "backend_id": "challenger-backend",
            "model_id": "challenger-model",
            "config_sha256": SHA_C,
        }
        context = DecisionRecord(
            replay_run_id="replay-context",
            agent="voc-admission-test",
            observed_ts=T_DECISION,
            action="VOC_ROUTE_CONTEXT",
            payload={
                "voc_current_context": {
                    "request_id": "request-1",
                    "decision_input_sha256": SHA_A,
                    "task_class": "route-voc",
                    **self.scope,
                }
            },
            context_hash=SHA_A,
            decision_id="decision-context",
            recorded_at=T_DECISION,
        )
        self.context_sha = self.ledger.append(context)

        self.router_path = root / "router.json"
        self.router = ModelComputeRouterStore(self.router_path)
        baseline_candidate = ComputeCandidate(
            candidate_id="baseline",
            tier=ComputeTier.LOCAL,
            backend_id="baseline-backend",
            model_id="baseline-model",
            config_sha256=SHA_B,
            capabilities=("route-voc",),
            estimated_cost=Decimal("0.5"),
            estimated_latency_seconds=Decimal("0.5"),
        )
        challenger_candidate = ComputeCandidate(
            candidate_id="challenger",
            tier=ComputeTier.CLOUD,
            backend_id="challenger-backend",
            model_id="challenger-model",
            config_sha256=SHA_C,
            capabilities=("route-voc",),
            estimated_cost=Decimal("1"),
            estimated_latency_seconds=Decimal("1"),
        )
        self.candidates = (baseline_candidate, challenger_candidate)
        request = ComputeRouteRequest(
            request_id="request-1",
            created_at=T_DECISION,
            decision_deadline=T_DEADLINE,
            required_capability="route-voc",
            data_classification=DataClassification.PUBLIC,
            allow_cloud=False,
            max_cost=Decimal("10"),
            response_ttl_seconds=Decimal("10"),
            baseline_candidate_id="baseline",
            cloud_candidate_id=None,
            decision_input_sha256=SHA_A,
            decision_evidence_sha256=self.context_sha,
            voc_regime_id="regime-voc",
            voc_urgency_id="normal",
            voc_contradiction_state="none",
        )
        policy = ComputeRoutingPolicy(
            policy_id="terminal-measurement-test",
            policy_version=1,
            cloud_enabled=False,
        )
        with patch(
            "autosport.model_compute_router._authority_now",
            return_value=T_DECISION,
        ):
            self.router.route(
                request,
                self.candidates,
                policy,
                as_of=T_DECISION,
                voc_precompute_admission={
                    "admission_id": "admission-1",
                    "research_protocol_id": "protocol-1",
                    "cohort_id": "cohort-1",
                    "baseline_candidate_id": "baseline",
                    "challenger_candidate_id": "challenger",
                    "sport_id": "table_tennis",
                    "league_id": "league-voc",
                },
            )
        self.request = request
        self.policy = policy

    def test_router_owns_precompute_admission_and_restart_revalidates_it(self) -> None:
        authority = self.router.get_voc_precompute_admission("request-1")
        self.assertIsNotNone(authority)
        assert authority is not None
        self.assertEqual(authority["admission_id"], "admission-1")
        self.assertEqual(authority["decision_context_sha256"], self.context_sha)
        self.assertEqual(authority["baseline_compute_identity"], self.baseline)
        self.assertEqual(authority["challenger_compute_identity"], self.challenger)
        self.assertEqual(len(authority["route_record_sha256"]), 64)

        reopened = ModelComputeRouterStore(self.router_path)
        self.assertEqual(
            reopened.get_voc_precompute_admission("request-1"),
            authority,
        )
        self.assertEqual(reopened.voc_precompute_admissions(), (authority,))

    def test_existing_route_cannot_gain_posthoc_precompute_admission(self) -> None:
        path = self.router_path.with_name("router-without-precommit.json")
        router = ModelComputeRouterStore(path)
        router.route(
            self.request,
            self.candidates,
            self.policy,
            as_of=T_DECISION,
        )
        self.assertIsNone(router.get_voc_precompute_admission("request-1"))
        with self.assertRaisesRegex(
            ModelComputeRouterError,
            "immutable request id conflicts with stored route",
        ):
            router.route(
                self.request,
                self.candidates,
                self.policy,
                as_of=T_DECISION,
                voc_precompute_admission={
                    "admission_id": "admission-1",
                    "research_protocol_id": "protocol-1",
                    "cohort_id": "cohort-1",
                    "baseline_candidate_id": "baseline",
                    "challenger_candidate_id": "challenger",
                    "sport_id": "table_tennis",
                    "league_id": "league-voc",
                },
            )

    def test_shadow_output_cannot_predate_physical_precompute_authority(self) -> None:
        path = self.router_path.with_name("router-post-output-precommit.json")
        router = ModelComputeRouterStore(path)
        with patch(
            "autosport.model_compute_router._authority_now",
            return_value="2026-09-20T00:00:04Z",
        ):
            router.route(
                self.request,
                self.candidates,
                self.policy,
                as_of=T_DECISION,
                voc_precompute_admission={
                    "admission_id": "admission-physical-fence",
                    "research_protocol_id": "protocol-1",
                    "cohort_id": "cohort-1",
                    "baseline_candidate_id": "baseline",
                    "challenger_candidate_id": "challenger",
                    "sport_id": "table_tennis",
                    "league_id": "league-voc",
                },
            )

        with patch(
            "autosport.model_compute_router._authority_now",
            return_value="2026-09-20T00:00:05Z",
        ):
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "predates physical precompute authority",
            ):
                router.record_voc_shadow_execution(
                    request_id="request-1",
                    role="baseline",
                    output_sha256=SHA_A,
                    action="BASE",
                    abstained=False,
                    completed_at="2026-09-20T00:00:03Z",
                    available_at="2026-09-20T00:00:03Z",
                    actual_cost=Decimal("0.10"),
                    evidence_sha256=SHA_A,
                )

    def test_shadow_execution_authority_is_append_only_and_restart_safe(self) -> None:
        with patch(
            "autosport.model_compute_router._authority_now",
            side_effect=[
                "2026-09-20T00:00:05Z",
                "2026-09-20T00:00:06Z",
            ],
        ):
            baseline = self.router.record_voc_shadow_execution(
                request_id="request-1",
                role="baseline",
                output_sha256=SHA_A,
                action="BASE",
                abstained=False,
                completed_at="2026-09-20T00:00:05Z",
                available_at="2026-09-20T00:00:05Z",
                actual_cost=Decimal("0.10"),
                evidence_sha256=SHA_A,
            )
            challenger = self.router.record_voc_shadow_execution(
                request_id="request-1",
                role="challenger",
                output_sha256=SHA_B,
                action="CLOUD",
                abstained=False,
                completed_at="2026-09-20T00:00:06Z",
                available_at="2026-09-20T00:00:06Z",
                actual_cost=Decimal("0.20"),
                evidence_sha256=SHA_B,
            )
        self.assertEqual(baseline["authority_sequence"], 1)
        self.assertEqual(challenger["authority_sequence"], 2)
        self.assertEqual(
            challenger["previous_authority_sha256"],
            baseline["authority_sha256"],
        )

        reopened = ModelComputeRouterStore(self.router_path)
        self.assertEqual(
            reopened.get_voc_shadow_execution("request-1", "baseline"),
            baseline,
        )
        self.assertEqual(
            reopened.get_voc_shadow_execution("request-1", "challenger"),
            challenger,
        )
        with self.assertRaisesRegex(
            ModelComputeRouterError,
            "historical/restarted requests cannot be backfilled",
        ):
            reopened.record_voc_shadow_execution(
                request_id="request-1",
                role="baseline",
                output_sha256=SHA_A,
                action="BASE",
                abstained=False,
                completed_at="2026-09-20T00:00:05Z",
                available_at="2026-09-20T00:00:05Z",
                actual_cost=Decimal("0.10"),
                evidence_sha256=SHA_A,
            )

    def _admit(self) -> str:
        return append_paired_voc_admission(
            self.ledger,
            admission_id="admission-1",
            decision_context_sha256=self.context_sha,
            decision_input_sha256=SHA_A,
            decision_deadline=T_DEADLINE,
            research_protocol_id="protocol-1",
            cohort_id="cohort-1",
            task_class="route-voc",
            scope=self.scope,
            baseline_compute_identity=self.baseline,
            challenger_compute_identity=self.challenger,
            replay_run_id="replay-admission",
            agent="voc-admission-test",
            recorded_at=T_DECISION,
        )

    def _record_timeout_execution(
        self,
        *,
        execution_id: str = "execution-timeout-1",
        actual_cost: Decimal = Decimal("0.25"),
        latency: Decimal = Decimal("10"),
    ):
        return self.router.record_execution(
            execution_id=execution_id,
            request_id="request-1",
            completed_at=T_TIMEOUT,
            available_at=T_TIMEOUT,
            backend_id="challenger-backend",
            model_id="challenger-model",
            config_sha256=SHA_C,
            actual_cost=actual_cost,
            actual_latency_seconds=latency,
            evidence_sha256=SHA_B,
            as_of=T_TIMEOUT,
        )

    def test_terminal_binds_canonical_execution_and_survives_restart(self) -> None:
        admission_sha = self._admit()
        execution = self._record_timeout_execution()
        with patch(
            "autosport.voc_outcome_scoring._authority_now",
            return_value=T_TIMEOUT,
        ):
            terminal_sha = append_paired_voc_terminal(
                self.ledger,
                compute_execution_store=self.router,
                admission_sha256=admission_sha,
                status="timeout",
                execution_id=execution.execution_id,
                replay_run_id="replay-terminal",
                agent="voc-admission-test",
                recorded_at=T_TIMEOUT,
            )

        records = JsonlDecisionLedger(self.path).verified_records()
        self.assertEqual(
            [record.action for record in records],
            ["VOC_ROUTE_CONTEXT", "VOC_PAIRED_ADMISSION", "VOC_PAIRED_TERMINAL"],
        )
        admission = records[1].payload["voc_paired_admission"]
        self.assertEqual(admission["decision_context_sha256"], self.context_sha)
        self.assertEqual(admission["baseline_compute_identity"], self.baseline)
        self.assertEqual(admission["challenger_compute_identity"], self.challenger)
        self.assertEqual(admission["research_protocol_id"], "protocol-1")
        self.assertEqual(admission["cohort_id"], "cohort-1")
        self.assertNotIn("baseline_output_sha256", admission)
        self.assertNotIn("challenger_output_sha256", admission)

        terminal = records[2].payload["voc_terminal"]
        self.assertEqual(terminal["schema_version"], 3)
        self.assertEqual(terminal["authority_recorded_at"], T_TIMEOUT)
        self.assertEqual(terminal["admission_sha256"], admission_sha)
        self.assertEqual(terminal["status"], "timeout")
        self.assertEqual(terminal["execution_id"], execution.execution_id)
        self.assertEqual(
            terminal["execution_record_sha256"], execution.execution_record_sha256
        )
        self.assertNotIn("observed_extra_compute_cost", terminal)
        self.assertNotIn("observed_extra_latency_seconds", terminal)
        self.assertEqual(
            terminal_sha,
            hashlib.sha256(
                json.dumps(
                    records[2].to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
        )

        reopened = ModelComputeRouterStore(self.router_path)
        self.assertEqual(
            reopened.total_actual_cost("request-1"), Decimal("0.25")
        )

    def test_terminal_physical_authority_cannot_predate_logical_terminal(self) -> None:
        admission_sha = self._admit()
        execution = self._record_timeout_execution()
        with patch(
            "autosport.voc_outcome_scoring._authority_now",
            return_value=T_DECISION,
        ):
            with self.assertRaisesRegex(
                VOCEvaluationError,
                "physical authority predates logical terminal time",
            ):
                append_paired_voc_terminal(
                    self.ledger,
                    compute_execution_store=self.router,
                    admission_sha256=admission_sha,
                    status="timeout",
                    execution_id=execution.execution_id,
                    replay_run_id="replay-terminal-posthoc",
                    agent="voc-admission-test",
                    recorded_at=T_TIMEOUT,
                )

    def test_missing_or_forged_zero_measurement_cannot_terminalize(self) -> None:
        admission_sha = self._admit()
        with self.assertRaisesRegex(
            VOCEvaluationError,
            "canonical compute execution is missing",
        ):
            append_paired_voc_terminal(
                self.ledger,
                compute_execution_store=self.router,
                admission_sha256=admission_sha,
                status="timeout",
                execution_id="caller-forged-zero-measurement",
                replay_run_id="replay-terminal-forged-zero",
                agent="voc-admission-test",
                recorded_at=T_TIMEOUT,
            )
        self.assertEqual(len(self.ledger.verified_records()), 2)

    def test_timeout_cannot_be_backdated_before_frozen_deadline(self) -> None:
        admission_sha = self._admit()
        execution = self._record_timeout_execution()
        with self.assertRaisesRegex(
            VOCEvaluationError,
            "recorded before the frozen deadline",
        ):
            append_paired_voc_terminal(
                self.ledger,
                compute_execution_store=self.router,
                admission_sha256=admission_sha,
                status="timeout",
                execution_id=execution.execution_id,
                replay_run_id="replay-terminal-early",
                agent="voc-admission-test",
                recorded_at="2026-09-20T00:00:05Z",
            )


    def test_true_deadline_miss_shadow_is_durable_negative_evidence(self) -> None:
        with patch(
            "autosport.model_compute_router._authority_now",
            return_value=T_TIMEOUT,
        ):
            late = self.router.record_voc_shadow_execution(
                request_id="request-1",
                role="challenger",
                output_sha256=SHA_B,
                action="CLOUD",
                abstained=False,
                completed_at=T_TIMEOUT,
                available_at=T_TIMEOUT,
                actual_cost=Decimal("0.20"),
                evidence_sha256=SHA_B,
            )
        self.assertEqual(late["completed_at"], T_TIMEOUT)
        self.assertEqual(late["available_at"], T_TIMEOUT)
        reopened = ModelComputeRouterStore(self.router_path)
        self.assertEqual(
            reopened.get_voc_shadow_execution("request-1", "challenger"),
            late,
        )

    def test_on_time_completion_unavailable_by_deadline_remains_rejected(self) -> None:
        with patch(
            "autosport.model_compute_router._authority_now",
            return_value=T_TIMEOUT,
        ):
            with self.assertRaisesRegex(
                ModelComputeRouterError,
                "completed on time but was unavailable by deadline",
            ):
                self.router.record_voc_shadow_execution(
                    request_id="request-1",
                    role="challenger",
                    output_sha256=SHA_B,
                    action="CLOUD",
                    abstained=False,
                    completed_at=T_DEADLINE,
                    available_at=T_TIMEOUT,
                    actual_cost=Decimal("0.20"),
                    evidence_sha256=SHA_B,
                )

if __name__ == "__main__":
    unittest.main()
