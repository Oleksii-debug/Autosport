"""Compare two bounded synthetic recovery schedules; not a runtime policy."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import timedelta
from pathlib import Path

from autosport.paper_live_sandbox_campaign import BlindPaperCampaignPlan, BlindPaperSandboxCampaign
from compare_account_c_recovery_probe import BoundedRecoveryProbe, START, STEPS, snapshots


class FamilyBalancedProbe(BoundedRecoveryProbe):
    PROBE_INTERVAL = 37


def compare():
    cases = []
    for scenario in ("loss", "favorable", "recovery"):
        data = snapshots(scenario)
        arms = {}
        for name, cls in (("baseline", BlindPaperSandboxCampaign),
                          ("probe_40", BoundedRecoveryProbe),
                          ("probe_37", FamilyBalancedProbe)):
            plan = BlindPaperCampaignPlan(campaign_id=f"families-{scenario}",
                started_at=START.isoformat(), cutoff_at=(START+timedelta(hours=1)).isoformat())
            campaign = cls(plan, seed=11)
            for observation, outcome in zip(data, data[1:]):
                campaign.step(observation, outcome)
            diag = campaign.diagnostics()
            arms[name] = {"net_reward": diag["net_reward"],
                          "maximum_drawdown": diag["maximum_drawdown"],
                          "ticket_count": diag["ticket_count"],
                          "wait_count": campaign.counters["WAIT"],
                          "cohorts": diag["cohorts"],
                          "estimates": campaign.summary()["estimates"]}
        cases.append({"scenario": scenario, "snapshots_sha256": hashlib.sha256(
            "".join(s.snapshot_sha256 for s in data).encode()).hexdigest(), "arms": arms})
    base = Path(__file__).resolve().parent
    return {"schema": "autosport.account_c.family_recovery.development.v1",
            "steps": STEPS, "intervals": [40, 37], "truth": "simulated",
            "forward_evidence_eligible": False, "promotion_authorized": False,
            "profitability_claim": False, "real_money_execution": False,
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "probe_source_sha256": hashlib.sha256((base / "compare_account_c_recovery_probe.py").read_bytes()).hexdigest(),
            "campaign_source_sha256": hashlib.sha256((base.parent / "src/autosport/paper_live_sandbox_campaign.py").read_bytes()).hexdigest(),
            "cases": cases}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare()
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True)+"\n", encoding="utf-8")
    print(json.dumps([{ "scenario": x["scenario"], "outcomes": {k:{f:v[f] for f in
        ("net_reward", "maximum_drawdown", "ticket_count")} for k,v in x["arms"].items()}}
        for x in result["cases"]]))
