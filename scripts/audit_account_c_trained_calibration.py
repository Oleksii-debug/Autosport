"""Chronological, retrospective probability-training experiment; not runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from audit_account_c_full_tennis import SOURCE, adapt
from audit_account_c_outcome_odds import EDGE, FEE, STAKE, fresh_arm, load_data, record

TRAIN_START = date(2024, 12, 27)
EVAL_START = date(2025, 7, 1)
EVAL_END = date(2026, 1, 1)
ITERATIONS = 500


def sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-min(value, 700.0)))
    exp = math.exp(max(value, -700.0))
    return exp / (1.0 + exp)


def logit(value: float) -> float:
    value = min(1.0 - 1e-12, max(1e-12, value))
    return math.log(value / (1.0 - value))


def fit(examples: list[tuple[float, float, int]]) -> tuple[float, float]:
    if not examples:
        raise ValueError("no usable training examples")
    intercept, weight = 0.0, 0.0
    for _ in range(ITERATIONS):
        errors = [(sigmoid(logit_market + intercept + weight * elo_logit) - y, elo_logit)
                  for logit_market, elo_logit, y in examples]
        grad_b = math.fsum(error for error, _ in errors) / len(errors) + .01 * intercept
        grad_w = math.fsum(error*x for error, x in errors) / len(errors) + .1 * weight
        intercept -= .4 * grad_b
        weight -= .4 * grad_w
    return intercept, weight


def serialise_arm(arm: dict) -> dict:
    def clean(value):
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        return value
    result = clean({k: v for k, v in arm.items() if k not in ("peak", "equity")})
    result["roi"] = str(arm["net_profit"] / arm["turnover"]) if arm["turnover"] else None
    return result


def evaluate(raw: bytes) -> dict:
    adapted, adapter_counts = adapt(raw)
    rows, _ = load_data([("adapted", adapted)])
    days = defaultdict(list)
    for row in rows:
        if TRAIN_START <= row["at"].date() < EVAL_END:
            days[row["at"].date()].append(row)
    ratings, games = defaultdict(lambda: 1500.0), defaultdict(int)
    training = []
    coefficients = None
    arms = {name: fresh_arm() for name in ("no_bet", "market_favorite", "trained_calibration", "trained_price_guarded")}
    scores = {name: {"brier": [], "log_loss": []} for name in ("elo", "market_proxy", "trained_calibration")}
    eligible = 0
    underround_eligible, underround_selected = 0, 0
    for day, batch in sorted(days.items()):
        if day >= EVAL_START and coefficients is None:
            coefficients = fit(training)
        changes = defaultdict(list)
        for row in batch:
            elo = 1.0 / (1.0 + 10.0 ** ((ratings[row["b"]] - ratings[row["a"]]) / 400.0))
            if row["score_ok"] and row["odds_a"] is not None:
                inv_a, inv_b = Decimal(1)/row["odds_a"], Decimal(1)/row["odds_b"]
                market = float(inv_a / (inv_a+inv_b))
                y = int(row["winner"] == row["a"])
                if day < EVAL_START:
                    training.append((logit(market), logit(elo), y))
                else:
                    eligible += 1
                    intercept, weight = coefficients
                    calibrated = sigmoid(logit(market)+intercept+weight*logit(elo))
                    for name, probability in (("elo", elo), ("market_proxy", market),
                                               ("trained_calibration", calibrated)):
                        scores[name]["brier"].append((probability-y)**2)
                        scores[name]["log_loss"].append(-math.log(probability if y else 1-probability))
                    record(arms["no_bet"], None, row)
                    favorite = row["a"] if row["odds_a"] <= row["odds_b"] else row["b"]
                    record(arms["market_favorite"], favorite, row)
                    p = Decimal(str(calibrated))
                    edge_a, edge_b = p-inv_a, (Decimal(1)-p)-inv_b
                    candidate = (row["a"] if edge_a >= EDGE and edge_a >= edge_b else
                                 row["b"] if edge_b >= EDGE else None)
                    candidate = candidate if games[row["a"]] and games[row["b"]] else None
                    underround = inv_a + inv_b < Decimal(1)
                    underround_eligible += int(underround)
                    underround_selected += int(underround and candidate is not None)
                    record(arms["trained_calibration"], candidate, row)
                    record(arms["trained_price_guarded"], None if underround else candidate, row)
            if row["score_ok"]:
                delta = 16.0 * ((1.0 if row["winner"] == row["a"] else 0.0) - elo)
                changes[row["a"]].append(delta)
                changes[row["b"]].append(-delta)
        for player, deltas in sorted(changes.items()):
            ratings[player] += math.fsum(deltas)
            games[player] += len(deltas)
    if coefficients is None or not eligible:
        raise ValueError("need usable training and evaluation periods")
    return {"schema": "autosport.account_c.trained_calibration.development.v1",
        "source": {"url": SOURCE, "license": "CC BY 4.0",
                   "attribution": "minmaxxxer, ATP/WTA closing odds 2025",
                   "text_snapshot_sha256": hashlib.sha256(raw).hexdigest(),
                   "adapter_sha256": hashlib.sha256(Path(__file__).with_name("audit_account_c_full_tennis.py").read_bytes()).hexdigest(),
                   "economic_evaluator_sha256": hashlib.sha256(Path(__file__).with_name("audit_account_c_outcome_odds.py").read_bytes()).hexdigest(),
                   "source_and_time_independently_verified": False},
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "protocol": {"training_start": TRAIN_START.isoformat(), "evaluation_start": EVAL_START.isoformat(),
            "evaluation_end_exclusive": EVAL_END.isoformat(), "iterations": ITERATIONS, "step": .4,
            "weight_l2": .1, "intercept_l2": .01, "elo_k": 16,
            "edge_threshold": str(EDGE), "stake": str(STAKE), "fee": str(FEE),
            "within_day_learning": False, "best_closing_prices_executable": False},
        "training": {"examples": len(training), "intercept": coefficients[0], "elo_weight": coefficients[1]},
        "evaluation_count": eligible, "adapter_counts": adapter_counts,
        "price_diagnostics": {"underround_eligible": underround_eligible,
                              "underround_selected_by_original": underround_selected,
                              "simultaneous_quotes_verified": False},
        "probability_scores": {name: {metric: math.fsum(vals)/len(vals) for metric, vals in metrics.items()}
                               for name, metrics in scores.items()},
        "arms": {name: serialise_arm(arm) for name, arm in arms.items()},
        "truth": {"historical_data_already_consumed": True, "untouched_holdout": False,
                  "upstream_point_in_time_verified": False, "actual_price_fills_verified": False,
                  "forward_evidence_eligible": False, "promotion_authorized": False,
                  "profitability_claim": False, "real_money_execution": False}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("output cannot overwrite input")
    report = evaluate(args.input.read_bytes())
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False)+"\n", encoding="utf-8")
    print(json.dumps({"training": report["training"], "evaluation_count": report["evaluation_count"],
                      "scores": report["probability_scores"], "arms": report["arms"], "truth": report["truth"]}))
