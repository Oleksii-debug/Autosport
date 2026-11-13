"""Count source-local date-label support; never admit history to a learner.

Earlier date labels are an optimistic structural filter, not proof a result or
identity was available before a decision. The file-order comparison is a
counterfactual warning, not an assertion of the physical match order.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import io
import json
from pathlib import Path

from elo_sample_inventory import audit as inventory
from wtt_archive_inventory import _day, _present


def audit(raw, *, expected_sha256):
    """Aggregate exact ID/league coincidences in a date-only research sample."""
    summary = inventory(raw, expected_sha256=expected_sha256)
    if (summary["invalid_date_label"] or summary["missing_match_id"] or
            summary["duplicate_match_id_values"] or summary["missing_league"] or
            summary["both_player_ids_present_rows"] != summary["rows"] or
            summary["same_id_both_sides_rows"]):
        raise ValueError("ambiguous source dates, IDs, or league")
    days = defaultdict(list)
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    for row in reader:
        match_id, league, a, b = (row[k] for k in ("match_id", "league", "player_a", "player_b"))
        if any(not _present(x) or x != x.strip() for x in (match_id, league, a, b)):
            raise ValueError("noncanonical source strings")
        days[_day(row["date"])].append((league, a, b))

    prior_players, prior_pairs = Counter(), Counter()
    strict, file_order, pair_distribution = Counter(), Counter(), Counter()
    for day in sorted(days):
        today_players, today_pairs = Counter(), Counter()
        for league, a, b in days[day]:
            ak, bk, pair = (league, a), (league, b), (league, *sorted((a, b)))
            pa, pb, ph = prior_players[ak], prior_players[bk], prior_pairs[pair]
            na, nb, nh = pa + today_players[ak], pb + today_players[bk], ph + today_pairs[pair]
            strict["any_player"] += int(bool(pa or pb))
            strict["both_players"] += int(bool(pa and pb))
            strict["h2h_pair"] += int(bool(ph))
            pair_distribution["0" if ph == 0 else "1" if ph == 1 else "2-4" if ph <= 4 else "5+"] += 1
            file_order["player_support_includes_same_day"] += int(bool(today_players[ak] or today_players[bk]))
            file_order["any_player_only_from_same_day"] += int(not (pa or pb) and bool(na or nb))
            file_order["both_players_only_from_same_day"] += int(not (pa and pb) and bool(na and nb))
            file_order["h2h_support_includes_same_day"] += int(bool(today_pairs[pair]))
            file_order["h2h_only_from_same_day"] += int(not ph and bool(nh))
            today_players[ak] += 1
            today_players[bk] += 1
            today_pairs[pair] += 1
        # Apply all rows only after every row in the date batch was inspected.
        prior_players.update(today_players)
        prior_pairs.update(today_pairs)

    strict_keys = ("any_player", "both_players", "h2h_pair")
    unsafe_keys = ("player_support_includes_same_day", "any_player_only_from_same_day",
                   "both_players_only_from_same_day", "h2h_support_includes_same_day",
                   "h2h_only_from_same_day")
    return {
        "schema": "research.tt_elo_day_support_audit.v1",
        "source_sha256": summary["source_sha256"], "source_rows": summary["rows"],
        "date_labels": len(days), "league_comparison": "exact source label",
        "strict_earlier_date_source_row_support": {k: strict[k] for k in strict_keys},
        "strict_earlier_date_h2h_count_distribution": {
            k: pair_distribution[k] for k in ("0", "1", "2-4", "5+")},
        "same_day_prefix_in_file_order": {k: file_order[k] for k in unsafe_keys},
        "physical_people_verified": False, "physical_match_deduplicated": False,
        "event_date_verified": False, "result_available_before_decision_verified": False,
        "source_rights_verified": False, "independent_holdout": False,
        "model_training_authorized": False, "bookmaker_execution": False,
        "profitability_claim": False,
        "note": "Source-row coincidences under optimistic assumptions, not causal features. "
                "File-order comparison does not establish within-day chronology.",
    }


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
