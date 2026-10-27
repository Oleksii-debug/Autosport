"""Read-only retrospective calibration diagnostic; never product learning evidence.

See docs/ACCOUNT_C_OUTCOME_CALIBRATION_PROTOCOL_20261027.md. No network,
PaperBook, production memory, monetary evaluation or policy promotion is used.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path


ARMS = ("flat", "publisher", "fixed_half", "past_dates_ridge")
SOURCE_COMMIT = "9eba2ade435ce27b4f3dcbd5aafc6bfbcd52d534"
SOURCE_URL = "https://github.com/lathise2025/table-tennis-elo-dataset"
WARMUP_DAYS = 28
RIDGE = 1.0


def load_rows(raw: bytes) -> list[dict]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    required = {"match_id", "date", "league", "player_a", "player_b",
                "p_model", "winner", "sets_a", "sets_b"}
    if (not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames))
            or not required <= set(reader.fieldnames)):
        raise ValueError("missing or duplicate CSV columns")
    rows, seen = [], set()
    for number, record in enumerate(reader, 2):
        if None in record or any(record.get(key) in (None, "") for key in required):
            raise ValueError(f"invalid row shape at line {number}")
        match_id = record["match_id"]
        if match_id in seen:
            raise ValueError(f"duplicate match_id at line {number}")
        seen.add(match_id)
        day = date.fromisoformat(record["date"])
        p = float(record["p_model"])
        if not math.isfinite(p) or not 0 < p < 1:
            raise ValueError(f"probability outside open unit interval at line {number}")
        if record["winner"] not in ("A", "B"):
            raise ValueError(f"invalid winner at line {number}")
        a, b = int(record["sets_a"]), int(record["sets_b"])
        if (a < 0 or b < 0 or a == b or
                (a > b) != (record["winner"] == "A") or
                record["player_a"] == record["player_b"]):
            raise ValueError(f"inconsistent match result at line {number}")
        rows.append({"match_id": match_id, "day": day, "league": record["league"],
                     "p": p, "y": int(record["winner"] == "A")})
    if not rows:
        raise ValueError("empty sample")
    return sorted(rows, key=lambda row: (row["day"], row["match_id"]))


def predict_by_date(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Predict the entire day, then update; same-day outcomes never enter slope."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["day"]].append(row)
    first = min(grouped)
    cutoff = first + timedelta(days=WARMUP_DAYS)
    xx, xy, count = 0.0, 0.0, 0
    history, predictions = [], []
    for day, batch in sorted(grouped.items()):
        batch = sorted(batch, key=lambda row: row["match_id"])
        slope = min(1.0, max(0.0, xy / (RIDGE + xx)))
        history.append({"date": day.isoformat(), "prior_rows": count,
                        "slope": slope, "evaluated": day >= cutoff})
        for row in batch:
            x = row["p"] - 0.5
            predictions.append({**row, "evaluated": day >= cutoff, "predictions": {
                "flat": 0.5, "publisher": row["p"], "fixed_half": 0.5 + 0.5*x,
                "past_dates_ridge": 0.5 + slope*x,
            }})
        # Summation order is stable regardless of input CSV order.
        xx = math.fsum((xx, math.fsum((row["p"]-.5)**2 for row in batch)))
        xy = math.fsum((xy, math.fsum((row["p"]-.5)*(row["y"]-.5) for row in batch)))
        count += len(batch)
    return predictions, history


def metrics(rows: list[dict]) -> dict:
    result = {"count": len(rows), "arms": {}}
    for arm in ARMS:
        brier = math.fsum((r["predictions"][arm]-r["y"])**2 for r in rows)/len(rows)
        log_loss = -math.fsum(math.log(r["predictions"][arm] if r["y"] else
                                      1-r["predictions"][arm]) for r in rows)/len(rows)
        result["arms"][arm] = {"brier": brier, "log_loss": log_loss}
    baseline = result["arms"]["publisher"]["brier"]
    for arm in ARMS:
        result["arms"][arm]["brier_delta_vs_publisher"] = (
            result["arms"][arm]["brier"] - baseline)
    return result


def audit(raw: bytes) -> dict:
    rows = load_rows(raw)
    predicted, history = predict_by_date(rows)
    evaluated = [row for row in predicted if row["evaluated"]]
    if not evaluated:
        raise ValueError("sample does not extend beyond fixed warmup")
    by_day, by_week, by_league = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in evaluated:
        by_day[row["day"].isoformat()].append(row)
        monday = row["day"] - timedelta(days=row["day"].weekday())
        by_week[monday.isoformat()].append(row)
        by_league[row["league"]].append(row)
    return {
        "schema": "autosport.account_c.retrospective_calibration_diagnostic.v1",
        "source": {"author": "lathise2025", "url": SOURCE_URL,
                   "reference_commit": SOURCE_COMMIT, "license": "CC BY 4.0",
                   "license_url": "https://creativecommons.org/licenses/by/4.0/",
                   "input_sha256": hashlib.sha256(raw).hexdigest(),
                   "reference_commit_verified_by_caller": False},
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "protocol": {"warmup_calendar_days": WARMUP_DAYS, "ridge": RIDGE,
                     "fixed_shrinkage": 0.5, "within_day_learning": False},
        "truth": {"evidence_class": "EXTERNAL_REPORTED_RETROSPECTIVE_SAMPLE",
                  "upstream_point_in_time_verified": False,
                  "upstream_results_independently_verified": False,
                  "untouched_holdout": False, "forward_evidence_eligible": False,
                  "promotion_authorized": False, "profitability_claim": False,
                  "real_money_execution": False},
        "economic_metrics": {"status": "UNAVAILABLE_NO_ODDS_COSTS_OR_FILLS",
                             "net_profit": None, "roi": None, "drawdown": None},
        "input_count": len(rows), "warmup_count": len(rows)-len(evaluated),
        "first_date": rows[0]["day"].isoformat(),
        "last_date": rows[-1]["day"].isoformat(),
        "evaluation_first_date": evaluated[0]["day"].isoformat(),
        "overall": metrics(evaluated),
        "by_day": {key: metrics(value) for key, value in sorted(by_day.items())},
        "by_week": {key: metrics(value) for key, value in sorted(by_week.items())},
        "by_league": {key: metrics(value) for key, value in sorted(by_league.items())},
        "learning_history": history,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.csv.resolve() == args.output.resolve():
        parser.error("output cannot overwrite input")
    report = audit(args.csv.read_bytes())
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False)+"\n",
                           encoding="utf-8")
    print(json.dumps({"overall": report["overall"], "truth": report["truth"]}))


if __name__ == "__main__":
    main()
