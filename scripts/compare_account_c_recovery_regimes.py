"""Seeded regime stress for research-only synthetic policies."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from datetime import timedelta
from pathlib import Path

from autosport.paper_live_sandbox_campaign import (
    BlindPaperCampaignPlan, BlindPaperSandboxCampaign, parse_sandbox_live_payload,
)
from compare_account_c_recovery_probe import BoundedRecoveryProbe, START
from compare_account_c_recovery_families import FamilyBalancedProbe

SEEDS = (211, 223, 227, 229)
STEPS = 180


def snapshots(regime: str, seed: int):
    if regime not in {"continuing_loss", "recovery"}:
        raise ValueError("unknown scenario")
    rng = random.Random(seed)
    price = 150
    switch = 60 + seed % 31
    result = []
    for i in range(STEPS + 1):
        if i:
            movement = rng.choice((0, 1, 2, 3))
            price += -movement if regime == "recovery" and i > switch else movement
            price = max(100, min(500, price))
        at = START + timedelta(seconds=i+1)
        rows = [{"match_id": f"fixture-{j}", "sport_key": "table_tennis",
                 "source": "pinnacle", "period_key": "FT", "market": "h2h",
                 "line": 0, "side": "home", "price": price+j*30,
                 "timestamp_ms": int(at.timestamp()*1000), "age_seconds": 0}
                for j in range(3)]
        result.append(parse_sandbox_live_payload(
            {"sport_key": "table_tennis", "period": "FT", "count": len(rows),
             "results": rows}, available_at=(at+timedelta(microseconds=500000)).isoformat()))
    return result, switch


def compare():
    cases = []
    for regime in ("continuing_loss", "recovery"):
        for seed in SEEDS:
            data, switch = snapshots(regime, seed)
            arms = {}
            for name, cls in (("baseline", BlindPaperSandboxCampaign),
                              ("probe_40", BoundedRecoveryProbe),
                              ("probe_37", FamilyBalancedProbe)):
                plan = BlindPaperCampaignPlan(campaign_id=f"regimes-{regime}-{seed}",
                    started_at=START.isoformat(), cutoff_at=(START+timedelta(hours=1)).isoformat())
                campaign = cls(plan, seed=seed)
                for observation, outcome in zip(data, data[1:]):
                    campaign.step(observation, outcome)
                diag = campaign.diagnostics()
                arms[name] = {"net_reward": diag["net_reward"],
                              "maximum_drawdown": diag["maximum_drawdown"],
                              "ticket_count": diag["ticket_count"],
                              "wait_count": campaign.counters["WAIT"]}
            cases.append({"regime": regime, "seed": seed, "switch_step": switch,
                          "snapshots_sha256": hashlib.sha256("".join(
                              x.snapshot_sha256 for x in data).encode()).hexdigest(), "arms": arms})
    base = Path(__file__).resolve().parent
    return {"schema": "autosport.account_c.recovery_regimes.development.v1",
            "seeds": list(SEEDS), "steps": STEPS, "truth": "simulated",
            "forward_evidence_eligible": False, "promotion_authorized": False,
            "profitability_claim": False, "real_money_execution": False,
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "probe_sha256": hashlib.sha256((base / "compare_account_c_recovery_probe.py").read_bytes()).hexdigest(),
            "family_sha256": hashlib.sha256((base / "compare_account_c_recovery_families.py").read_bytes()).hexdigest(),
            "campaign_sha256": hashlib.sha256((base.parent / "src/autosport/paper_live_sandbox_campaign.py").read_bytes()).hexdigest(),
            "cases": cases}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare()
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True)+"\n", encoding="utf-8")
    print(json.dumps(result["cases"]))
