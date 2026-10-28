"""Retrospective causal replay of the existing unseeded shadow preference."""
from __future__ import annotations

import argparse
from datetime import timedelta
from decimal import Decimal
import heapq
import json
from pathlib import Path

from autosport.shadow_strategy_memory import ShadowMemoryPlan, ShadowStrategyMemory, instant
from benchmark_recorded_match_returns import summarize
from evaluate_recorded_live_flags import evaluate


def replay(report):
    source_sha = report["source_sha256"]
    memory = ShadowStrategyMemory(ShadowMemoryPlan(("PRICE_GAP",)))
    candidates = [row for row in report["decisions"] if row["current_rule_selected"]]
    candidates.sort(key=lambda row: (instant(row["recorded_at"]), row["id"]))
    pending, decisions, all_rows, chosen_rows = [], [], [], []
    for index, row in enumerate(candidates):
        now = instant(row["recorded_at"])
        while pending and pending[0][0] <= now:
            available, _, past = heapq.heappop(pending)
            memory.observe(event_id=past["id"], decision_at=past["recorded_at"],
                           result_available_at=available.isoformat(), now=now.isoformat(),
                           net_returns={"PRICE_GAP": Decimal(past["reward"]) - Decimal(".01")},
                           source_sha256=source_sha, availability_basis="retrospective_assumed_delay")
        choice = memory.choose(as_of=now.isoformat(), admissible_actions=frozenset({"WAIT", "PRICE_GAP"}))
        decisions.append({"id": row["id"], "decision_at": row["recorded_at"], "choice": choice,
                          "previous_outcomes": len(memory.payload()["records"]), "state": row["state"],
                          "reward": row["reward"]})
        if row["state"] == "SETTLED":
            target = {"kickoff": instant(row["kickoff"]), "rewards": {"CHOSEN": Decimal(row["reward"])}}
            all_rows.append(target)
            if choice == "PRICE_GAP":
                chosen_rows.append(target)
            heapq.heappush(pending, (instant(row["kickoff"]) + timedelta(hours=48), index, row))
    all_rows.sort(key=lambda x: x["kickoff"])
    chosen_rows.sort(key=lambda x: x["kickoff"])
    return {"schema": "autosport.live_flag_learner_replay.v1", "source_sha256": source_sha,
            "decisions": decisions, "waits": sum(x["choice"] == "WAIT" for x in decisions),
            "selected": sum(x["choice"] == "PRICE_GAP" for x in decisions),
            "all_source_signals": {"zero_cost": summarize(all_rows, "CHOSEN", 0),
                                   "one_percent_cost": summarize(all_rows, "CHOSEN", 100)},
            "learner_selected": {"zero_cost": summarize(chosen_rows, "CHOSEN", 0),
                                 "one_percent_cost": summarize(chosen_rows, "CHOSEN", 100)},
            "outcomes_pending_at_last_decision": len(pending),
            "evaluation_mode": "exploratory previously-inspected recorded source; 48-hour assumed delay",
            "quote_acceptance_verified": False, "profitability_claim": False,
            "promotion_authorized": False, "real_money_execution": False,
            "attribution": "TipsAudit (2026), https://tipsaudit.com/data, CC BY 4.0"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = replay(evaluate(args.csv, ordered_source_times=True))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({key: value for key, value in result.items() if key != "decisions"}))
