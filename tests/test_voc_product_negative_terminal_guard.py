from __future__ import annotations

import importlib.util
import sys
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.decision_ledger import JsonlDecisionLedger
from autosport.model_compute_router import (
    ExecutionDisposition,
    ModelComputeRouterError,
    ModelComputeRouterStore,
)
from autosport.voc_production_orchestrator import VOCProductionOrchestrator


def _fixture_module():
    path = Path(__file__).with_name("test_voc_production_orchestrator.py")
    spec = importlib.util.spec_from_file_location("_voc_negative_product_fixture", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load VOC production fixture module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_FIXTURE = _fixture_module()


class VOCProductNegativeTerminalGuardTests(unittest.TestCase):
    def _fixture(self):
        fixture = _FIXTURE.VOCProductionOrchestratorTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def test_negative_terminal_re_resolves_execution_and_is_restart_idempotent(self):
        fixture = self._fixture()
        orchestrator = VOCProductionOrchestrator(
            fixture.router,
            fixture.receipt_path,
            decision_ledger=fixture.ledger,
        )
        admission_sha = orchestrator._ensure_pair_admission("request-1")
        execution = fixture.router.record_execution(
            execution_id="challenger-failed-1",
            request_id="request-1",
            completed_at=_FIXTURE.T2,
            available_at=_FIXTURE.T2,
            backend_id=fixture.challenger.backend_id,
            model_id=fixture.challenger.model_id,
            config_sha256=fixture.challenger.config_sha256,
            actual_cost=Decimal("0.2"),
            actual_latency_seconds=Decimal("2"),
            evidence_sha256=_FIXTURE.SHA_B,
            as_of=_FIXTURE.T2,
        )
        self.assertIs(execution.disposition, ExecutionDisposition.REJECTED_IDENTITY)
        self.assertEqual(execution.actual_cost, Decimal("0.2"))
        self.assertEqual(execution.actual_latency_seconds, Decimal("2"))

        closed = orchestrator.close_pair_negative_terminal(
            request_id="request-1",
            status="failed",
            execution_id=execution.execution_id,
            recorded_at=_FIXTURE.T3,
        )
        self.assertEqual(closed["admission_sha256"], admission_sha)
        self.assertEqual(closed["status"], "failed")
        self.assertEqual(closed["execution_id"], execution.execution_id)
        self.assertEqual(len(closed["terminal_sha256"]), 64)

        records = fixture.ledger.verified_records()
        self.assertEqual(
            [record.action for record in records],
            ["VOC_ROUTE_CONTEXT", "VOC_PAIRED_ADMISSION", "VOC_PAIRED_TERMINAL"],
        )
        terminal = records[-1].payload["voc_terminal"]
        self.assertEqual(terminal["schema_version"], 3)
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["execution_id"], execution.execution_id)
        self.assertEqual(terminal["execution_record_sha256"], execution.execution_record_sha256)
        self.assertTrue(terminal["authority_recorded_at"].endswith("Z"))

        reopened = ModelComputeRouterStore(fixture.router_path)
        restarted = VOCProductionOrchestrator(
            reopened,
            fixture.receipt_path,
            decision_ledger=JsonlDecisionLedger(fixture.ledger_path),
        )
        closed_again = restarted.close_pair_negative_terminal(
            request_id="request-1",
            status="failed",
            execution_id=execution.execution_id,
            recorded_at=_FIXTURE.T3,
        )
        self.assertEqual(closed_again, closed)
        self.assertEqual(
            len(JsonlDecisionLedger(fixture.ledger_path).verified_records()),
            3,
        )

    def test_existing_negative_terminal_refuses_status_rewrite(self):
        fixture = self._fixture()
        orchestrator = VOCProductionOrchestrator(
            fixture.router,
            fixture.receipt_path,
            decision_ledger=fixture.ledger,
        )
        orchestrator._ensure_pair_admission("request-1")
        execution = fixture.router.record_execution(
            execution_id="challenger-failed-2",
            request_id="request-1",
            completed_at=_FIXTURE.T2,
            available_at=_FIXTURE.T2,
            backend_id=fixture.challenger.backend_id,
            model_id=fixture.challenger.model_id,
            config_sha256=fixture.challenger.config_sha256,
            actual_cost=Decimal("0.2"),
            actual_latency_seconds=Decimal("2"),
            evidence_sha256=_FIXTURE.SHA_B,
            as_of=_FIXTURE.T2,
        )
        orchestrator.close_pair_negative_terminal(
            request_id="request-1",
            status="failed",
            execution_id=execution.execution_id,
            recorded_at=_FIXTURE.T3,
        )

        with self.assertRaisesRegex(
            ModelComputeRouterError,
            "conflicts with durable product authority",
        ):
            orchestrator.close_pair_negative_terminal(
                request_id="request-1",
                status="timeout",
                execution_id=execution.execution_id,
                recorded_at=_FIXTURE.T3,
            )


if __name__ == "__main__":
    unittest.main()
