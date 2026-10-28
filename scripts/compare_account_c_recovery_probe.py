"""Predeclared PAPER-only recovery experiment; never a runtime policy."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autosport.paper_live_sandbox_campaign import (
    BlindPaperCampaignPlan, BlindPaperSandboxCampaign, PARLAY_ACTIONS,
    SINGLE_ACTIONS, parse_sandbox_live_payload,
)

STEPS = 180
START = datetime(2026, 10, 28, tzinfo=timezone.utc)
INTERVAL = 40


class BoundedRecoveryProbe(BlindPaperSandboxCampaign):
    PROBE_INTERVAL = INTERVAL

    def _admissible_actions(self, snapshot):
        base = super()._admissible_actions(snapshot)
        generation = self.policy.generation
        if base != frozenset({"WAIT"}) or generation == 0 or generation % self.PROBE_INTERVAL:
            return base
        slot = generation % self.plan.curriculum_period
        events = len({q.match_id for q in snapshot.quotes})
        if slot < self.plan.parlay_slots and events >= 2:
            family = PARLAY_ACTIONS
        elif slot < self.plan.curriculum_period - 1 and snapshot.quotes:
            family = SINGLE_ACTIONS
        elif slot == self.plan.curriculum_period - 1:
            family = (PARLAY_ACTIONS if events >= 2 else
                      SINGLE_ACTIONS if snapshot.quotes else frozenset())
        else:
            family = frozenset()
        if not family:
            return base
        estimates = {e.action_type: e for e in self.policy.estimates}
        selected = min(family, key=lambda name: (estimates[name].observations, name))
        return frozenset({selected})


def snapshots(scenario):
    result = []
    for i in range(STEPS + 1):
        if scenario == "loss":
            price = 100 + i * 2
        elif scenario == "favorable":
            price = 500 - i * 2
        elif scenario == "recovery":
            price = 150 + (i if i <= 90 else 180-i) * 2
        else:
            raise ValueError("unknown scenario")
        at = START + timedelta(seconds=i+1)
        rows = [{"match_id": f"fixture-{j}", "sport_key": "table_tennis",
                 "source": "pinnacle", "period_key": "FT", "market": "h2h",
                 "line": 0, "side": "home", "price": price+j*30,
                 "timestamp_ms": int(at.timestamp()*1000), "age_seconds": 0}
                for j in range(3)]
        result.append(parse_sandbox_live_payload(
            {"sport_key": "table_tennis", "period": "FT", "count": len(rows),
             "results": rows}, available_at=(at+timedelta(microseconds=500000)).isoformat()))
    return result


def compare():
    cases = []
    for scenario in ("loss", "favorable", "recovery"):
        data = snapshots(scenario)
        outcomes = {}
        for name, cls in (("baseline", BlindPaperSandboxCampaign), ("probe", BoundedRecoveryProbe)):
            plan = BlindPaperCampaignPlan(campaign_id=f"recovery-{scenario}",
                started_at=START.isoformat(), cutoff_at=(START+timedelta(hours=1)).isoformat())
            campaign = cls(plan, seed=11)
            for decision, outcome in zip(data, data[1:]):
                campaign.step(decision, outcome)
            diag = campaign.diagnostics()
            outcomes[name] = {"net_reward": diag["net_reward"],
                "maximum_drawdown": diag["maximum_drawdown"],
                "ticket_count": diag["ticket_count"], "wait_count": campaign.counters["WAIT"],
                "estimates": campaign.summary()["estimates"]}
        cases.append({"scenario": scenario, "snapshot_sha256": hashlib.sha256(
            "".join(s.snapshot_sha256 for s in data).encode()).hexdigest(), "results": outcomes})
    return {"schema": "autosport.account_c.recovery_probe.development.v1",
            "steps": STEPS, "probe_interval": INTERVAL, "max_probes": 4,
            "truth": "simulated", "forward_evidence_eligible": False,
            "promotion_authorized": False, "profitability_claim": False,
            "real_money_execution": False,
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "campaign_sha256": hashlib.sha256((Path(__file__).resolve().parents[1] /
                "src/autosport/paper_live_sandbox_campaign.py").read_bytes()).hexdigest(),
            "cases": cases}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare()
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True)+"\n", encoding="utf-8")
    print(json.dumps(result["cases"]))
