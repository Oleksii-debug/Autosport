"""Independent score-shape calculation over the audit's selected source rows.

The row selection and input parser are shared with the primary audit; this
independently checks score qualification and comparison, not source truth.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from setka_collision_audit import collision_groups
from setka_source_time_audit import FIELDS


def _parse_number(value):
    if not isinstance(value, str) or len(value) not in (1, 2, 3):
        return None
    if any(c not in "0123456789" for c in value):
        return None
    return int(value)


def _missing(value):
    return not isinstance(value, str) or value.strip().lower() in ("", "na", "nan", "null", "none")


def _reference_signature(row):
    if row["Player1"] == row["Player2"] or row["HomeWinner"] not in ("0", "1"):
        return None
    declared = [_parse_number(row[k]) for k in ("Sets_P1", "Sets_P2")]
    if any(value is None for value in declared):
        return None
    scores, wins, missing_seen = [], [0, 0], False
    for index in range(1, 6):
        left, right = row[f"P1_G{index}"], row[f"P2_G{index}"]
        lm, rm = _missing(left), _missing(right)
        if lm and rm:
            missing_seen = True
            continue
        if lm or rm or missing_seen or 3 in wins:
            return None
        a, b = _parse_number(left), _parse_number(right)
        if a is None or b is None:
            return None
        high, low = max(a, b), min(a, b)
        if not ((high == 11 and low <= 9) or (high > 11 and high - low == 2)):
            return None
        wins[0 if a > b else 1] += 1
        scores.append((a, b))
    winner = 0 if row["HomeWinner"] == "1" else 1
    if 3 not in wins or wins.index(3) != winner or declared != wins:
        return None
    if row["Player1"] < row["Player2"]:
        return (winner, tuple(wins), tuple(scores))
    return (1 - winner, tuple(reversed(wins)), tuple((b, a) for a, b in scores))


def summarize(groups):
    categories, sizes = Counter(), Counter()
    qualified, reversed_groups, raw_equal_groups = 0, 0, 0
    for rows in groups:
        sizes[str(len(rows))] += 1
        reversed_groups += int(len({(row["Player1"], row["Player2"]) for row in rows}) > 1)
        raw_equal_groups += int(all(all(row[k] == rows[0][k] for k in FIELDS if k != "X")
                                    for row in rows[1:]))
        signatures = [_reference_signature(row) for row in rows]
        qualified += sum(sig is not None for sig in signatures)
        if any(sig is None for sig in signatures):
            categories["not_fully_qualified"] += 1
        elif all(sig == signatures[0] for sig in signatures):
            categories["all_qualified_equal"] += 1
        else:
            categories["all_qualified_different"] += 1
    return {"collision_groups": len(groups), "collision_rows": sum(map(len, groups)),
            "collision_group_size_counts": dict(sorted(sizes.items())),
            "groups_with_reversed_source_side_order": reversed_groups,
            "posthoc_groups_equal_on_raw_fields_except_row_label": raw_equal_groups,
            "structurally_qualified_collision_rows": qualified,
            "group_score_status": {k: categories[k] for k in
                                   ("all_qualified_equal", "all_qualified_different", "not_fully_qualified")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    info, groups = collision_groups(args.sample.read_bytes(), expected_sha256=args.expected_sha256)
    print(json.dumps({"source_sha256": info["source_sha256"], **summarize(groups),
                      "independent_input_parser_or_selection": False}, sort_keys=True))


if __name__ == "__main__":
    main()
