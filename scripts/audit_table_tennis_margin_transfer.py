"""Post-hoc transfer of a fixed research rating, never a betting result."""
from __future__ import annotations

import csv
import hashlib
import json
import random
from pathlib import Path

from audit_table_tennis_probability_sample import DATA, CUTOFF, load_rows
from audit_setka_margin_rating import day_predictions
from audit_setka_result_history import brier


def audit():
    validated = {r[0]: r for r in load_rows()}
    with DATA.open(encoding="utf-8", newline="") as handle:
        raw = list(csv.DictReader(handle))
    rows = [(r["match_id"], validated[r["match_id"]][1], r["player_a"], r["player_b"],
             validated[r["match_id"]][4]) for r in raw]
    # The research model accepts set margins up to three. A larger final margin
    # is clipped only when updating after the match's day; no row is selected
    # or excluded according to its future result.
    margins = {r["match_id"]: min(3, abs(int(r["sets_a"]) - int(r["sets_b"]))) for r in raw}
    match = day_predictions(rows, margins, k=10.0, weight=0.0)
    margin = day_predictions(rows, margins, k=20.0, weight=1.0)
    evaluation = [r for r in rows if r[1] >= CUTOFF]
    days = sorted({r[1] for r in evaluation})
    day_diffs, day_counts = {}, {}
    for day in days:
        subset = [r for r in evaluation if r[1] == day]
        day_diffs[day.isoformat()] = brier(subset, match) - brier(subset, margin)
        day_counts[day.isoformat()] = len(subset)
    keys = sorted(day_diffs)
    rng = random.Random(19)
    bootstrap = []
    for _ in range(2000):
        selected_days = rng.choices(keys, k=len(keys))
        bootstrap.append(sum(day_diffs[d] * day_counts[d] for d in selected_days) /
                         sum(day_counts[d] for d in selected_days))
    bootstrap.sort()
    return {"schema": "autosport.table_tennis_margin_transfer.research.v1",
            "source_sha256": hashlib.sha256(DATA.read_bytes()).hexdigest(),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "margin_model_sha256": hashlib.sha256((Path(__file__).resolve().parent /
                "audit_setka_margin_rating.py").read_bytes()).hexdigest(),
            "cutoff_date": CUTOFF.isoformat(), "evaluation_count": len(evaluation),
            "clipped_margin_count_total": sum(abs(int(r["sets_a"])-int(r["sets_b"])) > 3 for r in raw),
            "match_only_k10_brier": brier(evaluation, match),
            "margin_k20_brier": brier(evaluation, margin),
            "constant_50_brier": .25,
            "day_count": len(days), "days_margin_better": sum(x > 0 for x in day_diffs.values()),
            "days_margin_worse": sum(x < 0 for x in day_diffs.values()),
            "day_bootstrap_95_percent_interval": [bootstrap[49], bootstrap[1949]],
            "day_bootstrap_draws": 2000, "day_bootstrap_seed": 19,
            "per_day_brier_difference": day_diffs,
            "posthoc_transfer": True, "sample_is_incomplete": True,
            "evaluation_is_untouched_holdout": False, "odds_present": False,
            "profitability_claim": False, "promotion_authorized": False}


if __name__ == "__main__":
    print(json.dumps(audit(), indent=2, sort_keys=True))
