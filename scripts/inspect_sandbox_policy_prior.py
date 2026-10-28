"""Export a read-only simulated policy prior; this does not import or promote it."""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from decimal import Decimal
from pathlib import Path

from audit_account_c_artifact import audit
from autosport.json_integrity import strict_json_loads
from autosport.paper_live_sandbox_campaign import (
    ACTION_TYPES, BlindPaperCampaignPlan, BlindPaperSandboxCampaign, SIMULATION_MODEL_ID,
)
from autosport.transparent_bandit_policy import BanditPolicyState


def inspect(path: Path, *, expected_artifact_sha256: str):
    if type(expected_artifact_sha256) is not str or len(expected_artifact_sha256) != 64:
        raise ValueError("an independently obtained expected artifact SHA-256 is required")
    verified = audit(path)
    if verified["artifact_sha256"] != expected_artifact_sha256:
        raise ValueError("artifact SHA-256 does not match expected identity")
    with zipfile.ZipFile(path) as archive:
        state = strict_json_loads(archive.read("campaign_state.json").decode("utf-8"))
    raw = state["plan"]
    if type(raw) is not dict or raw.get("simulation_model_id") != SIMULATION_MODEL_ID:
        raise ValueError("unsupported simulation model")
    plan = BlindPaperCampaignPlan(
        campaign_id=raw["campaign_id"], started_at=raw["started_at"], cutoff_at=raw["cutoff_at"],
        initial_bankroll=Decimal(raw["initial_bankroll"]), currency=raw["currency"],
        max_stake=Decimal(raw["max_stake"]), curriculum_period=raw["curriculum_period"],
        parlay_slots=raw["parlay_slots"],
    )
    if plan.payload() != raw:
        raise ValueError("campaign plan is not canonical")
    policy = BanditPolicyState.from_payload(state["policy"])
    probe = BlindPaperSandboxCampaign(plan, seed=state["seed"])
    if (policy.environment_id != probe.environment.environment_id
        or policy.protocol_id != probe.policy.protocol_id
        or policy.config_sha256 != plan.plan_sha256
        or policy.seed != state["seed"]
        or {estimate.action_type for estimate in policy.estimates} != ACTION_TYPES
        or policy.policy_id != state["summary"]["policy_id"]):
        raise ValueError("policy identity or campaign binding mismatch")
    payload = {"schema": "autosport.sandbox_policy_prior.inspection.v1",
               "source_artifact_sha256": verified["artifact_sha256"],
               "source_policy_id": policy.policy_id,
               "source_environment_id": policy.environment_id,
               "source_plan_sha256": plan.plan_sha256,
               "simulation_model_id": SIMULATION_MODEL_ID,
               "source_generation": policy.generation,
               "action_estimates": verified["action_estimates"],
               "truth": "simulated", "paper_only": True,
               "real_money_execution": False, "forward_evidence_eligible": False,
               "promotion_authorized": False, "profitability_claim": False,
               "source_authentication_independently_verified": False,
               "new_policy_created": False, "desktop_import_performed": False}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    payload["inspection_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    print(json.dumps(inspect(args.artifact, expected_artifact_sha256=args.expected_sha256), indent=2, sort_keys=True))
