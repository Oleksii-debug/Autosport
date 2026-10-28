"""Research-only weighting of an inspected simulated prior in a new campaign."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from compare_account_c_recovery_probe import snapshots
from autosport.paper_live_sandbox_campaign import BlindPaperCampaignPlan, BlindPaperSandboxCampaign

ROOT = Path(__file__).resolve().parents[1]
PRIOR = ROOT / "docs/ACCOUNT_C_POLICY_PRIOR_INSPECTION_20261028.json"
START = datetime(2026, 10, 28, 6, 0, tzinfo=timezone.utc)
PRIOR_STRENGTH = 2


def load_prior(path=PRIOR):
    raw = json.loads(path.read_text(encoding="utf-8"))
    digest = raw.pop("inspection_sha256", None)
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if digest != hashlib.sha256(canonical).hexdigest():
        raise ValueError("prior inspection digest mismatch")
    if (raw.get("truth") != "simulated" or raw.get("paper_only") is not True
        or raw.get("simulation_model_id") != "parlayapi-sandbox-line-movement-v1"
        or raw.get("real_money_execution") is not False or raw.get("promotion_authorized") is not False
        or raw.get("desktop_import_performed") is not False):
        raise ValueError("prior simulation boundary mismatch")
    return raw


class PriorRankCampaign(BlindPaperSandboxCampaign):
    """Prior breaks ties among currently positive choices; it cannot override WAIT."""
    def __init__(self, *args, prior, prior_strength=PRIOR_STRENGTH, **kwargs):
        super().__init__(*args, **kwargs)
        self.prior = prior
        self.prior_strength = prior_strength

    def _explore_then_admit_family(self, family):
        base = super()._explore_then_admit_family(family)
        if base == frozenset({"WAIT"}) or len(base) <= 1:
            return base
        estimates = {e.action_type: e for e in self.policy.estimates}
        def score(name):
            old = self.prior["action_estimates"][name]
            count = min(self.prior_strength, old["observations"])
            current = estimates[name]
            old_mean = Decimal(old["simulated_mean_reward"])
            return (current.reward_sum + old_mean * count) / Decimal(current.observations + count)
        return frozenset({min(base, key=lambda name: (-score(name), name))})


def later_snapshots(scenario):
    # Existing research fixtures begin at 00:00 UTC, before the source artifact
    # was created at 04:42:57. Move all event times to 06:00 before reuse.
    shift = timedelta(hours=6)
    def stamp(value):
        return (datetime.fromisoformat(value.replace("Z", "+00:00")) + shift).isoformat(timespec="microseconds")
    return [replace(s, observed_at=stamp(s.observed_at), available_at=stamp(s.available_at),
                    quotes=tuple(replace(q, source_timestamp_ms=q.source_timestamp_ms + 6*3600*1000)
                                 for q in s.quotes)) for s in snapshots(scenario)]


def compare():
    prior = load_prior()
    cases = []
    for scenario in ("loss", "favorable", "recovery"):
        data = later_snapshots(scenario)
        outcomes = {}
        for name, cls in (("baseline", BlindPaperSandboxCampaign),
                          ("rank_no_prior", PriorRankCampaign),
                          ("prior_rank", PriorRankCampaign)):
            plan = BlindPaperCampaignPlan(campaign_id=f"prior-{scenario}", started_at=START.isoformat(),
                                          cutoff_at=(START + timedelta(hours=1)).isoformat())
            kwargs = ({"prior": prior, "prior_strength": 0 if name == "rank_no_prior" else PRIOR_STRENGTH}
                      if cls is PriorRankCampaign else {})
            campaign = cls(plan, seed=11, **kwargs)
            for before, after in zip(data, data[1:]):
                campaign.step(before, after)
            diag = campaign.diagnostics()
            outcomes[name] = {"net_reward": diag["net_reward"], "drawdown": diag["maximum_drawdown"],
                              "tickets": diag["ticket_count"], "waits": campaign.counters["WAIT"]}
        cases.append({"scenario": scenario, "outcomes": outcomes})
    return {"schema": "autosport.sandbox_prior_rank.research.v1", "prior_strength": PRIOR_STRENGTH,
            "prior_inspection_sha256": hashlib.sha256(PRIOR.read_bytes()).hexdigest(),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "source_artifact_created_at": "2026-10-28T04:42:57Z", "scenario_start": START.isoformat(),
            "truth": "simulated", "real_money_execution": False, "profitability_claim": False,
            "forward_evidence_eligible": False, "promotion_authorized": False,
            "desktop_import_performed": False, "cases": cases}


if __name__ == "__main__":
    print(json.dumps(compare(), indent=2, sort_keys=True))
