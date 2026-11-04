"""Reconcile recorded outcome revisions for frozen TT strength candidates.

Research-only: externally retain both forecast pins and the latest history pin.
No source entitlement, participant identity, truthful clocks, unseen revisions,
independent holdout, training, executable odds, or betting is established.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from causal_h2h_revision_history import _append_followup, _evaluate_history
from causal_strength_frozen import evaluate as score_strength
from next_day_player_eval import _scores
from prospective_capture import _write_exclusive


def _strength_scorer(forecast_sha256, forecast_receipt_sha256):
    """Adapt the existing strength scorer to the existing reconciliation API."""
    def score(root, body, receipt):
        result = score_strength(root, body, receipt,
                                expected_forecast_sha256=forecast_sha256,
                                expected_receipt_sha256=forecast_receipt_sha256)
        return {**result, "counts": result["outcome_counts"]}
    return score


def _cohort(rows, keys):
    return {"matches": len(rows), "fixture_ids": [row["fixture_id"] for row in rows],
            "scores": {key: _scores(rows, key) for key in keys}}


def evaluate_history(root, directory, expected_head, *, expected_forecast_sha256,
                     expected_forecast_receipt_sha256):
    """Score recorded revisions as of each receipt, with persistent quarantine.

    The three pins are caller-supplied. A valid chain does not establish that
    pins were retained externally on time, are latest, or include every read.
    """
    scorer = _strength_scorer(expected_forecast_sha256, expected_forecast_receipt_sha256)
    report = _evaluate_history(root, directory, expected_head, scorer=scorer,
                               forecast_receipt_sha256=expected_forecast_receipt_sha256)
    for snapshot in report["snapshots"]:
        admitted = snapshot["rows"]
        form = [row for row in admitted if row["form"] is not None]
        h2h = [row for row in admitted if row["form_h2h"] is not None]
        snapshot["all_eligible_market"] = _cohort(admitted, ("market",))
        snapshot["matched_form"] = _cohort(form, ("market", "form"))
        snapshot["matched_h2h"] = _cohort(h2h, ("market", "form", "form_h2h"))
    report.update(schema="research.tt_frozen_strength_revision_history.v1",
                  forecast_receipt_sha256=expected_forecast_receipt_sha256,
                  calibration_verified=False, execution_verified=False,
                  note=("Paired scores use only each receipt's non-quarantined rows. "
                        "Pins, completeness, clocks, identities and source truth require independent evidence."))
    return report


def append_followup(root, directory, body, receipt_bytes, *, expected_head,
                    expected_forecast_sha256, expected_forecast_receipt_sha256):
    """Append one dated response to a single-writer history; never overwrite.

    Each v2 link binds the externally pinned forecast and forecast receipt, its
    source body and receipt, and the predecessor. The returned latest link SHA
    must be retained independently before the next response. Partial triplets
    and omitted, concurrently written or unrecorded source reads need review.
    """
    scorer = _strength_scorer(expected_forecast_sha256, expected_forecast_receipt_sha256)
    return _append_followup(root, directory, body, receipt_bytes,
                            expected_head=expected_head, scorer=scorer,
                            forecast_receipt_sha256=expected_forecast_receipt_sha256)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    append = sub.add_parser("append", help="append one recorded dated result")
    append.add_argument("root", type=Path)
    append.add_argument("history", type=Path)
    append.add_argument("body", type=Path)
    append.add_argument("receipt", type=Path)
    append.add_argument("--expected-head", help="omit only for an empty history")
    score = sub.add_parser("score", help="score all recorded receipts")
    score.add_argument("root", type=Path)
    score.add_argument("history", type=Path)
    score.add_argument("output", type=Path)
    score.add_argument("--expected-head", required=True)
    for command in (append, score):
        command.add_argument("--expected-forecast-sha256", required=True)
        command.add_argument("--expected-forecast-receipt-sha256", required=True)
    args = parser.parse_args()
    pins = {"expected_forecast_sha256": args.expected_forecast_sha256,
            "expected_forecast_receipt_sha256": args.expected_forecast_receipt_sha256}
    if args.command == "append":
        result = {"latest_link_sha256": append_followup(
            args.root, args.history, args.body.read_bytes(), args.receipt.read_bytes(),
            expected_head=args.expected_head, **pins)}
    else:
        result = evaluate_history(args.root, args.history, args.expected_head, **pins)
        _write_exclusive(args.output,
                         (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
