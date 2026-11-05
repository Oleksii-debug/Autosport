"""Position-specific source-string coverage for a pinned in-play example.

No estimator fit, prediction, identity resolution, or financial authority.
Source date labels, source names and outcome availability remain unverified.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from ligapro_notebook_inventory import (
    REFERENCE_PAIR_CAP, _load_notebook_population, _notebook_split, _pair,
    audit as prior_audit,
)


def _positions(item, sets):
    a, b, _ = item
    def label(value, own, other):
        if value in own:
            return "same_position"
        return "other_position_only" if value in other else "unseen_anywhere"
    return label(a, sets[0], sets[1]), label(b, sets[1], sets[0])


def _indexed(train, test):
    by_a = {a for a, _, _ in train}
    by_b = {b for _, b, _ in train}
    pairs = {(a, b) for a, b, _ in train}
    latest_a, latest_b, latest_pair = {}, {}, {}
    for a, b, when in train:
        if when is None:
            continue
        for lookup, key in ((latest_a, a), (latest_b, b), (latest_pair, (a, b))):
            if key not in lookup or lookup[key] < when:
                lookup[key] = when
    counts = {"player1": {k: 0 for k in ("same_position", "other_position_only", "unseen_anywhere")},
              "player2": {k: 0 for k in ("same_position", "other_position_only", "unseen_anywhere")},
              "test_rows_any_position_unknown": 0, "test_rows_both_positions_unknown": 0,
              "test_rows_same_oriented_pair_seen": 0, "test_rows_reverse_only_pair_seen": 0,
              "test_rows_later_same_position_player1": 0, "test_rows_later_same_position_player2": 0,
              "test_rows_later_any_same_position": 0, "test_rows_later_both_same_position": 0,
              "test_rows_later_same_orientation_pair": 0,
              "test_rows_later_reverse_orientation_pair": 0,
              "test_rows_later_either_orientation_pair": 0}
    for a, b, when in test:
        x, y = _positions((a, b, when), (by_a, by_b))
        counts["player1"][x] += 1
        counts["player2"][y] += 1
        counts["test_rows_any_position_unknown"] += x != "same_position" or y != "same_position"
        counts["test_rows_both_positions_unknown"] += x != "same_position" and y != "same_position"
        same, reverse = (a, b) in pairs, (b, a) in pairs
        counts["test_rows_same_oriented_pair_seen"] += same
        counts["test_rows_reverse_only_pair_seen"] += reverse and not same
        if when is None:
            continue
        later_a = a in latest_a and latest_a[a] > when
        later_b = b in latest_b and latest_b[b] > when
        later_same = (a, b) in latest_pair and latest_pair[a, b] > when
        later_reverse = (b, a) in latest_pair and latest_pair[b, a] > when
        counts["test_rows_later_same_position_player1"] += later_a
        counts["test_rows_later_same_position_player2"] += later_b
        counts["test_rows_later_any_same_position"] += later_a or later_b
        counts["test_rows_later_both_same_position"] += later_a and later_b
        counts["test_rows_later_same_orientation_pair"] += later_same
        counts["test_rows_later_reverse_orientation_pair"] += later_reverse
        counts["test_rows_later_either_orientation_pair"] += later_same or later_reverse
    return counts


def _reference(train, test):
    """Direct per-row comparisons; shares only the same parsed inputs."""
    counts = {"player1": {k: 0 for k in ("same_position", "other_position_only", "unseen_anywhere")},
              "player2": {k: 0 for k in ("same_position", "other_position_only", "unseen_anywhere")},
              "test_rows_any_position_unknown": 0, "test_rows_both_positions_unknown": 0,
              "test_rows_same_oriented_pair_seen": 0, "test_rows_reverse_only_pair_seen": 0,
              "test_rows_later_same_position_player1": 0, "test_rows_later_same_position_player2": 0,
              "test_rows_later_any_same_position": 0, "test_rows_later_both_same_position": 0,
              "test_rows_later_same_orientation_pair": 0,
              "test_rows_later_reverse_orientation_pair": 0,
              "test_rows_later_either_orientation_pair": 0}
    for a, b, when in test:
        x_same = any(a == ta for ta, _, _ in train)
        y_same = any(b == tb for _, tb, _ in train)
        x_other = any(a == tb for _, tb, _ in train)
        y_other = any(b == ta for ta, _, _ in train)
        counts["player1"]["same_position" if x_same else
                          ("other_position_only" if x_other else "unseen_anywhere")] += 1
        counts["player2"]["same_position" if y_same else
                          ("other_position_only" if y_other else "unseen_anywhere")] += 1
        counts["test_rows_any_position_unknown"] += not x_same or not y_same
        counts["test_rows_both_positions_unknown"] += not x_same and not y_same
        same = any(a == ta and b == tb for ta, tb, _ in train)
        reverse = any(a == tb and b == ta for ta, tb, _ in train)
        counts["test_rows_same_oriented_pair_seen"] += same
        counts["test_rows_reverse_only_pair_seen"] += reverse and not same
        if when is None:
            continue
        later_a = any(a == ta and t is not None and t > when for ta, _, t in train)
        later_b = any(b == tb and t is not None and t > when for _, tb, t in train)
        later_same = any(a == ta and b == tb and t is not None and t > when for ta, tb, t in train)
        later_reverse = any(a == tb and b == ta and t is not None and t > when for ta, tb, t in train)
        counts["test_rows_later_same_position_player1"] += later_a
        counts["test_rows_later_same_position_player2"] += later_b
        counts["test_rows_later_any_same_position"] += later_a or later_b
        counts["test_rows_later_both_same_position"] += later_a and later_b
        counts["test_rows_later_same_orientation_pair"] += later_same
        counts["test_rows_later_reverse_orientation_pair"] += later_reverse
        counts["test_rows_later_either_orientation_pair"] += later_same or later_reverse
    return counts


def audit(raw, *, expected_sha256, expected_previous, reference_pair_cap=REFERENCE_PAIR_CAP):
    """Reproduce previous aggregate before counting position-local categories."""
    if type(reference_pair_cap) is not int or not 0 <= reference_pair_cap <= REFERENCE_PAIR_CAP:
        raise ValueError("invalid reference pair cap")
    if type(expected_previous) is not dict:
        raise ValueError("previous aggregate must be supplied")
    previous = prior_audit(raw, expected_sha256=expected_sha256)
    if previous != expected_previous:
        raise ValueError("previous aggregate differs from expected inventory")
    digest, rows, clean = _load_notebook_population(raw, expected_sha256=expected_sha256)
    train_idx, test_idx = _notebook_split(clean)

    def materialize(indices):
        included = []
        invalid = excluded_time = 0
        for index in indices:
            row = rows[int(index)]
            if _pair(row) is None:
                invalid += 1
                continue
            when = clean.loc[index, "Date"]
            if getattr(when, "tzinfo", None) is not None:
                when = None
                excluded_time += 1
            included.append((row["Player1"], row["Player2"], when))
        return included, invalid, excluded_time

    train, train_bad, train_zoned = materialize(train_idx)
    test, test_bad, test_zoned = materialize(test_idx)
    counts = _indexed(train, test)
    comparisons = len(train) * len(test)
    if comparisons <= reference_pair_cap:
        if _reference(train, test) != counts:
            raise ValueError("direct position reference differs from indexed counts")
        reference = "matched_direct_pairwise"
    else:
        reference = "skipped_pair_cap"
    return {"schema": "research.tt_ligapro_position_coverage.v1",
            "source_sha256": digest, "source_bytes": len(raw),
            "previous_aggregate_unchanged": True, "notebook_clean_rows": len(clean),
            "split": previous["split"],
            "train_valid_pair_rows": len(train), "test_valid_pair_rows": len(test),
            "train_invalid_pair_rows": train_bad, "test_invalid_pair_rows": test_bad,
            "train_timezone_aware_excluded_from_time_comparison": train_zoned,
            "test_timezone_aware_excluded_from_time_comparison": test_zoned,
            "position_coverage": counts,
            "reference": {"status": reference, "candidate_pairs": comparisons,
                          "same_source_rows_and_parsing": True},
            "source_names_verified_as_people": False, "source_times_verified": False,
            "historical_result_availability_verified": False, "underlying_data_rights_verified": False,
            "actual_notebook_run_verified": False, "independent_holdout": False,
            "model_fit_or_predict": False, "bookmaker_acceptance_verified": False,
            "real_money_execution": False, "profitability_claim": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("previous", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    previous = strict_json_loads(args.previous.read_text(encoding="utf-8"))
    report = audit(args.csv.read_bytes(), expected_sha256=args.expected_sha256, expected_previous=previous)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
