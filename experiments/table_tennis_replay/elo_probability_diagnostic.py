"""Retrospective descriptive scores of publisher-supplied TT probabilities.

The selected sample, outcomes and date range were already inspected. Publisher
probabilities, person IDs, source rights, fit times and match/result times are
not verified. No model is fitted, calibrated, promoted or used for a bet.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from decimal import Decimal, InvalidOperation, localcontext
import io
import json
import math
from pathlib import Path
import random

from elo_sample_inventory import _score_shape
from elo_split_graph_audit import _component_membership, _cutoff, _read_rows
from next_day_player_eval import _scores

REPEATS = 2_000
SEED = 20261104


def _probability(raw):
    try:
        value = Decimal(raw)
        f = float(value)
        if not value.is_finite() or not 0 < value < 1 or not math.isfinite(f) or not 0 < f < 1:
            return None
        return value, f
    except (InvalidOperation, OverflowError, TypeError, ValueError):
        return None


def _resample(rows):
    """Paired component resampling; labels remain internal, not verified people."""
    groups = defaultdict(list)
    for row in rows:
        p, y = row["publisher"], row["outcome"]
        groups[row["component"]].append((p - y) ** 2 - (0.5 - y) ** 2)
    clusters = [(sum(values), len(values)) for _, values in sorted(groups.items())]
    base = {"components": len(clusters),
            "largest_component_rows": max((n for _, n in clusters), default=0),
            "replicates": 0, "seed": SEED, "lower_2_5_percentile": None,
            "upper_97_5_percentile": None, "status": "insufficient_components"}
    if len(clusters) < 2:
        return base
    rng = random.Random(SEED)
    samples = []
    for _ in range(REPEATS):
        drawn = [rng.choice(clusters) for _ in clusters]
        samples.append(sum(value for value, _ in drawn) / sum(n for _, n in drawn))
    samples.sort()
    base.update(replicates=REPEATS, status="descriptive_only",
                lower_2_5_percentile=samples[int((REPEATS - 1) * .025)],
                upper_97_5_percentile=samples[int((REPEATS - 1) * .975)])
    return base


def _decimal_check(rows, publisher, neutral):
    """Recalculate same cohort via Decimal, independent of the float scorer."""
    if not rows:
        return "no_eligible_rows"
    with localcontext() as ctx:
        ctx.prec = 50
        count = Decimal(len(rows))
        model_brier = sum((r["decimal_p"] - r["outcome"]) ** 2 for r in rows) / count
        neutral_brier = sum((Decimal("0.5") - r["outcome"]) ** 2 for r in rows) / count
        model_loss = -sum((r["decimal_p"] if r["outcome"] else 1 - r["decimal_p"]).ln()
                          for r in rows) / count
        neutral_loss = -Decimal("0.5").ln()
    comparisons = ((publisher["brier"], model_brier), (publisher["log_loss"], model_loss),
                   (neutral["brier"], neutral_brier), (neutral["log_loss"], neutral_loss))
    if any(not math.isclose(actual, float(exact), rel_tol=1e-12, abs_tol=1e-12)
           for actual, exact in comparisons):
        raise ValueError("float and Decimal scoring disagree")
    return "matched_1e-12"


def _cohort(rows):
    publisher = _scores(rows, "publisher")
    neutral = _scores(rows, "neutral")
    return {"eligible_rows": len(rows), "publisher": publisher, "neutral": neutral,
            "publisher_minus_neutral_brier": (None if publisher is None else
                                                publisher["brier"] - neutral["brier"]),
            "decimal_crosscheck": _decimal_check(rows, publisher, neutral),
            "component_resampling": _resample(rows)}


def audit(raw, *, expected_sha256, cutoff_date):
    cutoff = _cutoff(cutoff_date)
    summary, structural = _read_rows(raw, expected_sha256)
    components = _component_membership(structural, league_scoped=False)
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    excluded, later_excluded, eligible = Counter(), Counter(), []
    for index, row in enumerate(reader):
        day = structural[index][0]
        reason = None
        if _score_shape(row) != "terminal_shape_format_unverified":
            reason = "score_or_winner_shape_unqualified"
        probability = _probability(row["p_model"])
        if reason is None and probability is None:
            reason = "probability_not_strictly_inside_zero_one"
        if reason is not None:
            excluded[reason] += 1
            if day >= cutoff:
                later_excluded[reason] += 1
            continue
        exact, value = probability
        eligible.append({"publisher": value, "neutral": .5,
                         "outcome": int(row["winner"] == "A"), "decimal_p": exact,
                         "component": components[index], "date": day})
    later = [row for row in eligible if row["date"] >= cutoff]
    keys = ("score_or_winner_shape_unqualified", "probability_not_strictly_inside_zero_one")
    return {"schema": "research.tt_publisher_probability_diagnostic.v1",
            "source_sha256": summary["source_sha256"], "source_rows": len(structural),
            "cutoff_date_label": cutoff.isoformat(),
            "exclusions_all": {key: excluded[key] for key in keys},
            "exclusions_later": {key: later_excluded[key] for key in keys},
            "all_eligible": _cohort(eligible), "later_date_label_eligible": _cohort(later),
            "source_ids_verified_as_people": False, "physical_results_verified": False,
            "source_rights_verified": False, "prediction_time_verified": False,
            "model_fit_provenance_verified": False, "independent_holdout": False,
            "calibration_verified": False, "market_odds_available": False,
            "model_training_authorized": False, "bookmaker_execution": False,
            "profitability_claim": False,
            "note": "Retrospective publisher values, not an independent validation. "
                    "The component range is descriptive, not a certified confidence interval; "
                    "the graph uses the entire known sample and is not a causal feature."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--cutoff-date", required=True)
    args = parser.parse_args()
    result = audit(args.sample.read_bytes(), expected_sha256=args.expected_sha256,
                   cutoff_date=args.cutoff_date)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
