"""Exploratory day-batched Elo over a sparse attributed table-tennis sample."""
from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from audit_table_tennis_probability_sample import DATA, CUTOFF, load_rows

K = 20.0
INITIAL = 1500.0
K_CANDIDATES = (0.0, 5.0, 10.0, 20.0, 40.0, 80.0)


def predict_day_batched(rows, *, k=K):
    """Use each day's pre-day ratings for every match, update only after the day."""
    if k not in K_CANDIDATES:
        raise ValueError("unsupported research K")
    days = defaultdict(list)
    seen = set()
    for row in rows:
        match_id, day, player_a, player_b, outcome = row
        if not match_id or match_id in seen or not player_a or not player_b or player_a == player_b:
            raise ValueError("invalid match or player identity")
        if not isinstance(day, date) or type(outcome) is not int or outcome not in {0, 1}:
            raise ValueError("invalid match date or result")
        seen.add(match_id)
        days[day].append(row)
    ratings = defaultdict(lambda: INITIAL)
    predictions = {}
    for day in sorted(days):
        increments = defaultdict(float)
        for match_id, _, a, b, outcome in sorted(days[day]):
            p = 1 / (1 + 10 ** ((ratings[b] - ratings[a]) / 400.0))
            predictions[match_id] = p
            change = k * (outcome - p)
            increments[a] += change
            increments[b] -= change
        for player, change in increments.items():
            ratings[player] += change
    return predictions


def fit_k_before_cutoff(rows):
    """Choose K using only pre-cutoff day-batched predictions and outcomes."""
    training = [row for row in rows if row[1] < CUTOFF]
    if not training:
        raise ValueError("no pre-cutoff rows")
    losses = {}
    for k in K_CANDIDATES:
        predictions = predict_day_batched(training, k=k)
        losses[k] = sum((predictions[row[0]] - row[4]) ** 2 for row in training) / len(training)
    return min(K_CANDIDATES, key=lambda candidate: (losses[candidate], candidate)), losses


def audit():
    # Reuse the stricter common validator for IDs, date, probability and score.
    validated = load_rows()
    with DATA.open(encoding="utf-8", newline="") as handle:
        source = list(csv.DictReader(handle))
    indexed = {r[0]: r for r in validated}
    rows = [(r["match_id"], indexed[r["match_id"]][1], r["player_a"], r["player_b"],
             indexed[r["match_id"]][4]) for r in source]
    predictions = predict_day_batched(rows)
    selected_k, training_losses = fit_k_before_cutoff(rows)
    fitted_predictions = predict_day_batched(rows, k=selected_k)
    evaluation = [r for r in validated if r[1] >= CUTOFF]
    results = {}
    for name, predictor in (("sample_only_elo", lambda r: predictions[r[0]]),
                            ("training_selected_elo", lambda r: fitted_predictions[r[0]]),
                            ("publisher", lambda r: r[3]),
                            ("constant_50", lambda r: 0.5)):
        results[name] = {
            "brier": sum((predictor(r) - r[4]) ** 2 for r in evaluation) / len(evaluation),
            "accuracy": sum((predictor(r) >= 0.5) == bool(r[4]) for r in evaluation) / len(evaluation),
        }
    return {"schema": "autosport.table_tennis_day_batch_elo.research.v1", "k": K,
            "training_selected_k": selected_k, "training_candidate_brier": training_losses,
            "initial_rating": INITIAL, "cutoff_date": CUTOFF.isoformat(),
            "data_sha256": hashlib.sha256(DATA.read_bytes()).hexdigest(),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "results": results, "evaluation_count": len(evaluation),
            "sample_is_incomplete": True, "date_only": True,
            "evaluation_is_untouched_holdout": False, "odds_present": False,
            "profitability_claim": False, "promotion_authorized": False}


if __name__ == "__main__":
    print(json.dumps(audit(), indent=2, sort_keys=True))
