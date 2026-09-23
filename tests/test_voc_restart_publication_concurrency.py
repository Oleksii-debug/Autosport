from __future__ import annotations

import tempfile
import threading
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


class VOCRestartPublicationConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.router_path = root / "router.json"
        self.receipt_path = root / "voc-production.json"
        self.ledger_path = root / "decision-ledger.jsonl"
        self.ledger = JsonlDecisionLedger(self.ledger_path)
        context = DecisionRecord(
            replay_run_id="restart-concurrency-context",
            agent="restart-concurrency-test",
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
            decision_id="restart-concurrency-context-1",
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
    def baseline_result() -> VOCBackendResult:
        return VOCBackendResult(
            output_sha256=SHA_A,
            action="LOCAL_ACTION",
            abstained=False,
            completed_at=T2,
            available_at=T2,
            actual_cost=Decimal("0.1"),
            evidence_sha256=SHA_A,
        )

    def _persist_baseline_succeeded_without_shadow(self) -> None:
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
                invoke=self.baseline_result,
                _admission_sha256=self.admission_sha,
            )
        receipt = self.orchestrator._load()[("request-1", "baseline")]
        self.assertEqual(receipt["state"], "SUCCEEDED")

    def test_recovery_never_exposes_historical_request_as_process_live(self) -> None:
        self._persist_baseline_succeeded_without_shadow()
        reopened = ModelComputeRouterStore(self.router_path)
        restarted = VOCProductionOrchestrator(
            reopened,
            self.receipt_path,
            decision_ledger=JsonlDecisionLedger(self.ledger_path),
        )

        entered_append = threading.Event()
        release_append = threading.Event()
        recovery_errors: list[BaseException] = []
        recovered: list[dict[str, object]] = []
        original_append = reopened._append_voc_shadow_authority

        def blocked_append(record: object) -> None:
            entered_append.set()
            if not release_append.wait(timeout=5):
                raise AssertionError("test did not release restart publication")
            original_append(record)  # type: ignore[arg-type]

        def recover_baseline() -> None:
            try:
                recovered.append(
                    restarted.run_role(
                        request_id="request-1",
                        role="baseline",
                        invoke=lambda: (_ for _ in ()).throw(
                            AssertionError("baseline backend was reinvoked")
                        ),
                        _admission_sha256=self.admission_sha,
                    )
                )
            except BaseException as exc:  # surfaced on the owning test thread below
                recovery_errors.append(exc)

        challenger_calls = 0

        def challenger_invoke() -> VOCBackendResult:
            nonlocal challenger_calls
            challenger_calls += 1
            return VOCBackendResult(
                output_sha256=SHA_B,
                action="CLOUD_ACTION",
                abstained=False,
                completed_at=T2,
                available_at=T2,
                actual_cost=Decimal("0.2"),
                evidence_sha256=SHA_B,
            )

        with (
            patch.object(
                reopened,
                "_append_voc_shadow_authority",
                side_effect=blocked_append,
            ),
            patch(
                "autosport.model_compute_router._authority_now",
                return_value=T3,
            ),
        ):
            thread = threading.Thread(target=recover_baseline, daemon=True)
            thread.start()
            self.assertTrue(
                entered_append.wait(timeout=5),
                "baseline recovery did not reach shadow publication",
            )
            try:
                self.assertNotIn("request-1", reopened._live_request_ids)
                with self.assertRaisesRegex(
                    ModelComputeRouterError,
                    "cannot begin new backend work",
                ):
                    restarted.run_role(
                        request_id="request-1",
                        role="challenger",
                        invoke=challenger_invoke,
                        _admission_sha256=self.admission_sha,
                    )
                self.assertEqual(challenger_calls, 0)

                with self.assertRaisesRegex(
                    ModelComputeRouterError,
                    "execution-frozen after restart",
                ):
                    reopened.record_execution(
                        execution_id="historical-execution",
                        request_id="request-1",
                        completed_at=T2,
                        available_at=T2,
                        backend_id="local-backend",
                        model_id="baseline-model",
                        config_sha256=SHA_A,
                        actual_cost=Decimal("0.1"),
                        actual_latency_seconds=Decimal("2"),
                        evidence_sha256=SHA_D,
                        as_of=T3,
                    )
            finally:
                release_append.set()
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive(), "baseline recovery thread did not finish")
        if recovery_errors:
            raise recovery_errors[0]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["role"], "baseline")
        self.assertEqual(recovered[0]["output_sha256"], SHA_A)
        self.assertNotIn("request-1", reopened._live_request_ids)
        self.assertIsNone(
            reopened.get_voc_shadow_execution("request-1", "challenger")
        )


if __name__ == "__main__":
    unittest.main()
