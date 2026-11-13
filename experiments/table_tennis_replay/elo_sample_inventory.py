"""Read-only source-local inventory of a publisher's opaque-ID Elo sample.

Counts describe the exact file, not verified individuals, source rights,
chronological availability, fit provenance, prediction quality, or returns.
No source IDs or match rows are emitted; no learning or execution occurs.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re

from wtt_archive_inventory import _day, _present

FIELDS = ("match_id", "date", "league", "player_a", "player_b", "elo_a_pre",
          "elo_b_pre", "sol_a_pre", "sol_b_pre", "p_model", "winner", "sets_a", "sets_b")
MAX_BYTES = 2_000_000
_OPAQUE = re.compile(r"^[0-9a-f]{12}$")


def _number(value, *, probability=False):
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return False
    return number.is_finite() and (not probability or Decimal(0) <= number <= Decimal(1))


def _score_shape(row):
    if row["winner"] not in ("A", "B"):
        return "winner_missing_or_invalid"
    raw = (row["sets_a"], row["sets_b"])
    if not all(isinstance(x, str) and x.isascii() and x.isdigit() for x in raw):
        return "sets_missing_or_invalid"
    a, b = map(int, raw)
    if a == b:
        return "tied_set_totals"
    if (a > b) != (row["winner"] == "A"):
        return "winner_disagrees_with_set_totals"
    if max(a, b) in (3, 4) and min(a, b) < max(a, b):
        return "terminal_shape_format_unverified"
    return "other_shape_format_unverified"


def audit(raw, *, expected_sha256):
    """Inventory one exact CSV without asserting any data's fitness to train."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        raise ValueError("empty or oversized sample")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError("sample SHA-256 mismatch")
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        fields = reader.fieldnames
        if not fields or len(fields) != len(set(fields)) or set(fields) != set(FIELDS):
            raise ValueError("invalid sample CSV header")
        ids, players, leagues, counts, shapes = Counter(), set(), Counter(), Counter(), Counter()
        dates, grouped = [], []
        by_day_player, by_day_pair = Counter(), Counter()
        previous_day = None
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("invalid sample CSV row shape")
            counts["rows"] += 1
            match_id, a, b, league = (row[k] for k in ("match_id", "player_a", "player_b", "league"))
            if _present(match_id):
                ids[match_id] += 1
            else:
                counts["missing_match_id"] += 1
            for name, value in (("match", match_id), ("player_a", a), ("player_b", b)):
                counts[name + "_opaque_hex12"] += int(bool(_OPAQUE.fullmatch(value)))
            counts["both_player_ids_present"] += int(_present(a) and _present(b))
            counts["same_id_both_sides"] += int(_present(a) and a == b)
            players.update(value for value in (a, b) if _present(value))
            if _present(league):
                leagues[league] += 1
            else:
                counts["missing_league"] += 1
            day = _day(row["date"])
            if day is None:
                counts["invalid_date_label"] += 1
            else:
                dates.append(day)
                if previous_day is not None and day < previous_day:
                    counts["date_decreases_in_file_order"] += 1
                previous_day = day
                if _present(a) and _present(b) and a != b:
                    pair = tuple(sorted((a, b)))
                    by_day_player[(day, a)] += 1
                    by_day_player[(day, b)] += 1
                    by_day_pair[(day, pair)] += 1
                    grouped.append((day, a, b, pair))
            for key in ("elo_a_pre", "elo_b_pre", "sol_a_pre", "sol_b_pre", "p_model"):
                counts["invalid_" + key] += int(not _number(
                    row[key], probability=key.startswith("sol_") or key == "p_model"))
            shapes[_score_shape(row)] += 1
        if not counts["rows"]:
            raise ValueError("empty sample CSV")
    except (UnicodeError, csv.Error) as exc:
        raise ValueError("invalid sample CSV encoding or structure") from exc
    repeated_players = {k for k, n in by_day_player.items() if n > 1}
    repeated_pairs = {k for k, n in by_day_pair.items() if n > 1}
    return {
        "schema": "research.tt_elo_sample_inventory.v1",
        "source_sha256": digest, "source_bytes": len(raw), "columns": fields,
        "rows": counts["rows"], "date_labels_valid": len(dates),
        "invalid_date_label": counts["invalid_date_label"],
        "earliest_date_label": min(dates).isoformat() if dates else None,
        "latest_date_label": max(dates).isoformat() if dates else None,
        "date_decreases_in_file_order": counts["date_decreases_in_file_order"],
        "missing_match_id": counts["missing_match_id"],
        "duplicate_match_id_values": sum(n > 1 for n in ids.values()),
        "rows_with_duplicate_match_id": sum(n for n in ids.values() if n > 1),
        "exact_player_id_strings": len(players),
        "both_player_ids_present_rows": counts["both_player_ids_present"],
        "same_id_both_sides_rows": counts["same_id_both_sides"],
        "opaque_hex12_rows": {k: counts[k + "_opaque_hex12"] for k in ("match", "player_a", "player_b")},
        "league_label_counts": dict(sorted(leagues.items())), "missing_league": counts["missing_league"],
        "date_player_groups_with_multiple_rows": len(repeated_players),
        "rows_with_repeated_player_on_date": sum(
            (day, a) in repeated_players or (day, b) in repeated_players for day, a, b, _ in grouped),
        "date_pair_groups_with_multiple_rows": len(repeated_pairs),
        "rows_with_repeated_pair_on_date": sum((day, pair) in repeated_pairs for day, _, _, pair in grouped),
        "score_shape_counts": dict(sorted(shapes.items())),
        "invalid_numeric_counts": {k: counts["invalid_" + k] for k in
                                   ("elo_a_pre", "elo_b_pre", "sol_a_pre", "sol_b_pre", "p_model")},
        "source_ids_verified_as_people": False, "cross_provider_identity_verified": False,
        "upstream_rights_verified": False, "exact_match_time_verified": False,
        "pre_match_or_outcome_availability_verified": False, "source_fit_provenance_verified": False,
        "independent_holdout": False, "model_training_authorized": False,
        "bookmaker_prices_accepted": False, "real_money_execution": False,
        "note": "Counts use exact source strings and date labels, not verified people or event ordering.",
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
