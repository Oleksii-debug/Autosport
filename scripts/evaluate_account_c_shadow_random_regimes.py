"""Exploratory seeded synthetic regime comparison, not forward evidence."""
from __future__ import annotations

import hashlib
import json
import random
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from compare_account_c_recovery_probe import START
from compare_account_c_shadow_recovery import ShadowRecoveryCapped
from autosport.paper_live_sandbox_campaign import (
    BlindPaperCampaignPlan, BlindPaperSandboxCampaign, parse_sandbox_live_payload,
)

SEEDS = tuple(range(20))
STEPS = 120


def snapshots(seed: int):
    rng = random.Random(seed)
    price = 200
    result = []
    for i in range(STEPS + 1):
        # The first 40 moves are adverse. Later direction changes in 15-step
        # regimes with noise; outcomes are synthetic quote moves, not matches.
        if i:
            if i < 40:
                price += rng.randint(1, 4)
            else:
                if (i - 40) % 15 == 0:
                    direction = rng.choice((-1, 1))
                price = max(30, price + direction * rng.randint(1, 4) + rng.choice((-2, 0, 2)))
        at = START + timedelta(seconds=i + 1)
        rows = [{"match_id": f"fixture-{j}", "sport_key": "table_tennis",
                 "source": "pinnacle", "period_key": "FT", "market": "h2h",
                 "line": 0, "side": "home", "price": price + j * 30,
                 "timestamp_ms": int(at.timestamp() * 1000), "age_seconds": 0}
                for j in range(3)]
        result.append(parse_sandbox_live_payload(
            {"sport_key": "table_tennis", "period": "FT", "count": len(rows), "results": rows},
            available_at=(at + timedelta(microseconds=500000)).isoformat()))
    return result


def compare():
    cases = []
    for seed in SEEDS:
        data = snapshots(seed)
        outcomes = {}
        for name, cls in (("baseline", BlindPaperSandboxCampaign), ("capped", ShadowRecoveryCapped)):
            plan = BlindPaperCampaignPlan(campaign_id=f"random-{seed}",
                started_at=START.isoformat(), cutoff_at=(START + timedelta(hours=1)).isoformat())
            campaign = cls(plan, seed=11)
            for decision, outcome in zip(data, data[1:]):
                campaign.step(decision, outcome)
            diagnostic = campaign.diagnostics()
            outcomes[name] = {"net_reward": diagnostic["net_reward"],
                              "drawdown": diagnostic["maximum_drawdown"],
                              "tickets": diagnostic["ticket_count"],
                              "post_readmission_tickets": getattr(campaign, "tickets_after_readmission", 0)}
        cases.append({"seed": seed,
            "snapshot_sha256": hashlib.sha256("".join(s.snapshot_sha256 for s in data).encode()).hexdigest(),
            "outcomes": outcomes})
    deltas = [Decimal(c["outcomes"]["capped"]["net_reward"]) -
              Decimal(c["outcomes"]["baseline"]["net_reward"]) for c in cases]
    risks = [Decimal(c["outcomes"]["capped"]["drawdown"]) -
             Decimal(c["outcomes"]["baseline"]["drawdown"]) for c in cases]
    return {"schema": "autosport.account_c.shadow_random_regimes.research.v1",
            "seeds": list(SEEDS), "steps": STEPS, "truth": "simulated",
            "profitability_claim": False, "promotion_authorized": False,
            "forward_evidence_eligible": False, "real_money_execution": False,
            "higher_net_cases": sum(x > 0 for x in deltas),
            "lower_net_cases": sum(x < 0 for x in deltas),
            "unchanged_net_cases": sum(x == 0 for x in deltas),
            "larger_drawdown_cases": sum(x > 0 for x in risks),
            "maximum_additional_drawdown": str(max(risks)),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "shadow_source_sha256": hashlib.sha256((Path(__file__).resolve().parent /
                "compare_account_c_shadow_recovery.py").read_bytes()).hexdigest(),
            "cases": cases}


if __name__ == "__main__":
    print(json.dumps(compare(), indent=2, sort_keys=True))
