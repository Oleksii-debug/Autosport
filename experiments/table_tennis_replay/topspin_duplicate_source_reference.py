"""Independent quadratic check, restricted to two exact pinned input files.

Outputs group counts only. This does not validate the Parquet decoder, source
truth, a prediction-to-match link, or distinct physical events.
"""
from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

CSV_SHA = "9bcaa521b50cc573195545094c54601fa8f071b017ea8ed2e8498f076f80b983"
TRANSCODE_SHA = "d0b4080bf79ab6e225e53846470fd9e5d2d9521ce81953959a10029ebb74bd61"


def verify(raw, expected):
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("input hash mismatch")


def count(csv_raw, transcode_raw):
    verify(csv_raw, CSV_SHA)
    verify(transcode_raw, TRANSCODE_SHA)
    csv_rows = list(csv.reader(io.StringIO(csv_raw.decode("utf-8-sig"), newline="")))
    header, rows = csv_rows[0], csv_rows[1:]
    fields = ("player_a_id", "player_b_id", "winner_id", "category")
    offsets = [header.index(name) for name in fields]
    eligible = [row for row in json.loads(transcode_raw)["rows"]
                if row["drop_reason"] is None and row["is_doubles"] is False]
    groups = []
    for values, count in sorted(Counter(tuple(row) for row in rows).items()):
        if count <= 1:
            continue
        candidates = [row for row in eligible
                      if tuple(str(row[name]) for name in fields) == tuple(values[i] for i in offsets)]
        groups.append({"prediction_rows": count, "candidate_source_rows": len(candidates),
                       "distinct_source_ids": len({row["source_match_id"] for row in candidates if row["source_match_id"] is not None}),
                       "distinct_match_keys": len({row["match_key"] for row in candidates if row["match_key"] is not None})})
    return {"source_rows": len(json.loads(transcode_raw)["rows"]), "eligible_rows": len(eligible),
            "duplicate_prediction_groups": len(groups), "groups": groups}


if __name__ == "__main__":
    print(json.dumps(count(Path(sys.argv[1]).read_bytes(), Path(sys.argv[2]).read_bytes()), sort_keys=True))
