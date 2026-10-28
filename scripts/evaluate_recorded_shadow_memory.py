"""Chronological exploratory learning from recorded final match outcomes.

Result availability is ASSUMED at kickoff+48h, never represented as verified.
The learner observes all pre-existing price rules after the delay, including
rules it did not select. Hypothetical unit returns never enter a real ledger.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import heapq
import json
from pathlib import Path

from autosport.shadow_strategy_memory import ShadowMemoryPlan, ShadowStrategyMemory, digest
from benchmark_recorded_match_returns import parse_rows, summarize, STRATEGIES


def evaluate(path: Path, config: dict):
    if set(config) != {"split_date", "window", "minimum_observations", "minimum_mean_return", "feedback_delay_hours", "cost_bps_per_stake"}:
        raise ValueError("unexpected shadow experiment fields")
    if type(config["feedback_delay_hours"]) is not int or not 24 <= config["feedback_delay_hours"] <= 168:
        raise ValueError("feedback delay must be 24..168 hours")
    if type(config["cost_bps_per_stake"]) is not int or not 0 <= config["cost_bps_per_stake"] <= 1000:
        raise ValueError("invalid cost stress")
    boundary = datetime.fromisoformat(config["split_date"]).replace(tzinfo=timezone.utc)
    rows, source = parse_rows(path)
    if not any(row["kickoff"] < boundary for row in rows) or not any(row["kickoff"] >= boundary for row in rows):
        raise ValueError("both chronological cohorts required")
    memories = {name: ShadowStrategyMemory(ShadowMemoryPlan(
        tuple(sorted(STRATEGIES)), window=window, minimum_observations=config["minimum_observations"],
        minimum_mean_return=config["minimum_mean_return"]))
        for name, window in (("rolling_shadow", config["window"]), ("long_history_shadow", 10000))}
    selections = {name: [] for name in memories}
    journal, pending = [], []
    cost = Decimal(config["cost_bps_per_stake"]) / 10000
    for index, row in enumerate(rows):
        decision_time = row["kickoff"]
        while pending and pending[0][0] <= decision_time:
            available, _, past = heapq.heappop(pending)
            for memory in memories.values():
                memory.observe(event_id=past["id"], decision_at=past["kickoff"].isoformat(),
                               result_available_at=available.isoformat(), now=decision_time.isoformat(),
                               net_returns={key: reward - cost for key, reward in past["rewards"].items()},
                               source_sha256=source["source_sha256"], availability_basis="retrospective_assumed_delay")
        # Choose before this outcome is made available, including simultaneous matches.
        choices = {name: memory.choose(as_of=decision_time.isoformat(),
                                       admissible_actions=frozenset((*STRATEGIES, "WAIT")))
                   for name, memory in memories.items()}
        if decision_time >= boundary:
            journal.append({"id": row["id"], "decision_time": decision_time.isoformat(), "choices": choices})
            for name, choice in choices.items():
                if choice != "WAIT":
                    selections[name].append({"kickoff": row["kickoff"], "rewards": {"CHOSEN": row["rewards"][choice]}})
        heapq.heappush(pending, (decision_time + timedelta(hours=config["feedback_delay_hours"]), index, row))
    validation_count = len(journal)
    return {"schema": "autosport.recorded_shadow_memory_experiment", "config": config,
            "config_sha256": digest(config), "source": source,
            "attribution": "TipsAudit (2026), https://tipsaudit.com/data, CC BY 4.0",
            "validation_matches": validation_count,
            "variants": {name: {"result": summarize(selected, "CHOSEN", config["cost_bps_per_stake"]),
                                 "wait_count": validation_count - len(selected)}
                         for name, selected in selections.items()},
            "decisions": journal, "pending_feedback_count": len(pending),
            "evaluation_mode": "exploratory retrospective rolling learning; not pristine holdout",
            "result_availability": "assumed kickoff plus configured delay, not verified publication time",
            "quote_timing_verified": False, "capital_schedule_verified": False,
            "profitability_claim": False, "promotion_authorized": False,
            "real_money_execution": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(args.csv, json.loads(args.config.read_text("utf-8")))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"variants": report["variants"], "validation_matches": report["validation_matches"]}))
