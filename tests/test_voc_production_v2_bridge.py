from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.model_compute_router import (
    ComputeCandidate,
    ComputeRouteRequest,
    ComputeRoutingPolicy,
    ComputeTier,
    DataClassification,
    ModelComputeRouterStore,
)
from autosport.scientific_registry import ScientificRegistry
from autosport.voc_production_orchestrator import (
    VOCBackendResult,
    VOCProductionOrchestrator,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
T_PROTOCOL = "2026-09-19T23:59:00Z"
T0 = "2026-09-20T00:00:00Z"
T2 = "2026-09-20T00:00:02Z"
T3 = "2026-09-20T00:00:03Z"
T4 = "2026-09-20T00:00:04Z"
T10 = "2026-09-20T00:00:10Z"


@dataclass(frozen=True)
class RawScientificRecord:
    record_type: str
    record_id: str
    available_at: str
    payload: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return self.payload


class VOCProductionV2BridgeTests(unittest.TestCase):
    def test_registry_bound_v2_precompute_runs_pair_without_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            ledger = JsonlDecisionLedger(root / "decision-ledger.jsonl")
            context = DecisionRecord(
                replay_run_id="v2-context",
                agent="v2-production-test",
                observed_ts=T0,
                action="VOC_ROUTE_CONTEXT",
                payload={
                    "voc_current_context": {
                        "request_id": "request-v2",
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
                decision_id="decision-context-v2",
                recorded_at=T0,
            )
            context_sha = ledger.append(context)

            registry = ScientificRegistry.initialize_pristine(
                root / "scientific-registry.json"
            )
            evaluation_design = {
                "cohort_id": "cohort-1",
                "cohort_eligibility": {
                    "kind": "decision-ledger-window-v1",
                    "decision_recorded_from": T0,
                    "decision_recorded_through": T10,
                },
            }
            registry.append(
                RawScientificRecord(
                    record_type="ResearchProtocol",
                    record_id="protocol-1",
                    available_at=T_PROTOCOL,
                    payload={
                        "research_protocol_id": "protocol-1",
                        "protocol_sha256": SHA_D,
                        "binding": {
                            "evaluation_design": json.dumps(
                                evaluation_design,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        },
                    },
                )
            )

            router = ModelComputeRouterStore(root / "router.json")
            baseline = ComputeCandidate(
                candidate_id="baseline",
                tier=ComputeTier.LOCAL,
                backend_id="local-backend",
                model_id="baseline-model",
                config_sha256=SHA_A,
                capabilities=("forecast",),
                estimated_cost=Decimal("0.1"),
                estimated_latency_seconds=Decimal("1"),
            )
            challenger = ComputeCandidate(
                candidate_id="challenger",
                tier=ComputeTier.CLOUD,
                backend_id="cloud-backend",
                model_id="challenger-model",
                config_sha256=SHA_B,
                capabilities=("forecast",),
                estimated_cost=Decimal("0.2"),
                estimated_latency_seconds=Decimal("2"),
            )
            request = ComputeRouteRequest(
                request_id="request-v2",
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
                policy_id="policy-v2",
                policy_version=1,
                cloud_enabled=False,
            )
            with patch(
                "autosport.model_compute_router._authority_now",
                return_value=T0,
            ):
                router.route(
                    request,
                    (baseline, challenger),
                    policy,
                    as_of=T0,
                    voc_precompute_admission={
                        "admission_id": "admission-v2",
                        "research_protocol_id": "protocol-1",
                        "cohort_id": "cohort-1",
                        "baseline_candidate_id": "baseline",
                        "challenger_candidate_id": "challenger",
                        "sport_id": "table-tennis",
                        "league_id": "league-1",
                    },
                )

            before = router.get_voc_precompute_admission("request-v2")
            self.assertIsNotNone(before)
            assert before is not None
            self.assertEqual(before["schema_version"], 2)
            self.assertEqual(before["research_protocol_sha256"], SHA_D)
            bound_prefix = before["research_protocol_registry_prefix_sha256"]

            orchestrator = VOCProductionOrchestrator(
                router,
                root / "voc-production.json",
                decision_ledger=ledger,
            )
            with patch(
                "autosport.model_compute_router._authority_now",
                side_effect=[T3, T4],
            ):
                baseline_authority, challenger_authority = orchestrator.run_pair(
                    request_id="request-v2",
                    baseline_invoke=lambda: VOCBackendResult(
                        output_sha256=SHA_A,
                        action="LOCAL_ACTION",
                        abstained=False,
                        completed_at=T2,
                        available_at=T2,
                        actual_cost=Decimal("0.1"),
                        evidence_sha256=SHA_A,
                    ),
                    challenger_invoke=lambda: VOCBackendResult(
                        output_sha256=SHA_B,
                        action="CLOUD_ACTION",
                        abstained=False,
                        completed_at=T3,
                        available_at=T3,
                        actual_cost=Decimal("0.2"),
                        evidence_sha256=SHA_B,
                    ),
                )

            self.assertEqual(baseline_authority["role"], "baseline")
            self.assertEqual(challenger_authority["role"], "challenger")
            actions = [record.action for record in ledger.verified_records()]
            self.assertEqual(actions, ["VOC_ROUTE_CONTEXT", "VOC_PAIRED_ADMISSION"])

            after = router.get_voc_precompute_admission("request-v2")
            self.assertEqual(after, before)
            assert after is not None
            self.assertEqual(after["schema_version"], 2)
            self.assertEqual(
                after["research_protocol_registry_prefix_sha256"],
                bound_prefix,
            )


if __name__ == "__main__":
    unittest.main()
