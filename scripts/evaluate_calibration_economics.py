"""Compare baseline and delayed-label calibration on recorded football outcomes.

Retrospective research only: price and label timing are assumptions, not proof of
available orders or profit. Fixed protocol: docs/CALIBRATION_ECONOMICS_PROTOCOL_20261028.md.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import heapq
import json
from pathlib import Path

from autosport.prospective_forecast_learning import PROTOCOL, RESULTS, SIDES, _bin, _predict
from autosport.probability import ForecastObservation, brier_score
from autosport.shadow_strategy_memory import digest
from benchmark_recorded_match_returns import summarize
from evaluate_recorded_cross_book_value import pick_quote


def evaluate(path, config):
    raw = Path(path).read_bytes()
    records = list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
    parsed, seen, exclusions = [], set(), Counter()
    for row in records:
        identity = row.get("id")
        if not identity or identity in seen:
            raise ValueError("duplicate or missing event identity")
        seen.add(identity)
        try:
            kickoff = datetime.fromisoformat(row["kickoff_utc"])
            if kickoff.tzinfo is None:
                raise ValueError("naive kickoff")
            p = {side: Decimal(row[f"fair_prob_{side}"]) for side in SIDES}
            if any(not x.is_finite() or not 0 < x < 1 for x in p.values()) or abs(sum(p.values()) - 1) > Decimal(".001"):
                raise ValueError("invalid probability")
            total = sum(p.values())
            p = {side: value / total for side, value in p.items()}
        except (KeyError, ValueError, InvalidOperation):
            exclusions["invalid_probability_or_kickoff"] += 1
            continue
        parsed.append((kickoff.astimezone(timezone.utc), identity, row, p))
    parsed.sort(key=lambda x: (x[0], x[1]))
    start = datetime.fromisoformat(config["earliest_quote_date"]).replace(tzinfo=timezone.utc)
    bins = {side: [[0, Decimal(0), Decimal(0)] for _ in range(PROTOCOL["bins"])] for side in SIDES}
    pending, daily = [], {name: Counter() for name in ("baseline", "calibrated")}
    selected = {name: [] for name in daily}
    scored = {name: [] for name in daily}
    journal, training_count = [], 0
    for index, (kickoff, identity, row, p) in enumerate(parsed):
        while pending and pending[0][0] <= kickoff:
            _, _, prior_p, result = heapq.heappop(pending)
            training_count += 1
            for side in SIDES:
                cell = bins[side][_bin(prior_p[side])]
                cell[0] += 1
                cell[1] += int(RESULTS[result] == side)
                cell[2] += prior_p[side]
        calibrated = {side: Decimal(value) for side, value in _predict(p, bins).items()}
        forecasts = {"baseline": p, "calibrated": calibrated}
        if kickoff >= start:
            output = {"id": identity, "kickoff": kickoff.isoformat(), "training_events": training_count,
                      "decisions": {}, "result": row.get("result"), "void_reason": row.get("void_reason")}
            if row.get("result") in RESULTS and not row.get("void_reason"):
                for name, forecast in forecasts.items():
                    scored[name].append(brier_score([ForecastObservation(float(forecast[side]),
                                              int(RESULTS[row["result"]] == side)) for side in SIDES]))
            for name, forecast in forecasts.items():
                inputs = {**row, **{f"fair_prob_{side}": str(forecast[side]) for side in SIDES}}
                try:
                    choice = pick_quote(inputs, config)
                except (KeyError, ValueError, InvalidOperation):
                    choice = None
                    exclusions[name + "_invalid_quote_inputs"] += 1
                day = kickoff.date().isoformat()
                if choice is None or daily[name][day] >= config["maximum_candidates_per_kickoff_day"]:
                    output["decisions"][name] = None
                    continue
                daily[name][day] += 1
                value, pick, odds, book = choice
                decision = {"selection": pick, "quote": str(odds), "bookmaker": book,
                            "model_value": str(value), "state": "PENDING", "reward": None}
                if row.get("void_reason"):
                    decision.update(state="VOID", reward="0")
                elif row.get("result") in RESULTS:
                    reward = odds - 1 if row["result"] == pick else Decimal(-1)
                    decision.update(state="SETTLED", reward=str(reward))
                    selected[name].append({"kickoff": kickoff, "rewards": {"CHOSEN": reward}})
                output["decisions"][name] = decision
            journal.append(output)
        if row.get("result") in RESULTS and not row.get("void_reason"):
            heapq.heappush(pending, (kickoff + timedelta(hours=48), index, p, row["result"]))
    overlap = Counter()
    for row in journal:
        a, b = row["decisions"]["baseline"], row["decisions"]["calibrated"]
        if a and b:
            overlap["same_selection" if a["selection"] == b["selection"] else "different_selection"] += 1
        elif a:
            overlap["baseline_only"] += 1
        elif b:
            overlap["calibrated_only"] += 1
    return {"schema": "autosport.calibration_economics.v1", "protocol": PROTOCOL,
            "source_sha256": hashlib.sha256(raw).hexdigest(), "config": config,
            "config_sha256": digest(config), "source_rows": len(records), "valid_rows": len(parsed),
            "exclusions": dict(exclusions), "decisions": journal, "selection_overlap": dict(overlap),
            "variants": {name: {"zero_cost": summarize(selected[name], "CHOSEN", 0),
                                "one_percent_cost": summarize(selected[name], "CHOSEN", 100),
                                "brier": sum(scored[name]) / len(scored[name]) if scored[name] else None,
                                "scored_matches": len(scored[name]),
                                "candidates_including_void_and_pending": sum(1 for row in journal if row["decisions"][name])}
                         for name in daily},
            "attribution": "TipsAudit (2026), https://tipsaudit.com/data, CC BY 4.0",
            "evaluation_mode": "exploratory retrospective, assumed 48h label delay, not untouched holdout",
            "quote_timing_verified": False, "quote_acceptance_verified": False,
            "profitability_claim": False, "promotion_authorized": False, "real_money_execution": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.csv, json.loads(args.config.read_text("utf-8")))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({"variants": result["variants"], "selection_overlap": result["selection_overlap"]}))
