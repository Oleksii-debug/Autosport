"""Research-only shadow recovery experiment over deterministic simulated quotes.

Counterfactual observations do not mutate the policy or PaperBook. They are
available only after the next snapshot and cannot represent match outcomes.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from datetime import timedelta
from pathlib import Path

from compare_account_c_recovery_probe import START, STEPS, snapshots as original_snapshots
from autosport.paper_live_sandbox_campaign import (
    BlindPaperCampaignPlan, BlindPaperSandboxCampaign, PARLAY_ACTIONS, SINGLE_ACTIONS,
    parse_sandbox_live_payload,
)


class ShadowRecovery(BlindPaperSandboxCampaign):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.shadow = {name: deque(maxlen=8) for name in PARLAY_ACTIONS | SINGLE_ACTIONS}
        self.shadow_admissions = 0
        self._pending_shadow_admission = False

    def _admissible_actions(self, snapshot):
        self._pending_shadow_admission = False
        base = super()._admissible_actions(snapshot)
        if base != frozenset({"WAIT"}) or self.shadow_admissions >= 4:
            return base
        slot = self.policy.generation % self.plan.curriculum_period
        events = len({q.match_id for q in snapshot.quotes})
        family = (PARLAY_ACTIONS if slot < self.plan.parlay_slots and events >= 2 else
                  SINGLE_ACTIONS if self.plan.parlay_slots <= slot < self.plan.curriculum_period - 1 and snapshot.quotes else
                  PARLAY_ACTIONS | SINGLE_ACTIONS if slot == self.plan.curriculum_period - 1 and events >= 2 else
                  SINGLE_ACTIONS if slot == self.plan.curriculum_period - 1 and snapshot.quotes else frozenset())
        eligible = [name for name in family if len(self.shadow[name]) == 8 and sum(self.shadow[name]) >= 7]
        if not eligible:
            return base
        selected = min(eligible, key=lambda name: (-sum(self.shadow[name]), name))
        self._pending_shadow_admission = True
        return frozenset({selected})

    def step(self, decision_snapshot, outcome_snapshot):
        before_wait = self.counters["WAIT"]
        advanced = super().step(decision_snapshot, outcome_snapshot)
        if not advanced:
            return False
        if self._pending_shadow_admission:
            self.shadow_admissions += 1
        if self.counters["WAIT"] == before_wait:
            return True
        future = outcome_snapshot.by_identity()
        for name in self.shadow:
            try:
                chosen = self._selected_quotes(decision_snapshot, name)
            except ValueError:
                continue
            moves = []
            for quote in chosen:
                later = future.get(quote.quote_identity)
                if later is not None and later.decimal_odds != quote.decimal_odds:
                    moves.append(later.decimal_odds < quote.decimal_odds)
            if moves:
                self.shadow[name].append(int(all(moves)))
        return True


class ShadowRecoveryCapped(ShadowRecovery):
    """Post-hoc comparison: at most four total tickets after first readmission."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tickets_after_readmission = 0

    def _admissible_actions(self, snapshot):
        if self.shadow_admissions > 0 and self.tickets_after_readmission >= 4:
            self._pending_shadow_admission = False
            return frozenset({"WAIT"})
        return super()._admissible_actions(snapshot)

    def step(self, decision_snapshot, outcome_snapshot):
        before_tickets = len(self.book.tickets)
        already_readmitted = self.shadow_admissions > 0
        advanced = super().step(decision_snapshot, outcome_snapshot)
        if advanced and len(self.book.tickets) > before_tickets and (
            already_readmitted or self.shadow_admissions > 0
        ):
            self.tickets_after_readmission += 1
        return advanced


def snapshots(scenario):
    if scenario not in {"short_recovery_then_reversal", "false_signal_reversal", "false_signal_immediate"}:
        return original_snapshots(scenario)
    reversal = {"short_recovery_then_reversal": 102,
                "false_signal_reversal": 98, "false_signal_immediate": 97}[scenario]
    data = []
    for i in range(STEPS + 1):
        # A temporary favorable period is followed by a renewed losing regime.
        price = (150 + i * 2 if i <= 90 else
                 330 - (i - 90) * 2 if i <= reversal else
                 330 - (reversal - 90) * 2 + (i - reversal) * 3)
        at = START + timedelta(seconds=i + 1)
        rows = [{"match_id": f"fixture-{j}", "sport_key": "table_tennis",
                 "source": "pinnacle", "period_key": "FT", "market": "h2h",
                 "line": 0, "side": "home", "price": price + j * 30,
                 "timestamp_ms": int(at.timestamp() * 1000), "age_seconds": 0}
                for j in range(3)]
        data.append(parse_sandbox_live_payload(
            {"sport_key": "table_tennis", "period": "FT", "count": len(rows), "results": rows},
            available_at=(at + timedelta(microseconds=500000)).isoformat()))
    return data


def compare():
    cases = []
    for scenario in ("loss", "favorable", "recovery", "short_recovery_then_reversal", "false_signal_reversal", "false_signal_immediate"):
        data = snapshots(scenario)
        results = {}
        for name, cls in (("baseline", BlindPaperSandboxCampaign), ("shadow", ShadowRecovery),
                          ("shadow_capped", ShadowRecoveryCapped)):
            plan = BlindPaperCampaignPlan(campaign_id=f"shadow-{scenario}",
                started_at=START.isoformat(), cutoff_at=(START + timedelta(hours=1)).isoformat())
            campaign = cls(plan, seed=11)
            for decision, outcome in zip(data, data[1:]):
                campaign.step(decision, outcome)
            diag = campaign.diagnostics()
            results[name] = {"net_reward": diag["net_reward"],
                "maximum_drawdown": diag["maximum_drawdown"],
                "ticket_count": diag["ticket_count"], "wait_count": campaign.counters["WAIT"],
                "shadow_admissions": getattr(campaign, "shadow_admissions", 0),
                "tickets_after_readmission": getattr(campaign, "tickets_after_readmission", None)}
        cases.append({"scenario": scenario, "snapshot_sha256": hashlib.sha256(
            "".join(snapshot.snapshot_sha256 for snapshot in data).encode()).hexdigest(),
            "results": results})
    return {"schema": "autosport.account_c.shadow_recovery.research.v1",
            "steps": STEPS, "shadow_window": 8, "minimum_positive": 7,
            "maximum_shadow_admissions": 4, "truth": "simulated",
            "posthoc_capped_total_tickets_after_first_readmission": 4,
            "forward_evidence_eligible": False, "promotion_authorized": False,
            "profitability_claim": False, "real_money_execution": False,
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "campaign_sha256": hashlib.sha256((Path(__file__).resolve().parents[1] /
                "src/autosport/paper_live_sandbox_campaign.py").read_bytes()).hexdigest(),
            "fixture_sha256": hashlib.sha256((Path(__file__).resolve().parent /
                "compare_account_c_recovery_probe.py").read_bytes()).hexdigest(),
            "cases": cases}


if __name__ == "__main__":
    print(json.dumps(compare(), indent=2, sort_keys=True))
