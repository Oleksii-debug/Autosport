from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

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
from autosport.voc_production_orchestrator import (
    VOCBackendResult,
    VOCProductionOrchestrator,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
T0 = "2026-09-20T00:00:00Z"
T2 = "2026-09-20T00:00:02Z"
T3 = "2026-09-20T00:00:03Z"
T10 = "2026-09-20T00:00:10Z"


class VOCRestartPublicationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.router_path = root / "router.json"
        self.receipt_path = root / "voc-production.json"
        self.ledger_path = root / "decision-ledger.jsonl"
        self.ledger = JsonlDecisionLedger(self.ledger_path)
        context = DecisionRecord(
            replay_run_id="restart-context",
            agent="restart-publication-test",
            observed_ts=T0,
            action="VOC_ROUTE_CONTEXT",
            payload={
                "voc_current_context": {
                    "request_id": "request-1",
                    "decision_input_sha256": SHA_C,
                    "task_class": "forecast",
                    "sport_id": "table-tennis",
                    "league_id": "league-1",
                    "regime_id": "regime-1",
                    "urgency_id": "routine",
                    "contradiction_state": "none",
                }
            },
            context_hash=SHA_C,
            decision_id="restart-context-1",
            recorded_at=T0,
        )
        context_sha = self.ledger.append(context)
        self.router = ModelComputeRouterStore(self.router_path)
        self.baseline = ComputeCandidate(
            candidate_id="baseline",
            tier=ComputeTier.LOCAL,
            backend_id="local-backend",
            model_id="baseline-model",
            config_sha256=SHA_A,
            capabilities=("forecast",),
            estimated_cost=Decimal("0.1"),
            estimated_latency_seconds=Decimal("1"),
        )
        self.challenger = ComputeCandidate(
            candidate_id="challenger",
            tier=ComputeTier.CLOUD,
            backend_id="cloud-backend",
            model_id="challenger-model",
            config_sha256=SHA_B,
            capabilities=("forecast",),
            estimated_cost=Decimal("0.2"),
            estimated_latency_seconds=Decimal("1"),
        )
        request = ComputeRouteRequest(
            request_id="request-1",
            created_at=T0,
            decision_deadline=T10,
            required_capability="forecast",
            data_classification=DataClassification.PUBLIC,
            allow_cloud=False,
            max_cost=Decimal("10"),
            response_ttl_seconds=Decimal("10"),
            baseline_candidate_id="baseline",
            cloud_candidate_id=None,
            decision_input_sha256=SHA_C,
            decision_evidence_sha256=context_sha,
            voc_regime_id="regime-1",
            voc_urgency_id="routine",
            voc_contradiction_state="none",
        )
        policy = ComputeRoutingPolicy(
            policy_id="policy-1",
            policy_version=1,
            cloud_enabled=False,
        )
        with patch(
            "autosport.model_compute_router._authority_now",
            return_value=T0,
        ):
            self.router.route(
                request,
                (self.baseline, self.challenger),
                policy,
                as_of=T0,
                voc_precompute_admission={
                    "admission_id": "admission-1",
                    "research_protocol_id": "protocol-1",
                    "cohort_id": "cohort-1",
                    "baseline_candidate_id": "baseline",
                    "challenger_candidate_id": "challenger",
                    "sport_id": "table-tennis",
                    "league_id": "league-1",
                },
            )
        self.orchestrator = VOCProductionOrchestrator(
            self.router,
            self.receipt_path,
            decision_ledger=self.ledger,
        )
        self.admission_sha = self.orchestrator._ensure_pair_admission("request-1")

    @staticmethod
    def result() -> VOCBackendResult:
        return VOCBackendResult(
            output_sha256=SHA_A,
            action="LOCAL_ACTION",
            abstained=False,
            completed_at=T2,
            available_at=T2,
            actual_cost=Decimal("0.1"),
            evidence_sha256=SHA_A,
        )

    def _persist_succeeded_without_shadow(self) -> int:
        calls = 0

        def invoke() -> VOCBackendResult:
            nonlocal calls
            calls += 1
            return self.result()

        with (
            patch(
                "autosport.voc_production_orchestrator._now",
                return_value=T0,
            ),
            patch.object(
                self.router,
                "record_voc_shadow_execution",
                side_effect=RuntimeError("crash before shadow publication"),
            ),
            self.assertRaisesRegex(RuntimeError, "crash before shadow publication"),
        ):
            self.orchestrator.run_role(
                request_id="request-1",
                role="baseline",
                invoke=invoke,
                _admission_sha256=self.admission_sha,
            )
        self.assertEqual(calls, 1)
        receipt = self.orchestrator._load()[("request-1", "baseline")]
        self.assertEqual(receipt["state"], "SUCCEEDED")
        self.assertIsNone(self.router.get_voc_shadow_execution("request-1", "baseline"))
        return calls

    def test_exact_succeeded_result_publishes_after_real_router_restart(self) -> None:
        calls = self._persist_succeeded_without_shadow()
        reopened = ModelComputeRouterStore(self.router_path)
        self.assertNotIn("request-1", reopened._live_request_ids)
        restarted = VOCProductionOrchestrator(
            reopened,
            self.receipt_path,
            decision_ledger=JsonlDecisionLedger(self.ledger_path),
        )

        def must_not_reinvoke() -> VOCBackendResult:
            nonlocal calls
            calls += 1
            raise AssertionError("backend was reinvoked")

        with patch(
            "autosport.model_compute_router._authority_now",
            return_value=T3,
        ):
            recovered = restarted.run_role(
                request_id="request-1",
                role="baseline",
                invoke=must_not_reinvoke,
                _admission_sha256=self.admission_sha,
            )

        self.assertEqual(calls, 1)
        self.assertEqual(recovered["role"], "baseline")
        self.assertEqual(recovered["output_sha256"], SHA_A)
        self.assertNotIn("request-1", reopened._live_request_ids)
        receipt = restarted._load()[("request-1", "baseline")]
        self.assertEqual(receipt["state"], "PUBLISHED")
        self.assertEqual(receipt["authority_sha256"], recovered["authority_sha256"])

        replayed = restarted.run_role(
            request_id="request-1",
            role="baseline",
            invoke=must_not_reinvoke,
            _admission_sha256=self.admission_sha,
        )
        self.assertEqual(replayed, recovered)
        self.assertEqual(calls, 1)

    def test_restart_without_preexisting_receipt_refuses_before_backend(self) -> None:
        reopened = ModelComputeRouterStore(self.router_path)
        restarted = VOCProductionOrchestrator(
            reopened,
            self.receipt_path,
            decision_ledger=JsonlDecisionLedger(self.ledger_path),
        )
        calls = 0

        def invoke() -> VOCBackendResult:
            nonlocal calls
            calls += 1
            return self.result()

        with self.assertRaisesRegex(
            ModelComputeRouterError,
            "cannot begin new backend work",
        ):
            restarted.run_role(
                request_id="request-1",
                role="baseline",
                invoke=invoke,
                _admission_sha256=self.admission_sha,
            )
        self.assertEqual(calls, 0)
        self.assertNotIn(("request-1", "baseline"), restarted._load())

    def test_mismatched_durable_receipt_cannot_gain_restart_publication(self) -> None:
        calls = self._persist_succeeded_without_shadow()
        receipts = self.orchestrator._load()
        receipts[("request-1", "baseline")]["route_record_sha256"] = SHA_D
        self.orchestrator._persist_map(receipts)

        reopened = ModelComputeRouterStore(self.router_path)
        restarted = VOCProductionOrchestrator(
            reopened,
            self.receipt_path,
            decision_ledger=JsonlDecisionLedger(self.ledger_path),
        )

        def must_not_reinvoke() -> VOCBackendResult:
            nonlocal calls
            calls += 1
            raise AssertionError("backend was reinvoked")

        with self.assertRaisesRegex(
            ModelComputeRouterError,
            "receipt route authority mismatch",
        ):
            restarted.run_role(
                request_id="request-1",
                role="baseline",
                invoke=must_not_reinvoke,
                _admission_sha256=self.admission_sha,
            )
        self.assertEqual(calls, 1)
        self.assertIsNone(reopened.get_voc_shadow_execution("request-1", "baseline"))


if __name__ == "__main__":
    unittest.main()
