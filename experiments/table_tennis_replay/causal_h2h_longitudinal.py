"""Reconcile two dated follow-ups for one frozen research forecast.

This deliberately compares exactly two receipts. Do not omit an intermediate
receipt or treat a two-receipt report as a complete multi-revision history.
Source timestamps, identity, rights and executable odds are not certified.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from causal_h2h_observer import verify_journal
from causal_h2h_score import evaluate as score_followup
from next_day_player_eval import _scores
from record import instant


def _signature(row):
    """Compare sporting content and last-live time, not an update timestamp."""
    periods = row.get("periods")
    if isinstance(periods, list) and all(isinstance(p, dict) for p in periods):
        periods = tuple(p.get("score") for p in periods)
    else:
        periods = None
    return (row.get("home"), row.get("away"), row.get("league"),
            row.get("sport", "table_tennis"), row.get("status"),
            row.get("final_score"), periods, row.get("finished_at"))


def compare(root, previous_raw, previous_receipt, current_raw, current_receipt):
    """Preserve prior as-of score and quarantine changed/absent later evidence."""
    if instant(previous_receipt.get("received_at")) >= instant(current_receipt.get("requested_at")):
        raise ValueError("later request must start strictly after earlier receipt")
    previous = score_followup(root, previous_raw, previous_receipt)
    current = score_followup(root, current_raw, current_receipt)
    if (previous["forecast_sha256"] != current["forecast_sha256"] or
            previous["followup_audit"]["source_date"] != current["followup_audit"]["source_date"]):
        raise ValueError("follow-ups must share frozen forecast and source date")

    # score_followup has already checked strict JSON, unique IDs and receipts.
    earlier_source = strict_json_loads(previous_raw.decode("utf-8"))
    later_source = strict_json_loads(current_raw.decode("utf-8"))
    earlier = {r["fixture_id"]: r for r in earlier_source["results"]}
    later = {r["fixture_id"]: r for r in later_source["results"]}
    earlier_scored = {r["fixture_id"]: r for r in previous["rows"]}
    later_scored = {r["fixture_id"]: r for r in current["rows"]}
    journal, _envelope, _verified = verify_journal(Path(root))
    candidates = [r["fixture_id"] for r in journal["forecasts"]]
    counts, decisions, admitted = Counter(), [], []
    for fixture in candidates:
        was_scored = fixture in earlier_scored
        now_scored = fixture in later_scored
        if was_scored and fixture not in later:
            category = "previously_eligible_missing"
        elif was_scored:
            if (not now_scored or _signature(earlier[fixture]) != _signature(later[fixture]) or
                    earlier_scored[fixture] != later_scored[fixture]):
                category = "quarantined"
            else:
                category = "unchanged_eligible"
                admitted.append(later_scored[fixture])
        elif now_scored:
            # A repaired previously-finished result is a correction, even if
            # the earlier invalid row was never eligible for scoring.
            if fixture in earlier and earlier[fixture].get("status") == "finished":
                category = "quarantined"
            else:
                category = "newly_eligible"
                admitted.append(later_scored[fixture])
        else:
            category = "unresolved"
        counts[category] += 1
        decisions.append({"fixture_id": fixture, "category": category})

    keys = ("unchanged_eligible", "newly_eligible", "previously_eligible_missing",
            "quarantined", "unresolved")
    return {"schema": "research.tt_two_followup_reconciliation.v1",
            "forecast_sha256": previous["forecast_sha256"],
            "frozen_candidates": len(candidates),
            "previous_as_of": previous_receipt["received_at"],
            "current_as_of": current_receipt["received_at"],
            "previous_report": previous,
            "current_source_audit": current["followup_audit"],
            "current_scorer_counts_before_reconciliation": current["counts"],
            "reconciliation_counts": {key: counts[key] for key in keys},
            "current_eligible": len(admitted),
            "current_market": _scores(admitted, "market"),
            "current_neutral": _scores(admitted, "neutral"),
            "current_rows": admitted, "decisions": decisions,
            "identity_verified": False, "rights_verified": False,
            "source_results_independently_verified": False, "independent_holdout": False,
            "bookmaker_acceptance_verified": False, "model_training_authorized": False,
            "real_money_execution": False, "profitability_claim": False,
            "note": "Exactly two receipts; later unknown/conflicting labels are not losses. "
                    "No third receipt or intervening revision is represented."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observation_directory", type=Path)
    parser.add_argument("previous_body", type=Path)
    parser.add_argument("previous_receipt", type=Path)
    parser.add_argument("current_body", type=Path)
    parser.add_argument("current_receipt", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = compare(args.observation_directory, args.previous_body.read_bytes(),
                     strict_json_loads(args.previous_receipt.read_text("utf-8")),
                     args.current_body.read_bytes(),
                     strict_json_loads(args.current_receipt.read_text("utf-8")))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
