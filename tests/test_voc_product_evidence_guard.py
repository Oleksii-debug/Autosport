from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.model_compute_router import ModelComputeRouterError, ModelComputeRouterStore
from autosport.voc_production_orchestrator import VOCProductionOrchestrator


def _fixture_module():
    path = Path(__file__).with_name("test_voc_production_orchestrator.py")
    spec = importlib.util.spec_from_file_location("_voc_product_fixture", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load VOC production fixture module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_FIXTURE = _fixture_module()


class VOCProductEvidenceGuardTests(unittest.TestCase):
    def _fixture(self):
        fixture = _FIXTURE.VOCProductionOrchestratorTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def test_run_pair_and_freeze_derives_exact_evidence_and_restart_is_idempotent(self):
        fixture = self._fixture()
        orchestrator = VOCProductionOrchestrator(
            fixture.router,
            fixture.receipt_path,
            decision_ledger=fixture.ledger,
        )
        calls = {"baseline": 0, "challenger": 0}

        def baseline_invoke():
            calls["baseline"] += 1
            return fixture.result(_FIXTURE.SHA_A, "LOCAL_ACTION", "0.1")

        def challenger_invoke():
            calls["challenger"] += 1
            return fixture.result(_FIXTURE.SHA_B, "CLOUD_ACTION", "0.2")

        with (
            patch(
                "autosport.model_compute_router._authority_now",
                side_effect=[_FIXTURE.T3, _FIXTURE.T4],
            ),
            patch(
                "autosport._voc_product_evidence_guard.production._now",
                return_value=_FIXTURE.T4,
            ),
        ):
            baseline, challenger, frozen = orchestrator.run_pair_and_freeze(
                request_id="request-1",
                baseline_invoke=baseline_invoke,
                challenger_invoke=challenger_invoke,
                quote_keys=("quote-a", "quote-b"),
                evaluation_id="evaluation-product-owned",
            )

        self.assertEqual(calls, {"baseline": 1, "challenger": 1})
        self.assertEqual(baseline["role"], "baseline")
        self.assertEqual(challenger["role"], "challenger")
        self.assertEqual(frozen["evaluation_id"], "evaluation-product-owned")
        self.assertEqual(len(frozen["decision_evidence_sha256"]), 64)
        self.assertEqual(len(frozen["admission_sha256"]), 64)

        records = fixture.ledger.verified_records()
        self.assertEqual(
            [record.action for record in records],
            [
                "VOC_ROUTE_CONTEXT",
                "VOC_PAIRED_ADMISSION",
                "VOC_PAIRED_SCORING_EVIDENCE",
            ],
        )
        scoring = records[-1]
        self.assertEqual(scoring.recorded_at, _FIXTURE.T4)
        self.assertEqual(scoring.observed_ts, _FIXTURE.T4)
        binding = scoring.payload["voc_binding"]
        evidence = scoring.payload["voc_scoring_evidence"]
        self.assertEqual(binding["baseline_output_sha256"], _FIXTURE.SHA_A)
        self.assertEqual(binding["challenger_output_sha256"], _FIXTURE.SHA_B)
        self.assertEqual(binding["baseline_action"], "LOCAL_ACTION")
        self.assertEqual(binding["challenger_action"], "CLOUD_ACTION")
        self.assertEqual(
            [sample["quote_key"] for sample in evidence["samples"]],
            ["quote-a", "quote-b"],
        )
        self.assertTrue(
            all(sample["baseline_compute_cost"] == "0.1" for sample in evidence["samples"])
        )
        self.assertTrue(
            all(sample["challenger_compute_cost"] == "0.2" for sample in evidence["samples"])
        )

        reopened = ModelComputeRouterStore(fixture.router_path)
        restarted = VOCProductionOrchestrator(
            reopened,
            fixture.receipt_path,
            decision_ledger=JsonlDecisionLedger(fixture.ledger_path),
        )
        with patch(
            "autosport._voc_product_evidence_guard.production._now",
            side_effect=AssertionError("restart must not restamp durable scoring evidence"),
        ):
            _, _, frozen_again = restarted.run_pair_and_freeze(
                request_id="request-1",
                baseline_invoke=baseline_invoke,
                challenger_invoke=challenger_invoke,
                quote_keys=("quote-a", "quote-b"),
                evaluation_id="evaluation-product-owned",
            )
        self.assertEqual(calls, {"baseline": 1, "challenger": 1})
        self.assertEqual(frozen_again, frozen)
        self.assertEqual(
            len(JsonlDecisionLedger(fixture.ledger_path).verified_records()),
            3,
        )

    def test_freeze_refuses_product_clock_that_predates_shadow_authority(self):
        fixture = self._fixture()
        orchestrator = VOCProductionOrchestrator(
            fixture.router,
            fixture.receipt_path,
            decision_ledger=fixture.ledger,
        )
        with patch(
            "autosport.model_compute_router._authority_now",
            side_effect=[_FIXTURE.T3, _FIXTURE.T4],
        ):
            orchestrator.run_pair(
                request_id="request-1",
                baseline_invoke=lambda: fixture.result(
                    _FIXTURE.SHA_A, "LOCAL_ACTION", "0.1"
                ),
                challenger_invoke=lambda: fixture.result(
                    _FIXTURE.SHA_B, "CLOUD_ACTION", "0.2"
                ),
            )

        with (
            patch(
                "autosport._voc_product_evidence_guard.production._now",
                return_value=_FIXTURE.T2,
            ),
            self.assertRaisesRegex(
                ModelComputeRouterError,
                "product clock predates canonical shadow availability",
            ),
        ):
            orchestrator.freeze_pair_scoring_evidence(
                request_id="request-1",
                quote_keys=("quote-a",),
                evaluation_id="evaluation-product-owned",
            )
        self.assertEqual(len(fixture.ledger.verified_records()), 2)

    def test_freeze_refuses_competing_caller_terminal_for_same_admission(self):
        fixture = self._fixture()
        orchestrator = VOCProductionOrchestrator(
            fixture.router,
            fixture.receipt_path,
            decision_ledger=fixture.ledger,
        )
        with patch(
            "autosport.model_compute_router._authority_now",
            side_effect=[_FIXTURE.T3, _FIXTURE.T4],
        ):
            orchestrator.run_pair(
                request_id="request-1",
                baseline_invoke=lambda: fixture.result(
                    _FIXTURE.SHA_A, "LOCAL_ACTION", "0.1"
                ),
                challenger_invoke=lambda: fixture.result(
                    _FIXTURE.SHA_B, "CLOUD_ACTION", "0.2"
                ),
            )

        precompute = fixture.router.get_voc_precompute_admission("request-1")
        self.assertIsNotNone(precompute)
        assert precompute is not None
        context_sha = precompute["decision_context_sha256"]
        fixture.ledger.append(
            DecisionRecord(
                replay_run_id="caller-forged-terminal",
                agent="caller",
                observed_ts=_FIXTURE.T4,
                action="CALLER_FORGED",
                payload={
                    "voc_binding": {
                        "decision_context_sha256": context_sha,
                        "baseline_output_sha256": "f" * 64,
                        "challenger_output_sha256": "0" * 64,
                    }
                },
                context_hash=precompute["decision_input_sha256"],
                decision_id="caller-forged-terminal",
                recorded_at=_FIXTURE.T4,
            )
        )

        with self.assertRaisesRegex(
            ModelComputeRouterError,
            "already has a different terminal record",
        ):
            orchestrator.freeze_pair_scoring_evidence(
                request_id="request-1",
                quote_keys=("quote-a",),
                evaluation_id="evaluation-product-owned",
            )


if __name__ == "__main__":
    unittest.main()
