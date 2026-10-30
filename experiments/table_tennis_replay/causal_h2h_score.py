"""Score a frozen in-play market journal only against later admissible source results.

Source labels and clocks are not independently verified; scores are not evidence
of bookmaker fills, independent holdout, profitability, or trained H2H skill.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext
import json
import math
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from canonical_history_bridge import validated_rows
from causal_h2h_observer import verify_journal
from dated_result_audit import audit as validate_followup
from delay_policy_audit import _exact_snapshot
from delayed_portfolio_replay import _session_results, _stable_outcome
from next_day_player_eval import _scores
from record import instant


def evaluate(root, raw, receipt):
    root = Path(root)
    journal, envelope, verified = verify_journal(root)
    followup = validate_followup(root / "snapshots", raw, receipt)
    received = instant(receipt["received_at"])
    # This request started after the journal receipt was actually written. It
    # is a conservative upper bound on the freeze, not official match time.
    decision_bound = instant(verified[(1, "results")]["requested_at"])
    if not instant(envelope["frozen_at"]) < decision_bound:
        raise ValueError("journal was not frozen before the next result request")
    snapshot = _exact_snapshot(root / "snapshots", 1, verified[(1, "odds")])
    timeline = _session_results(root / "snapshots", verified)
    source = strict_json_loads(raw.decode("utf-8"))
    valid, _ = validated_rows(source)
    complete = {row["fixture_id"]: (row, winner) for row, winner in valid}
    reported = {row["fixture_id"]: row for row in source["results"]}
    forecasts = journal.get("forecasts")
    if (not isinstance(forecasts, list) or
            journal.get("counts", {}).get("forecast_fixtures") != len(forecasts)):
        raise ValueError("invalid frozen forecast population")
    ids = [row.get("fixture_id") for row in forecasts]
    if (any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)) or
            any(row.get("h2h_status") != "identity_unresolved" or
                row.get("h2h_home_probability") is not None for row in forecasts)):
        raise ValueError("population contains duplicate IDs or unexpected H2H features")
    counts, rows = Counter(), []
    for forecast in forecasts:
        identity = forecast["fixture_id"]
        state = snapshot.get(identity)
        if state is None:
            raise ValueError("frozen fixture is absent or invalid in source odds")
        try:
            probability = Decimal(forecast["market_home_probability"])
            p = float(probability)
            if not probability.is_finite() or not math.isfinite(p) or not 0 < p < 1:
                raise ValueError("probability outside open interval")
        except (InvalidOperation, TypeError, KeyError, ValueError, OverflowError):
            counts["invalid_probability"] += 1
            continue
        home, away, _league, selections = state
        with localcontext() as ctx:
            ctx.prec = 28
            expected = selections[away][1] / (selections[home][1] + selections[away][1])
        if probability != expected:
            raise ValueError("frozen market probability differs from source quotes")
        if identity not in reported:
            counts["not_present_in_followup"] += 1
            continue
        if identity not in complete:
            counts["invalid_or_unfinished_result"] += 1
            continue
        result_row, winner = complete[identity]
        if instant(result_row["updated_at"]) > received:
            counts["source_update_after_receipt"] += 1
            continue
        # Require the source's full set-score field; absence remains unknown.
        if result_row.get("final_score") != ",".join(p["score"] for p in result_row["periods"]):
            counts["missing_or_mismatched_final_score"] += 1
            continue
        side = _stable_outcome(identity, state[:3], decision_bound, result_row, winner, timeline)
        if side is None:
            counts["identity_time_or_correction_conflict"] += 1
            continue
        counts["eligible"] += 1
        rows.append({"fixture_id": identity, "market": p, "neutral": 0.5,
                     "outcome": int(side == "home")})
    return {"schema": "research.tt_frozen_market_score.v1", "frozen_candidates": len(forecasts),
            "counts": {key: counts[key] for key in ("eligible", "not_present_in_followup",
                       "invalid_or_unfinished_result", "source_update_after_receipt",
                       "missing_or_mismatched_final_score", "identity_time_or_correction_conflict",
                       "invalid_probability")},
            "market": _scores(rows, "market"), "neutral": _scores(rows, "neutral"),
            "rows": rows, "forecast_sha256": envelope["sha256"], "followup_audit": followup,
            "decision_bound": decision_bound.isoformat(), "h2h_scored": 0,
            "source_results_independently_verified": False, "independent_holdout": False,
            "identity_verified": False, "rights_verified": False, "execution_verified": False,
            "profitability_claim": False, "model_training_authorized": False,
            "note": "Selected in-play source records; observational labels and prices, not accepted bets."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observation_directory", type=Path)
    parser.add_argument("followup_body", type=Path)
    parser.add_argument("followup_receipt", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.observation_directory, args.followup_body.read_bytes(),
                      strict_json_loads(args.followup_receipt.read_text("utf-8")))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
