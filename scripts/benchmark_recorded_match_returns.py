"""Read-only retrospective price-strategy benchmark, never a promotion authority.

Source: TipsAudit Football Prediction Dataset https://tipsaudit.com/data,
CC BY 4.0. Opening Pinnacle prices only; soft/closing prices are NOT inputs.
Provider prices can be backfilled, so no live availability or executable profit
is asserted. Actual recorded 1X2 results settle hypothetical fixed-unit returns.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import random


STRATEGIES = ("LOW_ODDS", "HIGH_ODDS", "SOURCE_PICK")
PICKS = {"1": {"1"}, "X": {"X"}, "2": {"2"}, "1X": {"1", "X"},
         "X2": {"X", "2"}, "12": {"1", "2"}}


def parse_rows(path: Path, *, include_team_context: bool = False):
    rows, exclusions, seen = [], Counter(), set()
    raw = path.read_bytes()
    source_sha = hashlib.sha256(raw).hexdigest()
    reader = csv.DictReader(raw.decode("utf-8-sig").splitlines())
    for item in reader:
        identity = item.get("id", "")
        if not identity or identity in seen:
            raise ValueError("missing or duplicate match identity")
        seen.add(identity)
        if item.get("void_reason") or item.get("result") not in {"1", "X", "2"}:
            exclusions["void_or_unsettled"] += 1
            continue
        try:
            kickoff = datetime.fromisoformat(item["kickoff_utc"])
            if kickoff.tzinfo is None:
                raise ValueError("naive kickoff")
            odds = {pick: Decimal(item[column]) for pick, column in (
                ("1", "pin_open_home"), ("X", "pin_open_draw"), ("2", "pin_open_away"))}
            source_odds = Decimal(item["prediction_odds_taken"])
            if any(not value.is_finite() or value <= 1 for value in (*odds.values(), source_odds)):
                raise ValueError("invalid price")
            source_pick = item["prediction_pick"]
            if source_pick not in PICKS:
                raise ValueError("unsupported source pick")
        except (ValueError, KeyError, InvalidOperation):
            exclusions["missing_or_invalid_common_price_cohort"] += 1
            continue
        choices = {
            "LOW_ODDS": min(odds, key=lambda key: (odds[key], key)),
            "HIGH_ODDS": min(odds, key=lambda key: (-odds[key], key)),
            "SOURCE_PICK": source_pick,
        }
        rewards = {}
        for name, pick in choices.items():
            price = source_odds if name == "SOURCE_PICK" else odds[pick]
            rewards[name] = price - 1 if item["result"] in PICKS[pick] else Decimal(-1)
        row = {"id": identity, "kickoff": kickoff.astimezone(timezone.utc),
               "rewards": rewards, "odds": odds, "result": item["result"]}
        if include_team_context:
            # Extract from the SAME byte snapshot as prices/results; no second
            # file read can silently attach context from a different revision.
            row["team_context"] = {key: item.get(key, "") for key in ("country", "home", "away")}
        rows.append(row)
    rows.sort(key=lambda item: (item["kickoff"], item["id"]))
    return rows, {"source_sha256": source_sha, "source_rows": len(seen),
                  "eligible_rows": len(rows), "exclusions": dict(exclusions)}


def summarize(rows, strategy, cost_bps=0):
    cost = Decimal(cost_bps) / 10000
    equity = peak = max_drawdown = Decimal(0)
    wins = 0
    days = defaultdict(lambda: [Decimal(0), 0])
    for row in rows:
        raw_reward = row["rewards"][strategy]
        reward = raw_reward - cost
        equity += reward
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        wins += raw_reward > 0
        day = row["kickoff"].date().isoformat()
        days[day][0] += reward
        days[day][1] += 1
    # Day-block resampling describes this retrospective sample only.
    intervals = None
    if len(days) >= 2:
        rng = random.Random(27102026)
        values = list(days.values())
        samples = []
        for _ in range(1000):
            blocks = rng.choices(values, k=len(values))
            samples.append(sum(value[0] for value in blocks) / sum(value[1] for value in blocks))
        samples.sort()
        intervals = [str(samples[24]), str(samples[974])]
    return {"bets": len(rows), "wins": wins, "net_units": str(equity),
            "roi": str(equity / len(rows)) if rows else None,
            "max_drawdown_units": str(max_drawdown), "cost_bps_per_stake": cost_bps,
            "day_block_bootstrap_roi_95pct": intervals}


def benchmark(path: Path, *, split: str = "2026-09-21", minimum_training=100):
    boundary = datetime.fromisoformat(split).replace(tzinfo=timezone.utc)
    rows, source = parse_rows(path)
    train = [row for row in rows if row["kickoff"] < boundary]
    validation = [row for row in rows if row["kickoff"] >= boundary]
    if len(train) < minimum_training or not validation:
        raise ValueError("insufficient chronological train/validation cohort")
    training = {name: summarize(train, name) for name in STRATEGIES}
    best = min(STRATEGIES, key=lambda name: (-Decimal(training[name]["roi"]), name))
    # Frozen before the validation result is inspected. No holdout tuning.
    selected = best if Decimal(training[best]["roi"]) > 0 else "WAIT"
    heldout = {name: {"zero_cost": summarize(validation, name),
                      "one_percent_stake_cost": summarize(validation, name, 100)}
               for name in STRATEGIES}
    return {
        "schema": "autosport.recorded_match_price_benchmark", "schema_version": 1,
        "source": {**source, "attribution": "TipsAudit (2026), https://tipsaudit.com/data, CC BY 4.0"},
        "split_date_utc": split, "train_matches": len(train), "validation_matches": len(validation),
        "training": training, "validation": heldout, "training_selected_strategy": selected,
        "training_selected_validation": heldout[selected] if selected != "WAIT" else {
            "bets": 0, "net_units": "0", "interpretation": "abstention; no demonstrated earning capability"},
        "evaluation_mode": "retrospective provider-recorded outcomes, hypothetical fixed-unit returns",
        "selection_inputs": "opening prices only; source pick is third-party, not an Autosport forecast",
        "quote_timing_verified": False, "executable_portfolio_verified": False,
        "forward_evidence_eligible": False, "profitability_claim": False,
        "promotion_authorized": False, "real_money_execution": False,
        "limitations": ["Opening prices may be backfilled by provider.",
                        "Source hashes and outcomes are not independently timestamp-verified here.",
                        "Covered matches only; incomplete-price and void counts are explicit.",
                        "No tax, liquidity, stake-limit, commission schedule or execution feasibility proof.",
                        "Retrospective validation is not a prospective untouched live campaign."],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = benchmark(args.csv)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"training_selected_strategy": report["training_selected_strategy"],
                      "train_matches": report["train_matches"],
                      "validation_matches": report["validation_matches"],
                      "validation": report["validation"]}, ensure_ascii=False))
