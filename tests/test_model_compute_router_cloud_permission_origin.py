"""Regression fence for caller-mintable cloud permission.

This test consumes the canonical router fixtures and deliberately publishes a
legacy V2 VOC context with no product-owned policy/backend permission. Caller
intent flags alone must therefore fail closed without creating a second router,
permission store, VOC authority, or privacy registry.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import runpy
import tempfile
import unittest


_HELPERS = runpy.run_path(
    str(Path(__file__).with_name("test_model_compute_router.py"))
)

ComputeTier = _HELPERS["ComputeTier"]
VOCEvaluationStore = _HELPERS["VOCEvaluationStore"]
_FixtureCanonicalVOCResolver = _HELPERS["_FixtureCanonicalVOCResolver"]
candidate = _HELPERS["candidate"]
policy = _HELPERS["policy"]
request = _HELPERS["request"]
route_compute = _HELPERS["route_compute"]
slow_observation = _HELPERS["slow_observation"]
voc = _HELPERS["voc"]
T1 = _HELPERS["T1"]
SHA_B = _HELPERS["SHA_B"]


class CloudPermissionOriginTests(unittest.TestCase):
    def test_caller_flags_cannot_mint_positive_cloud_authority(self) -> None:
        """Valid scientific/privacy evidence still needs product cloud permission."""

        local = candidate()
        cloud = candidate(
            "cloud",
            tier=ComputeTier.CLOUD,
            backend_id="permitted-cloud",
            model_id="challenger-v2",
            config_sha256=SHA_B,
            cost="5",
            latency="4",
        )
        candidates = (local, cloud)
        observation = slow_observation()
        route_request = request(
            request_id="req-caller-cloud-permission",
            allow_cloud=True,
        )
        route_policy = policy(cloud_enabled=True)

        with tempfile.TemporaryDirectory() as directory:
            canonical = _FixtureCanonicalVOCResolver()
            voc_store = VOCEvaluationStore(
                Path(directory) / "voc-evaluations.json",
                canonical_authority_resolver=canonical,
            )
            evidence = voc(evidence_id="voc-caller-cloud-permission")
            self.assertIsNotNone(evidence.evaluation)
            canonical.publish(evidence.evaluation)
            voc_store.record(evidence.evaluation)

            # Match the parent #1782 product-owned VOC decision context,
            # including its newly canonical data classification.  Deliberately
            # do not publish any product-issued cloud/network permission:
            # today no such authority is consumed by route_compute().
            context = {
                "request_id": route_request.request_id,
                "decision_input_sha256": route_request.decision_input_sha256,
                "task_class": route_request.required_capability,
                "data_classification": route_request.data_classification.value,
                "sport_id": observation.sport_id,
                "league_id": observation.league_id,
                "regime_id": route_request.voc_regime_id,
                "urgency_id": route_request.voc_urgency_id,
                "contradiction_state": route_request.voc_contradiction_state,
            }
            context_sha256 = hashlib.sha256(
                json.dumps(
                    context,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            canonical.publish_context(context_sha256, context)
            route_request = replace(
                route_request,
                decision_evidence_sha256=context_sha256,
            )

            decision = route_compute(
                route_request,
                candidates,
                route_policy,
                as_of=T1,
                voc_evidence=evidence,
                voc_evaluation_store=voc_store,
                domain_observation=observation,
            )

        self.assertIsNot(decision.tier, ComputeTier.CLOUD)
        self.assertIn("product cloud permission authority", decision.reason)


if __name__ == "__main__":
    unittest.main()
