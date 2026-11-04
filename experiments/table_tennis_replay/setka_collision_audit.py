"""Audit score consistency in previously detected exact-label time collisions.

This is an exploratory source-quality check, not event deduplication, identity
resolution, outcome verification, model validation or permission to bet.
Only aggregate counts are returned; player labels and scores stay in memory.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import io
import json
from pathlib import Path

from setka_source_time_audit import FIELDS, _read, _time_label, temporal_counts
from wtt_archive_inventory import _present, _score_shape


def _int(value):
    if (not isinstance(value, str) or not value.isascii() or
            not value.isdigit() or len(value) > 3):
        return None
    return int(value)


def _signature(row):
    """Return oriented score content only for structurally valid source rows."""
    a, b = row["Player1"], row["Player2"]
    if a == b or row["HomeWinner"] not in ("0", "1"):
        return None
    totals = (_int(row["Sets_P1"]), _int(row["Sets_P2"]))
    if None in totals:
        return None
    games, saw_missing = [], False
    for n in range(1, 6):
        left, right = row[f"P1_G{n}"], row[f"P2_G{n}"]
        lp, rp = _present(left), _present(right)
        if not lp and not rp:
            saw_missing = True
            continue
        if not lp or not rp or saw_missing:
            return None
        x, y = _int(left), _int(right)
        if x is None or y is None:
            return None
        games.append((x, y))
    text = ",".join(f"{x}-{y}" for x, y in games)
    winner = "1" if row["HomeWinner"] == "1" else "2"
    if _score_shape(text, winner, "5") != "terminal_and_winner_consistent":
        return None
    if totals != (sum(x > y for x, y in games), sum(y > x for x, y in games)):
        return None
    # Compare relative to the lexicographically first source label. This is
    # orientation normalization inside one exact-label group, not identity.
    if a < b:
        return (winner, totals, tuple(games))
    return ("2" if winner == "1" else "1", totals[::-1], tuple((y, x) for x, y in games))


def collision_groups(raw, *, expected_sha256):
    """Select exactly the earlier temporal audit's collision population."""
    info, records = _read(raw, expected_sha256=expected_sha256)
    if info["missing_or_noncanonical_source_row_label"] or info["duplicate_source_row_label_values"]:
        raise ValueError("source row labels do not uniquely bind eligible input")
    remaining = Counter(records)
    groups = defaultdict(list)
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    for row in reader:
        a, b = row["Player1"], row["Player2"]
        key = (row["Date"], _time_label(row["Time"]), a, b)
        if remaining[key]:
            remaining[key] -= 1
            groups[(key[0], key[1], *sorted((a, b)))].append(row)
    if sum(remaining.values()):
        raise ValueError("eligible source rows could not be bound")
    collisions = [group for group in groups.values() if len(group) > 1]
    if sum(map(len, collisions)) != temporal_counts(records)["same_time_same_pair_rows"]:
        raise ValueError("collision population differs from prior timing audit")
    return info, collisions


def audit(raw, *, expected_sha256):
    info, groups = collision_groups(raw, expected_sha256=expected_sha256)
    status, sizes, qualified_rows = Counter(), Counter(), 0
    reversed_groups = raw_equal_groups = 0
    for group in groups:
        sizes[str(len(group))] += 1
        reversed_groups += int(len({(r["Player1"], r["Player2"]) for r in group}) > 1)
        raw_equal_groups += int(len({tuple(r[k] for k in FIELDS if k != "X") for r in group}) == 1)
        signatures = [_signature(row) for row in group]
        qualified_rows += sum(s is not None for s in signatures)
        if any(s is None for s in signatures):
            status["not_fully_qualified"] += 1
        elif len(set(signatures)) == 1:
            status["all_qualified_equal"] += 1
        else:
            status["all_qualified_different"] += 1
    return {"schema": "research.tt_setka_collision_audit.v1",
            "source_sha256": info["source_sha256"], "source_rows": info["rows"],
            "eligible_temporal_rows": info["eligible_temporal_rows"],
            "collision_groups": len(groups), "collision_rows": sum(map(len, groups)),
            "collision_group_size_counts": dict(sorted(sizes.items())),
            "groups_with_reversed_source_side_order": reversed_groups,
            "posthoc_groups_equal_on_raw_fields_except_row_label": raw_equal_groups,
            "structurally_qualified_collision_rows": qualified_rows,
            "group_score_status": {k: status[k] for k in
                                   ("all_qualified_equal", "all_qualified_different", "not_fully_qualified")},
            "format_independently_verified": False, "identity_independently_verified": False,
            "physical_match_deduplication_verified": False, "source_outcomes_independently_verified": False,
            "result_availability_verified": False, "rights_verified": False,
            "independent_holdout": False, "model_training_authorized": False,
            "bookmaker_execution": False, "profitability_claim": False,
            "note": "Exact source-label and structurally comparable score coincidences only; "
                    "no source data or physical event was altered."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    result = audit(args.sample.read_bytes(), expected_sha256=args.expected_sha256)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
