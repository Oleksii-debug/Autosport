"""Inspect source identity fields and name overlap; never resolve people by name."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from itertools import combinations
import json
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from prospective_capture import verify_session
from quote_delay_audit import _snapshot


# Presence only: even a populated field must not be called verified identity.
ID_KEYS = ("home_id", "away_id", "home_player_id", "away_player_id",
           "home_participant_id", "away_participant_id", "player_id", "participant_id")


def _observed_labels(directory):
    verified = verify_session(directory)
    fixture_ids, names = set(), set()
    snapshots = 0
    for (seq, kind), receipt in sorted(verified.items()):
        if kind != "odds":
            continue
        raw = (Path(directory) / f"{seq:04d}.odds.body.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
            raise ValueError("snapshot changed after verification")
        board = strict_json_loads(raw.decode("utf-8"))
        _snapshot(directory, seq, receipt)  # validate fixture IDs and uniqueness
        for row in board["matches"]:
            fixture_ids.add(row["id"])
            for key in ("home", "away"):
                if isinstance(row.get(key), str) and row[key]:
                    names.add(row[key])
        snapshots += 1
    return fixture_ids, names, snapshots


def compare_sessions(earlier, later):
    """Exact labels only; neither identity resolution nor independence proof."""
    a_ids, a_names, a_n = _observed_labels(earlier)
    b_ids, b_names, b_n = _observed_labels(later)
    return {"earlier_odds_snapshots": a_n, "later_odds_snapshots": b_n,
            "earlier_fixture_ids": len(a_ids), "later_fixture_ids": len(b_ids),
            "common_fixture_ids": len(a_ids & b_ids),
            "earlier_exact_name_strings": len(a_names), "later_exact_name_strings": len(b_names),
            "common_exact_name_strings": len(a_names & b_names),
            "participant_identity_verified": False, "independence_verified": False}


def audit(directory):
    verified = verify_session(directory)
    reports = []
    for seq, kind in sorted(verified):
        if kind != "odds":
            continue
        receipt = verified[(seq, kind)]
        raw = (Path(directory) / f"{seq:04d}.odds.body.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
            raise ValueError("snapshot changed after verification")
        board = strict_json_loads(raw.decode("utf-8"))
        rows = board["matches"]
        root_keys = Counter(key for row in rows for key in row)
        fields = {key: sum(row.get(key) not in (None, "") for row in rows) for key in ID_KEYS}
        states = _snapshot(directory, seq, receipt)
        eligible = {key: state for key, state in states.items() if state is not None}
        pair_count = overlap_count = 0
        for a, b in combinations(sorted(eligible), 2):
            pair_count += 1
            overlap_count += bool(set(eligible[a][:2]) & set(eligible[b][:2]))
        reports.append({"sequence": seq, "source_fixture_rows": len(rows),
                        "root_field_occurrences": dict(sorted(root_keys.items())),
                        "candidate_player_id_fields_populated": fields,
                        "eligible_fixtures": len(eligible), "eligible_pairs": pair_count,
                        "pairs_sharing_exact_source_name": overlap_count})
    return {"schema": "research.tt_identity_coverage.v1", "snapshots": reports,
            "source_names_resolve_people": False, "fixture_or_selection_ids_are_player_ids": False,
            "identity_verified": False, "rights_verified": False,
            "note": "An exact name overlap is a warning only; absence of overlap does not prove different people."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--compare", type=Path, help="earlier verified capture directory")
    args = parser.parse_args()
    report = audit(args.session)
    if args.compare is not None:
        report["earlier_session_comparison"] = compare_sessions(args.compare, args.session)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
