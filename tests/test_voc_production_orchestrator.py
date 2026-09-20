from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

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
T0 = "2026-09-20T00:00:00Z"
T2 = "2026-09-20T00:00:02Z"
T3 = "2026-09-20T00:00:03Z"
T4 = "2026-09-20T00:00:04Z"
T10 = "2026-09-20T00:00:10Z"


class VOCProductionOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.router_path = root / "router.json"
        self.receipt_path = root / "voc-production.json"
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
            decision_evidence_sha256=SHA_C,
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

    @staticmethod
    def result(output_sha256: str, action: str, cost: str) -> VOCBackendResult:
        return VOCBackendResult(
            output_sha256=output_sha256,
            action=action,
            abstained=False,
            completed_at=T2,
            available_at=T2,
            actual_cost=Decimal(cost),
            evidence_sha256=output_sha256,
        )

    def test_pair_is_not_reinvoked_after_publication_or_restart(self) -> None:
        orchestrator = VOCProductionOrchestrator(
            self.router,
            self.receipt_path,
        )
        calls = {"baseline": 0, "challenger": 0}

        def baseline_invoke() -> VOCBackendResult:
            calls["baseline"] += 1
            return self.result(SHA_A, "LOCAL_ACTION", "0.1")

        def challenger_invoke() -> VOCBackendResult:
            calls["challenger"] += 1
            return self.result(SHA_B, "CLOUD_ACTION", "0.2")

        with patch(
            "autosport.model_compute_router._authority_now",
            side_effect=[T3, T4],
        ):
            baseline, challenger = orchestrator.run_pair(
                request_id="request-1",
                baseline_invoke=baseline_invoke,
                challenger_invoke=challenger_invoke,
            )
        self.assertEqual(calls, {"baseline": 1, "challenger": 1})
        self.assertEqual(baseline["role"], "baseline")
        self.assertEqual(challenger["role"], "challenger")

        orchestrator.run_pair(
            request_id="request-1",
            baseline_invoke=baseline_invoke,
            challenger_invoke=challenger_invoke,
        )
        self.assertEqual(calls, {"baseline": 1, "challenger": 1})

        reopened = ModelComputeRouterStore(self.router_path)
        restarted = VOCProductionOrchestrator(reopened, self.receipt_path)
        restarted.run_pair(
            request_id="request-1",
            baseline_invoke=baseline_invoke,
            challenger_invoke=challenger_invoke,
        )
        self.assertEqual(calls, {"baseline": 1, "challenger": 1})

    def test_restart_reconciles_receipt_after_authority_was_published(self) -> None:
        orchestrator = VOCProductionOrchestrator(
            self.router,
            self.receipt_path,
        )
        calls = 0

        def baseline_invoke() -> VOCBackendResult:
            nonlocal calls
            calls += 1
            return self.result(SHA_A, "LOCAL_ACTION", "0.1")

        original_persist = orchestrator._persist_map
        persist_calls = 0

        def interrupt_final_receipt(values: object) -> None:
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls == 3:
                raise RuntimeError("receipt publication interrupted")
            original_persist(values)  # type: ignore[arg-type]

        with (
            patch.object(
                orchestrator,
                "_persist_map",
                side_effect=interrupt_final_receipt,
            ),
            patch(
                "autosport.model_compute_router._authority_now",
                return_value=T3,
            ),
            self.assertRaisesRegex(RuntimeError, "receipt publication interrupted"),
        ):
            orchestrator.run_role(
                request_id="request-1",
                role="baseline",
                invoke=baseline_invoke,
            )

        self.assertEqual(calls, 1)
        published = self.router.get_voc_shadow_execution("request-1", "baseline")
        self.assertIsNotNone(published)
        receipts = orchestrator._load()
        self.assertEqual(receipts[("request-1", "baseline")]["state"], "SUCCEEDED")

        reopened = ModelComputeRouterStore(self.router_path)
        restarted = VOCProductionOrchestrator(reopened, self.receipt_path)
        recovered = restarted.run_role(
            request_id="request-1",
            role="baseline",
            invoke=baseline_invoke,
        )

        self.assertEqual(calls, 1)
        self.assertEqual(recovered, published)
        receipts = restarted._load()
        receipt = receipts[("request-1", "baseline")]
        self.assertEqual(receipt["state"], "PUBLISHED")
        self.assertEqual(receipt["authority_sha256"], recovered["authority_sha256"])

    def test_uncertain_backend_failure_is_durable_and_never_blindly_resent(self) -> None:
        orchestrator = VOCProductionOrchestrator(
            self.router,
            self.receipt_path,
        )
        calls = 0

        def uncertain() -> VOCBackendResult:
            nonlocal calls
            calls += 1
            raise RuntimeError("transport lost after provider may have accepted request")

        with self.assertRaisesRegex(RuntimeError, "transport lost"):
            orchestrator.run_role(
                request_id="request-1",
                role="challenger",
                invoke=uncertain,
            )
        self.assertEqual(calls, 1)

        restarted = VOCProductionOrchestrator(self.router, self.receipt_path)
        with self.assertRaisesRegex(
            ModelComputeRouterError,
            "uncertain; refusing blind resend",
        ):
            restarted.run_role(
                request_id="request-1",
                role="challenger",
                invoke=uncertain,
            )
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
