"""Read-only WTT source inventory; no forecasts, learning or identity merges.

The publisher's ID and event-date columns are source claims. Their existence
does not establish person identity, exact match time, data rights, historical
availability, executable prices or a valid forward holdout.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

FILES = ("table_tennis_wtt_men_std.csv", "table_tennis_wtt_women_std.csv")
REQUIRED = {"sport", "gender", "dataset_id", "match_id", "event_date", "player1_name",
            "player2_name", "winner", "score"}
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_MEMBER = 2 * 1024 * 1024


def _present(value):
    return isinstance(value, str) and value.strip().lower() not in ("", "nan", "na", "null", "none")


def _native_id(value):
    return _present(value) and value == value.strip() and value.isascii() and value.isdigit()


def _day(value):
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _score_shape(score, winner, best_of):
    """Classify source score structure, without deriving model outcomes."""
    if best_of not in ("1", "3", "5", "7"):
        return "best_of_missing_or_unsupported"
    if winner not in ("1", "2") or not _present(score):
        return "winner_or_score_missing"
    threshold = (int(best_of) + 1) // 2
    wins = [0, 0]
    for part in score.split(","):
        match = re.fullmatch(r"(\d+)-(\d+)", part.strip())
        if match is None:
            return "invalid_set_score"
        a, b = map(int, match.groups())
        hi, lo = max(a, b), min(a, b)
        if not ((hi == 11 and lo <= 9) or (hi > 11 and hi - lo == 2)):
            return "invalid_set_score"
        if threshold in wins:
            return "sets_after_declared_terminal"
        wins[int(b > a)] += 1
    if threshold not in wins:
        return "nonterminal_for_declared_best_of"
    if wins.index(threshold) != int(winner) - 1:
        return "winner_conflicts_with_set_scores"
    return "terminal_and_winner_consistent"


def _table(raw, name):
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        fields = reader.fieldnames
        if not fields or len(fields) != len(set(fields)) or not REQUIRED <= set(fields):
            raise ValueError("invalid WTT CSV header")
        both_ids = {"a_id_raw", "x_id_raw"} <= set(fields)
        counts = Counter(); dates = []; match_ids = Counter(); player_days = Counter(); grouped = []
        native_players = set(); id_names = {}; name_ids = {}; event_rounds = {}; best_of_values = Counter()
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("invalid WTT CSV row shape")
            counts["rows"] += 1
            for label in ("sport", "gender", "dataset_id"):
                counts[label + "_present"] += int(_present(row[label]))
            for label in ("player1_rank", "player2_rank", "player1_rank_points", "player2_rank_points"):
                counts[label + "_present"] += int(_present(row.get(label)))
            best_of_values[row.get("best_of") or "<missing>"] += 1
            day = _day(row["event_date"])
            if day is None:
                counts["invalid_or_missing_event_date"] += 1
            else:
                counts["valid_date_only"] += 1; dates.append(day)
            match = row["match_id"]
            if _present(match): match_ids[match] += 1
            else: counts["missing_match_id"] += 1
            counts["match_id_ascii_digits"] += int(_native_id(match))
            counts["both_names_present"] += int(_present(row["player1_name"]) and _present(row["player2_name"]))
            counts["winner_code_1_or_2"] += int(row["winner"] in ("1", "2"))
            counts["score_present"] += int(_present(row["score"]))
            counts["score_shape:" + _score_shape(row["score"], row["winner"], row.get("best_of"))] += 1
            if day is not None and _present(row.get("event_name")) and _present(row.get("round")):
                event_rounds.setdefault((day, row["event_name"]), set()).add(row["round"])
            if both_ids:
                left, right = row["a_id_raw"], row["x_id_raw"]
                for native_id, name_value in ((left, row["player1_name"]), (right, row["player2_name"])):
                    if _native_id(native_id) and _present(name_value):
                        id_names.setdefault(native_id, set()).add(name_value)
                        name_ids.setdefault(name_value, set()).add(native_id)
                if _present(left) and _present(right): counts["both_raw_ids_present"] += 1
                if _native_id(left) and _native_id(right):
                    counts["both_raw_ids_ascii_digits"] += 1
                    if left == right:
                        counts["same_raw_id_both_sides"] += 1
                    elif day is not None:
                        native_players.update((left, right)); player_days[(day, left)] += 1
                        player_days[(day, right)] += 1; grouped.append((day, left, right))
        if not counts["rows"]:
            raise ValueError("empty WTT CSV")
        repeated = {key for key, count in player_days.items() if count > 1}
        summary = {"file": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                "columns": fields, "rows": counts["rows"], "valid_date_only": counts["valid_date_only"],
                "sport_label_present": counts["sport_present"], "gender_label_present": counts["gender_present"],
                "dataset_id_label_present": counts["dataset_id_present"],
                "rank_fields_present": {label: counts[label + "_present"] for label in
                                        ("player1_rank", "player2_rank", "player1_rank_points", "player2_rank_points")},
                "best_of_values": dict(sorted(best_of_values.items())),
                "invalid_or_missing_event_date": counts["invalid_or_missing_event_date"],
                "earliest_event_date_label": min(dates).isoformat() if dates else None,
                "latest_event_date_label": max(dates).isoformat() if dates else None,
                "missing_match_id": counts["missing_match_id"],
                "match_id_ascii_digits": counts["match_id_ascii_digits"],
                "duplicated_match_id_values": sum(n > 1 for n in match_ids.values()),
                "rows_with_duplicated_match_id": sum(n for n in match_ids.values() if n > 1),
                "both_names_present": counts["both_names_present"],
                "winner_code_1_or_2": counts["winner_code_1_or_2"], "score_present": counts["score_present"],
                "score_shape_counts": {k.removeprefix("score_shape:"): v for k, v in sorted(counts.items())
                                       if k.startswith("score_shape:")},
                "native_id_fields_present": both_ids,
                "both_raw_ids_present": counts["both_raw_ids_present"] if both_ids else None,
                "both_raw_ids_ascii_digits": counts["both_raw_ids_ascii_digits"] if both_ids else None,
                "same_raw_id_both_sides": counts["same_raw_id_both_sides"] if both_ids else None,
                "distinct_native_ids_in_groupable_rows": len(native_players) if both_ids else None,
                "raw_ids_with_multiple_exact_name_strings": sum(len(v) > 1 for v in id_names.values()) if both_ids else None,
                "exact_name_strings_with_multiple_raw_ids": sum(len(v) > 1 for v in name_ids.values()) if both_ids else None,
                "event_date_name_groups_with_multiple_round_labels": sum(len(v) > 1 for v in event_rounds.values()),
                "repeated_native_id_event_date_groups": len(repeated) if both_ids else None,
                "rows_with_repeated_id_on_event_date": (sum((day, a) in repeated or (day, b) in repeated
                                                             for day, a, b in grouped) if both_ids else None)}
        return summary, frozenset(match_ids)
    except (UnicodeError, csv.Error) as exc:
        raise ValueError("invalid WTT CSV encoding or structure") from exc


def audit(raw, *, expected_sha256):
    """Inspect exact named members; ZIP member CRCs are checked as they are read."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_ARCHIVE:
        raise ValueError("empty or oversized WTT archive")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError("source archive SHA-256 mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            info = archive.infolist(); names = [x.filename for x in info]
            if len(names) != len(set(names)) or any(names.count(name) != 1 for name in FILES):
                raise ValueError("missing or duplicate WTT archive member")
            tables = []; id_sets = []
            for name in FILES:
                entry = archive.getinfo(name)
                if entry.is_dir() or entry.file_size > MAX_MEMBER or entry.flag_bits & 1:
                    raise ValueError("unsafe or oversized WTT member")
                content = archive.read(entry)
                if len(content) != entry.file_size:
                    raise ValueError("WTT member length mismatch")
                table, ids = _table(content, name)
                tables.append(table); id_sets.append(ids)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("corrupt WTT archive or member") from exc
    return {"schema": "research.wtt_archive_inventory.v1", "archive_sha256": digest,
            "archive_bytes": len(raw), "archive_members": len(names), "tables": tables,
            "cross_file_exact_match_id_overlap": len(id_sets[0] & id_sets[1]),
            "other_members_decompressed": False, "source_names_verified": False,
            "cross_provider_identity_verified": False, "historical_availability_verified": False,
            "training_authorized": False, "real_money_execution": False,
            "note": "Event dates and raw IDs are publisher labels, not proven causal times or identities."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    report = audit(args.archive.read_bytes(), expected_sha256=args.expected_sha256)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2); stream.write("\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
