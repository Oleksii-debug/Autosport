"""Exploratory calibration on an attributed public table-tennis sample.

The supplied probabilities and results are publisher assertions, not independently
verified point-in-time evidence. This file has no executable odds or settlements.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "research/data/tt_ratings_sample_2000.csv"
CUTOFF = date(2026, 9, 1)


def load_rows(path=DATA):
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"match_id", "date", "league", "p_model", "winner", "sets_a", "sets_b"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("required sample fields missing")
        rows = []
        seen = set()
        for row in reader:
            match_id = row["match_id"]
            if not match_id or match_id in seen:
                raise ValueError("missing or duplicate match identity")
            seen.add(match_id)
            day = date.fromisoformat(row["date"])
            probability = float(row["p_model"])
            if not math.isfinite(probability) or not 0 <= probability <= 1:
                raise ValueError("probability outside 0..1")
            winner = row["winner"]
            a, b = int(row["sets_a"]), int(row["sets_b"])
            if winner not in {"A", "B"} or a < 0 or b < 0 or a == b or (winner == "A") != (a > b):
                raise ValueError("winner does not match recorded set score")
            rows.append((match_id, day, row["league"], probability, int(winner == "A")))
    if not rows:
        raise ValueError("empty sample")
    return rows


def fit_shrink(rows):
    """Fit a bounded Brier shrinkage coefficient using pre-cutoff dates only."""
    training = [r for r in rows if r[1] < CUTOFF]
    if not training:
        raise ValueError("no training rows")
    denominator = sum((r[3] - 0.5) ** 2 for r in training)
    if denominator == 0:
        return 0.0
    numerator = sum((r[3] - 0.5) * (r[4] - 0.5) for r in training)
    return max(0.0, min(1.0, numerator / denominator))


def metrics(rows, alpha):
    if not rows:
        raise ValueError("empty evaluation")
    probs = [0.5 + alpha * (r[3] - 0.5) for r in rows]
    return {"count": len(rows),
            "brier": sum((p - r[4]) ** 2 for p, r in zip(probs, rows)) / len(rows),
            "accuracy": sum((p >= 0.5) == bool(r[4]) for p, r in zip(probs, rows)) / len(rows)}


def audit(path=DATA):
    rows = load_rows(path)
    training = [r for r in rows if r[1] < CUTOFF]
    evaluation = [r for r in rows if r[1] >= CUTOFF]
    alpha = fit_shrink(rows)
    groups = {}
    for league in sorted({r[2] for r in evaluation}):
        selected = [r for r in evaluation if r[2] == league]
        if len(selected) >= 30:
            groups[league] = {"publisher": metrics(selected, 1.0), "shrunk": metrics(selected, alpha)}
    return {"schema": "autosport.table_tennis_probability_sample.research.v1",
            "source_repository": "https://github.com/lathise2025/table-tennis-elo-dataset",
            "source_revision": "9eba2ade435ce27b4f3dcbd5aafc6bfbcd52d534",
            "source_license": "CC BY 4.0; sample only",
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "analysis_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "cutoff_date": CUTOFF.isoformat(), "training_count": len(training),
            "evaluation_count": len(evaluation), "shrink_coefficient": alpha,
            "date_only": True, "publisher_times_independently_verified": False,
            "evaluation_is_untouched_holdout": False, "odds_present": False,
            "profitability_claim": False, "promotion_authorized": False,
            "evaluation": {"publisher": metrics(evaluation, 1.0),
                           "train_fitted_shrink": metrics(evaluation, alpha),
                           "constant_50_percent": metrics(evaluation, 0.0)},
            "league_groups": groups}


if __name__ == "__main__":
    print(json.dumps(audit(), indent=2, sort_keys=True))
