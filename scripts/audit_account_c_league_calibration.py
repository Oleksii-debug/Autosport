"""Retrospective league-conditioned challenger, not production learning."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from audit_account_c_outcome_calibration import ARMS, load_rows, metrics, predict_by_date


CHALLENGER = "league_prior_ridge"


def league_predictions(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    predicted, global_history = predict_by_date(rows)
    global_by_day = {entry["date"]: entry for entry in global_history}
    groups = defaultdict(list)
    for row in predicted:
        groups[row["day"]].append(row)
    state = defaultdict(lambda: {"xx": 0.0, "xy": 0.0, "count": 0})
    history = []
    for day, batch in sorted(groups.items()):
        global_slope = global_by_day[day.isoformat()]["slope"]
        for row in batch:
            prior = state[row["league"]]
            slope = min(1.0, max(0.0, (prior["xy"] + global_slope) / (prior["xx"] + 1.0)))
            row["predictions"][CHALLENGER] = 0.5 + slope * (row["p"] - 0.5)
        for league in sorted({row["league"] for row in batch}):
            cohort = [row for row in batch if row["league"] == league]
            prior = state[league]
            history.append({"date": day.isoformat(), "league": league,
                            "prior_rows": prior["count"], "global_slope": global_slope,
                            "league_slope": min(1.0, max(0.0, (prior["xy"] + global_slope) /
                                                                  (prior["xx"] + 1.0)))})
            prior["xx"] = math.fsum((prior["xx"], math.fsum((r["p"]-.5)**2 for r in cohort)))
            prior["xy"] = math.fsum((prior["xy"], math.fsum((r["p"]-.5)*(r["y"]-.5) for r in cohort)))
            prior["count"] += len(cohort)
    return predicted, history


def audit(raw: bytes) -> dict:
    rows = load_rows(raw)
    predicted, history = league_predictions(rows)
    evaluation = [row for row in predicted if row["evaluated"]]
    if not evaluation:
        raise ValueError("sample does not extend beyond fixed warmup")
    by_week, by_league = defaultdict(list), defaultdict(list)
    for row in evaluation:
        week = row["day"] - timedelta(days=row["day"].weekday())
        by_week[week.isoformat()].append(row)
        by_league[row["league"]].append(row)
    arms = (*ARMS, CHALLENGER)
    return {"schema": "autosport.account_c.league_calibration.development.v1",
            "source_url": "https://github.com/lathise2025/table-tennis-elo-dataset",
            "source_commit": "9eba2ade435ce27b4f3dcbd5aafc6bfbcd52d534",
            "source_commit_verified_by_caller": False, "source_license": "CC BY 4.0",
            "input_sha256": hashlib.sha256(raw).hexdigest(),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "base_script_sha256": hashlib.sha256(Path(__file__).with_name(
                "audit_account_c_outcome_calibration.py").read_bytes()).hexdigest(),
            "protocol": {"warmup_calendar_days": 28, "league_ridge": 1.0,
                         "strictly_earlier_dates": True, "within_day_learning": False,
                         "prior_sample_consumed": True},
            "truth": {"upstream_point_in_time_verified": False,
                      "upstream_results_independently_verified": False,
                      "untouched_holdout": False, "forward_evidence_eligible": False,
                      "promotion_authorized": False, "profitability_claim": False,
                      "real_money_execution": False},
            "economic_metrics": {"status": "UNAVAILABLE_NO_ODDS_COSTS_OR_FILLS",
                                 "net_profit": None, "roi": None, "drawdown": None},
            "input_count": len(rows), "warmup_count": len(rows)-len(evaluation),
            "overall": metrics(evaluation, arms),
            "by_week": {k: metrics(v, arms) for k, v in sorted(by_week.items())},
            "by_league": {k: metrics(v, arms) for k, v in sorted(by_league.items())},
            "learning_history": history}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.csv.resolve() == args.output.resolve():
        parser.error("output cannot overwrite input")
    result = audit(args.csv.read_bytes())
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False)+"\n",
                           encoding="utf-8")
    print(json.dumps(result["overall"]))


if __name__ == "__main__":
    main()
