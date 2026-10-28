"""Exploratory train-only classifier on publisher-provided historical features."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import random
from pathlib import Path

from audit_table_tennis_probability_sample import CUTOFF, DATA, load_rows

ITERATIONS = 600
RATE = 0.1
RIDGE = 0.01


def load_features():
    validated = {row[0]: row for row in load_rows()}
    result = []
    with DATA.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            match_id = row["match_id"]
            a, b = float(row["elo_a_pre"]), float(row["elo_b_pre"])
            sa, sb = float(row["sol_a_pre"]), float(row["sol_b_pre"])
            if not all(math.isfinite(x) for x in (a, b, sa, sb)) or not (0 <= sa <= 1 and 0 <= sb <= 1):
                raise ValueError("invalid historical feature")
            if not (500 <= a <= 2500 and 500 <= b <= 2500):
                raise ValueError("rating outside research bounds")
            base = validated[match_id]
            result.append((match_id, base[1], (1.0, (a-b)/100.0, sa-sb), base[4], base[3]))
    return result


def sigmoid(x):
    x = max(-30.0, min(30.0, x))
    return 1 / (1 + math.exp(-x))


def fit(rows):
    training = [row for row in rows if row[1] < CUTOFF]
    if not training:
        raise ValueError("missing training records")
    weights = [0.0, 0.0, 0.0]
    for _ in range(ITERATIONS):
        gradient = [0.0, 0.0, 0.0]
        for _, _, features, outcome, _ in training:
            p = sigmoid(sum(w*x for w, x in zip(weights, features)))
            for i, value in enumerate(features):
                gradient[i] += (p - outcome) * value / len(training)
        for i in range(3):
            weights[i] -= RATE * (gradient[i] + (RIDGE * weights[i] if i else 0.0))
    return weights


def audit():
    rows = load_features()
    weights = fit(rows)
    later = [row for row in rows if row[1] >= CUTOFF]
    if not later:
        raise ValueError("missing evaluation records")
    predictors = {"trained_features": lambda r: sigmoid(sum(w*x for w, x in zip(weights, r[2]))),
                  "publisher": lambda r: r[4], "constant_50": lambda r: .5}
    result = {}
    for name, predict in predictors.items():
        values = [predict(row) for row in later]
        result[name] = {"brier": sum((p-r[3])**2 for p, r in zip(values, later))/len(later),
                        "accuracy": sum((p >= .5)==bool(r[3]) for p,r in zip(values,later))/len(later)}
    groups = {}
    for row in later:
        day = row[1]
        own, supplied = predictors["trained_features"](row), row[4]
        count, total = groups.get(day, (0, 0.0))
        groups[day] = (count + 1, total + (supplied - row[3]) ** 2 - (own - row[3]) ** 2)
    days, rng, boot = sorted(groups), random.Random(19), []
    for _ in range(2000):
        chosen = rng.choices(days, k=len(days))
        boot.append(sum(groups[d][1] for d in chosen) / sum(groups[d][0] for d in chosen))
    boot.sort()
    return {"schema":"autosport.table_tennis_feature_model.research.v1",
            "source_sha256":hashlib.sha256(DATA.read_bytes()).hexdigest(),
            "script_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "cutoff_date":CUTOFF.isoformat(), "training_count":len(rows)-len(later),
            "evaluation_count":len(later), "weights":weights, "iterations":ITERATIONS,
            "learning_rate":RATE, "ridge":RIDGE, "results":result,
            "publisher_minus_feature_brier_day_bootstrap_95_percent": [boot[49], boot[1949]],
            "bootstrap_days":len(days), "bootstrap_draws":2000, "bootstrap_seed":19,
            "publisher_features_point_in_time_independently_verified":False,
            "evaluation_is_untouched_holdout":False, "odds_present":False,
            "profitability_claim":False, "promotion_authorized":False}


if __name__ == "__main__":
    print(json.dumps(audit(), indent=2, sort_keys=True))
