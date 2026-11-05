"""Retrospective source-ID candidates for identical published prediction rows.

No row is merged or removed. A source ID is a publisher claim, not proof of a
physical event. The private transcode must come from the separately pinned
decoder; its hash does not itself prove that the decoder or source is truthful.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import io
import json
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from external_elo_causality_audit import CSV_FIELDS, prediction_inventory

PARQUET_SHA256 = "6d744f1001a48b5109790cd1d0ab896eacd7b1621885afd22d55195a4a970044"
READER_COMMIT = "a32933592707c833780f9cdacc2529c3b68a41d0"
COLUMNS = ("source_match_id", "match_key", "player_a_id", "player_b_id", "winner_id",
           "category", "drop_reason", "is_doubles")
MAX_TRANSCODE_BYTES = 5_000_000


def _number(value):
    if type(value) is int and 0 <= value <= (1 << 53) - 1:
        return str(value)
    if type(value) is str and value and value.isascii() and value.isdigit():
        return value
    return None


def _identity_stats(rows, field):
    values, missing, invalid = [], 0, 0
    for row in rows:
        value = row[field]
        if value is None or value == "":
            missing += 1
            continue
        if field == "source_match_id":
            normalized = _number(value)
        else:
            normalized = value if type(value) is str and value == value.strip() and 0 < len(value) <= 200 else None
        if normalized is None:
            invalid += 1
        else:
            values.append(normalized)
    counts = Counter(values)
    return {"present_rows": len(values), "missing_rows": missing, "invalid_rows": invalid,
            "distinct_values": len(counts), "repeated_values": sum(n > 1 for n in counts.values()),
            "rows_in_repeated_values": sum(n for n in counts.values() if n > 1)}


def audit(csv_raw, transcode_raw, *, expected_csv_sha256, expected_transcode_sha256):
    """Return aggregate-only candidate groups for pinned and verified local bytes."""
    inventory = prediction_inventory(csv_raw, expected_sha256=expected_csv_sha256)
    if type(transcode_raw) is not bytes or not transcode_raw or len(transcode_raw) > MAX_TRANSCODE_BYTES:
        raise ValueError("transcode is empty or oversized")
    transcode_sha = hashlib.sha256(transcode_raw).hexdigest()
    if transcode_sha != expected_transcode_sha256:
        raise ValueError("transcode SHA-256 mismatch")
    try:
        document = strict_json_loads(transcode_raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("invalid transcode JSON") from exc
    if (type(document) is not dict or set(document) != {"schema", "source_parquet_sha256", "reader_commit", "columns", "rows"}
            or document["schema"] != "research.topspin_private_transcode.v1"
            or document["source_parquet_sha256"] != PARQUET_SHA256
            or document["reader_commit"] != READER_COMMIT or document["columns"] != list(COLUMNS)
            or type(document["rows"]) is not list or len(document["rows"]) > 100_000):
        raise ValueError("invalid transcode envelope")

    eligible, excluded = defaultdict(list), Counter()
    for row in document["rows"]:
        if type(row) is not dict or set(row) != set(COLUMNS) or type(row["is_doubles"]) is not bool:
            raise ValueError("invalid decoded source row")
        if row["drop_reason"] is not None or row["is_doubles"]:
            excluded["not_usable_singles"] += 1
            continue
        a, b, winner = (_number(row[k]) for k in ("player_a_id", "player_b_id", "winner_id"))
        if not a or not b or not winner or a == b or winner not in (a, b) or row["category"] not in ("MT", "WT"):
            excluded["usable_rows_with_invalid_join_key"] += 1
            continue
        eligible[(a, b, winner, row["category"])].append(row)

    predictions = csv.DictReader(io.StringIO(csv_raw.decode("utf-8-sig"), newline=""))
    groups = defaultdict(list)
    for row in predictions:
        groups[tuple(row[k] for k in CSV_FIELDS)].append(row)
    duplicate_groups = [group for group in groups.values() if len(group) > 1]
    result_groups, multiplicity = [], Counter()
    for group in sorted(duplicate_groups, key=lambda g: tuple(g[0][k] for k in CSV_FIELDS)):
        row = group[0]
        key = tuple(row[k] for k in ("player_a_id", "player_b_id", "winner_id", "category"))
        candidates = eligible.get(key, [])
        status = "zero" if not candidates else "one" if len(candidates) == 1 else "multiple"
        multiplicity[status] += 1
        result_groups.append({"prediction_rows": len(group), "candidate_source_rows": len(candidates),
                              "candidate_multiplicity": status,
                              "source_match_id": _identity_stats(candidates, "source_match_id"),
                              "match_key": _identity_stats(candidates, "match_key")})
    return {"schema": "research.tt_topspin_duplicate_source_candidates.v1",
            "prediction_csv_sha256": inventory["source_sha256"],
            "source_parquet_sha256": PARQUET_SHA256, "transcode_sha256": transcode_sha,
            "source_rows": len(document["rows"]),
            "source_rows_eligible_for_key_matching": sum(map(len, eligible.values())),
            "source_rows_excluded": {k: excluded[k] for k in ("not_usable_singles", "usable_rows_with_invalid_join_key")},
            "duplicate_prediction_groups": len(result_groups),
            "prediction_groups_by_candidate_multiplicity": {k: multiplicity[k] for k in ("zero", "one", "multiple")},
            "groups": result_groups, "individual_source_ids_published": False,
            "prediction_row_to_source_row_verified": False, "physical_events_verified": False,
            "physical_people_verified": False, "source_truth_verified": False,
            "result_availability_verified": False, "source_rights_verified": False,
            "independent_holdout_verified": False, "real_money_execution": False,
            "note": "Candidate counts only for identical published prediction rows; exact source IDs "
                    "are not physical-event proof, and the CSV lacks a source-match key."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("private_transcode", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-csv-sha256", required=True)
    parser.add_argument("--expected-transcode-sha256", required=True)
    args = parser.parse_args()
    result = audit(args.csv_path.read_bytes(), args.private_transcode.read_bytes(),
                   expected_csv_sha256=args.expected_csv_sha256,
                   expected_transcode_sha256=args.expected_transcode_sha256)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
