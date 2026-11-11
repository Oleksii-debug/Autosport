"""Paired synthetic selection regression; never sporting or profitability evidence.

Every contender consumes identical pre-generated snapshots. The baseline reproduces
the fixed family alternation from 2c3cb80c. No result selects parameters, edits the
policy, or promotes a contender. All scenarios, including losses, are exported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from autosport.integrity import atomic_write_json
from autosport.paper_live_sandbox_campaign import (
    ACTION_TYPES,
    PARLAY_ACTIONS,
    SINGLE_ACTIONS,
    BlindPaperCampaignPlan,
    BlindPaperSandboxCampaign,
    parse_sandbox_live_payload,
)

SCENARIOS = ("rising", "falling", "flat", "alternating", "regime_change", "random_walk")
SEEDS = (11, 29, 47)
STEPS = 120
START = datetime(2026, 10, 27, tzinfo=timezone.utc)


class FixedAlternation(BlindPaperSandboxCampaign):
    """Only the historical action admission differs; economics are shared."""

    def _admissible_actions(self, snapshot):
        slot = self.policy.generation % self.plan.curriculum_period
        if slot < self.plan.parlay_slots:
            return frozenset({"PARLAY_2_LOW_ODDS" if slot % 2 == 0 else "PARLAY_2_HIGH_ODDS"})
        if slot < self.plan.curriculum_period - 1:
            return frozenset({"SINGLE_LOW_ODDS" if (slot - self.plan.parlay_slots) % 2 == 0 else "SINGLE_HIGH_ODDS"})
        return ACTION_TYPES


def snapshots(scenario: str, seed: int):
    rng = random.Random(seed)
    prices = [220, 260, 300]
    result = []
    for index in range(STEPS + 1):
        if scenario == "rising":
            prices = [100 + index * 2 + j * 30 for j in range(3)]
        elif scenario == "falling":
            prices = [500 - index * 2 + j * 30 for j in range(3)]
        elif scenario == "alternating":
            prices = [180 + ((index + j) % 2) * 30 + j * 15 for j in range(3)]
        elif scenario == "regime_change":
            phase = index if index <= STEPS // 2 else STEPS - index
            prices = [350 - phase * 2 + j * 30 for j in range(3)]
        elif scenario == "random_walk":
            prices = [max(100, min(500, p + rng.choice((-12, 0, 12)))) for p in prices]
        elif scenario != "flat":
            raise ValueError("unknown fixed scenario")
        observed = START + timedelta(seconds=index + 1)
        rows = [{
            "match_id": f"fixture-{j}", "sport_key": "table_tennis",
            "source": "pinnacle", "period_key": "FT", "market": "h2h",
            "line": 0, "side": "home", "price": price,
            "timestamp_ms": int(observed.timestamp() * 1000), "age_seconds": 0,
        } for j, price in enumerate(prices)]
        result.append(parse_sandbox_live_payload(
            {"sport_key": "table_tennis", "period": "FT", "count": len(rows), "results": rows},
            available_at=(observed + timedelta(microseconds=500_000)).isoformat(timespec="microseconds"),
        ))
    return result


def compare():
    rows = []
    for scenario in SCENARIOS:
        # Deterministic trends need one seed; random walks retain every fixed seed.
        for seed in (SEEDS if scenario == "random_walk" else SEEDS[:1]):
            data = snapshots(scenario, seed)
            snapshot_digest = hashlib.sha256("".join(s.snapshot_sha256 for s in data).encode()).hexdigest()
            outcomes = {}
            for name, cls in (("fixed_alternation", FixedAlternation), ("adaptive_abstention", BlindPaperSandboxCampaign)):
                plan = BlindPaperCampaignPlan(
                    campaign_id=f"selection-{scenario}-{seed}",
                    started_at=START.isoformat(), cutoff_at=(START + timedelta(hours=1)).isoformat(),
                )
                campaign = cls(plan, seed=seed)
                for decision, outcome in zip(data, data[1:]):
                    campaign.step(decision, outcome)
                diagnostics = campaign.diagnostics()
                outcomes[name] = {
                    "net_reward": diagnostics["net_reward"],
                    "maximum_drawdown": diagnostics["maximum_drawdown"],
                    "ticket_count": diagnostics["ticket_count"],
                    "wait_count": campaign.counters["WAIT"],
                    "cohorts": diagnostics["cohorts"],
                    "estimates": campaign.summary()["estimates"],
                }
            delta = Decimal(outcomes["adaptive_abstention"]["net_reward"]) - Decimal(outcomes["fixed_alternation"]["net_reward"])
            rows.append({"scenario": scenario, "seed": seed, "snapshots_sha256": snapshot_digest, "results": outcomes, "reward_delta": str(delta)})
    return {
        "schema": "autosport.account_c.paired_selection_regression", "schema_version": 1,
        "truth": "simulated", "paper_only": True, "profitability_claim": False,
        "promotion_authorized": False, "forward_evidence_eligible": False,
        "real_money_execution": False, "steps_per_case": STEPS,
        "baseline_source": "2c3cb80c348dd5328668cae042b4b2507baab0f8",
        "limitation": "Development fixtures using line-movement settlement; no authoritative match outcomes, holdout, statistical significance or economic edge is established.",
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "campaign_source_sha256": hashlib.sha256((Path(__file__).resolve().parents[1] / "src/autosport/paper_live_sandbox_campaign.py").read_bytes()).hexdigest(),
        "cases": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare()
    atomic_write_json(args.output, report)
    print(json.dumps({"cases": len(report["cases"]), "output": str(args.output), "profitability_claim": False}))
