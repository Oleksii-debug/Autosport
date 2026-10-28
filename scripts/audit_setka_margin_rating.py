"""Exploratory set-margin rating; no odds, no production authorization."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path

from audit_setka_result_history import CUTOFF, SOURCE_SHA256, brier, load_history
from audit_table_tennis_day_batch_elo import K_CANDIDATES

WEIGHTS = (0.0, 0.5, 1.0)


def day_predictions(rows, margins, *, k, weight):
    if k not in K_CANDIDATES or weight not in WEIGHTS:
        raise ValueError("unsupported research parameter")
    days = defaultdict(list)
    for row in rows:
        days[row[1]].append(row)
    ratings = defaultdict(lambda: 1500.0)
    result = {}
    for day in sorted(days):
        increments = defaultdict(float)
        for match_id, _, a, b, winner in sorted(days[day]):
            margin = margins[match_id]
            if type(margin) is not int or not 1 <= margin <= 3:
                raise ValueError("unsupported set margin")
            probability = 1 / (1 + 10 ** ((ratings[b] - ratings[a]) / 400))
            result[match_id] = probability
            multiplier = 1 + weight * (margin / 3 - 1)
            change = k * (winner - probability) * multiplier
            increments[a] += change
            increments[b] -= change
        for participant, change in increments.items():
            ratings[participant] += change
    return result


def fit_pre_cutoff(rows, margins):
    training = [row for row in rows if row[1] < CUTOFF]
    if not training:
        raise ValueError("no training records")
    losses = {}
    for weight in WEIGHTS:
        for k in K_CANDIDATES:
            predictions = day_predictions(training, margins, k=k, weight=weight)
            losses[(k, weight)] = brier(training, predictions)
    best = min(losses, key=lambda item: (losses[item], item))
    return best, losses


def audit(path):
    rows, excluded = load_history(path)
    with path.open(encoding="utf-8", newline="") as handle:
        margins = {r["X"]: abs(int(r["Sets_P1"]) - int(r["Sets_P2"])) for r in csv.DictReader(handle)}
    best, losses = fit_pre_cutoff(rows, margins)
    later = [r for r in rows if r[1] >= CUTOFF]
    if not later:
        raise ValueError("no evaluation records")
    selected = day_predictions(rows, margins, k=best[0], weight=best[1])
    baseline = day_predictions(rows, margins, k=10.0, weight=0.0)
    days = sorted({r[1] for r in later})
    by_day = {}
    for day in days:
        subset = [r for r in later if r[1] == day]
        by_day[day.isoformat()] = {"count": len(subset),
            "match_minus_margin_brier": brier(subset, baseline) - brier(subset, selected)}
    keys, rng, boot = sorted(by_day), random.Random(19), []
    for _ in range(2000):
        chosen_days = rng.choices(keys, k=len(keys))
        boot.append(sum(by_day[d]["count"] * by_day[d]["match_minus_margin_brier"] for d in chosen_days) /
                    sum(by_day[d]["count"] for d in chosen_days))
    boot.sort()
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"schema": "autosport.setka_margin_rating.research.v1", "source_sha256": sha,
            "known_source_hash_match": sha == SOURCE_SHA256, "source_rights": "UNKNOWN",
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "cutoff_date": CUTOFF.isoformat(), "excluded_unresolved_ids": excluded,
            "training_count": len(rows) - len(later), "evaluation_count": len(later),
            "chosen_k": best[0], "chosen_margin_weight": best[1],
            "train_selected_brier": losses[best],
            "evaluation_selected_brier": brier(later, selected),
            "evaluation_match_elo_k10_brier": brier(later, baseline),
            "per_day_brier_differences": by_day,
            "day_bootstrap_95_percent_interval": [boot[49], boot[1949]],
            "day_bootstrap_draws": 2000, "day_bootstrap_seed": 19,
            "evaluation_constant_brier": 0.25, "date_source_conflict": True if sha == SOURCE_SHA256 else None,
            "identity_independently_verified": False, "evaluation_is_untouched_holdout": False,
            "odds_present": False, "profitability_claim": False, "promotion_authorized": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    print(json.dumps(audit(parser.parse_args().csv), indent=2, sort_keys=True))
