"""Delayed retrospective shadow learning using recorded match outcomes.

This experiment can learn about a declined action without calling that action
an executed bet. It reuses the existing recorded-source parser and accounting.
The release lag is an assumption, NOT proof of actual result availability.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from autosport.shadow_action_selection import ShadowSelectionConfig, choose_shadow_action
from benchmark_recorded_match_returns import STRATEGIES, parse_rows, summarize


def evaluate(path: Path, config: dict) -> dict:
    if type(config) is not dict or set(config) != {"split_date", "outcome_lag_hours", "selector"}:
        raise ValueError("unexpected shadow experiment configuration")
    if type(config["split_date"]) is not str:
        raise ValueError("split date must be a string")
    boundary = datetime.strptime(config["split_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    lag = config["outcome_lag_hours"]
    if type(lag) is not int or not 24 <= lag <= 720:
        raise ValueError("outcome lag must be in 24..720 hours")
    selector = ShadowSelectionConfig(**config["selector"])
    rows, source = parse_rows(path)
    validation = [row for row in rows if row["kickoff"] >= boundary]
    if not validation or not any(row["kickoff"] < boundary for row in rows):
        raise ValueError("both chronological cohorts required")
    histories = {name: deque(maxlen=selector.window) for name in STRATEGIES}
    released = 0
    selected_rows, decisions = [], []
    for row in validation:
        # Strict inequality also prevents same-time observations entering a choice.
        cutoff = row["kickoff"] - timedelta(hours=lag)
        while released < len(rows) and rows[released]["kickoff"] < cutoff:
            for name in STRATEGIES:
                histories[name].append(rows[released]["rewards"][name])
            released += 1
        action = choose_shadow_action(histories, selector)
        decisions.append({"id": row["id"], "decision_at": row["kickoff"].isoformat(),
                          "assumed_outcome_cutoff_exclusive": cutoff.isoformat(),
                          "released_shadow_matches": released, "selection": action,
                          "shadow_window_observations": len(histories[STRATEGIES[0]])})
        # Current result is read only after choosing. Only the chosen return
        # enters virtual accounting; declined returns remain shadow observations.
        if action != "WAIT":
            selected_rows.append({"kickoff": row["kickoff"],
                                  "rewards": {"SHADOW_SELECTOR": row["rewards"][action]}})
    return {
        "schema": "autosport.recorded_shadow_agent_experiment", "schema_version": 1,
        "source": {**source, "attribution": "TipsAudit (2026), https://tipsaudit.com/data, CC BY 4.0"},
        "config": config,
        "config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "validation_matches": len(validation), "decisions": decisions,
        "validation": {"chosen_actions": summarize(selected_rows, "SHADOW_SELECTOR", selector.cost_bps_per_stake),
                       "wait_count": len(validation) - len(selected_rows),
                       "fixed_strategy_controls": {name: summarize(validation, name, selector.cost_bps_per_stake)
                                                   for name in STRATEGIES}},
        "learning_basis": "hypothetical returns of all three proxies; separate from chosen-action accounting",
        "evaluation_mode": "exploratory retrospective with delayed online adaptation; NOT untouched holdout",
        "settlement_basis": "provider-recorded final results; hypothetical fixed one-unit single stakes",
        "quote_timing_verified": False, "outcome_availability_verified": False,
        "forward_evidence_eligible": False, "profitability_claim": False,
        "promotion_authorized": False, "real_money_execution": False,
        "limitations": ["Outcome release lag is assumed; postponed or late-corrected results may arrive later.",
                        "Opening prices can be backfilled; no executable quote or capital-schedule proof.",
                        "Validation outcomes enter later decisions after the assumed lag; this is not a frozen model holdout.",
                        "Selected-source, multiple-strategy and prior experiment inspection biases remain.",
                        "Rolling means are not confidence bounds or proof of positive expected profit."],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(args.csv, strict_json_loads(args.config.read_text("utf-8")))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"config_sha256": report["config_sha256"], "validation": report["validation"]}))
