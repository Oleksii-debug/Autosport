"""Compare manually transcribed source claims; never merge people or matches.

An injective, user-supplied crosswalk is an *unverified hypothesis*. Calendar
and clock labels have no established time zone or availability semantics. The
output is aggregate-only, retrospective, and cannot authorize training, a
portfolio action, or a physical-identity/event claim.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re

from autosport.json_integrity import strict_json_loads
from wtt_archive_inventory import _day, _score_shape

MAX_BYTES = 1_000_000
MAX_ROWS = 5_000
ROW_FIELDS = frozenset({"row_id", "date", "clock", "a", "b", "score", "winner"})
_SCORE = re.compile(r"[0-9]{1,3}-[0-9]{1,3}(?:,[0-9]{1,3}-[0-9]{1,3}){2,4}\Z")
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")


def _text(value):
    return (type(value) is str and 0 < len(value) <= 200 and value == value.strip()
            and "\x00" not in value)


def _rows(rows, *, translate, possible_left):
    if type(rows) is not list or len(rows) > MAX_ROWS:
        raise ValueError("source rows must be a bounded list")
    signatures, row_ids = defaultdict(list), set()
    unmapped = 0
    for row in rows:
        if type(row) is not dict or frozenset(row) != ROW_FIELDS:
            raise ValueError("source row fields are invalid")
        if not all(_text(row[k]) for k in ("row_id", "a", "b")) or row["a"] == row["b"]:
            raise ValueError("row or participant labels are invalid")
        if row["row_id"] in row_ids:
            raise ValueError("duplicate local row reference")
        row_ids.add(row["row_id"])
        day = _day(row["date"])
        if day is None:
            raise ValueError("date must be a canonical date label")
        clock = row["clock"]
        if clock is not None and (type(clock) is not str or not _CLOCK.fullmatch(clock)):
            raise ValueError("clock must be a canonical HH:MM label or null")
        score, winner = row["score"], row["winner"]
        if (type(score) is not str or _SCORE.fullmatch(score) is None or
                _score_shape(score, winner, "5") != "terminal_and_winner_consistent"):
            raise ValueError("score is incompatible with assumed best-of-five")
        if translate is None:
            a, b = row["a"], row["b"]
            if a not in possible_left or b not in possible_left:
                unmapped += 1
                continue
        else:
            a, b = translate.get(row["a"]), translate.get(row["b"])
            if a is None or b is None:
                unmapped += 1
                continue
        # Crosswalk keys are explicit; no name similarity or automatic identity.
        swapped = a > b
        sets = tuple(tuple(map(int, part.split("-"))) for part in score.split(","))
        normalized = tuple((y, x) if swapped else (x, y) for x, y in sets)
        signatures[(day.isoformat(), *sorted((a, b)), normalized)].append((clock, swapped))
    return signatures, unmapped


def audit(raw, *, expected_sha256):
    if type(raw) is not bytes or not raw or len(raw) > MAX_BYTES:
        raise ValueError("input is empty or oversized")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise ValueError("input SHA-256 mismatch")
    try:
        document = strict_json_loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid input JSON") from exc
    if type(document) is not dict or set(document) != {"left", "right", "right_to_left"}:
        raise ValueError("input fields are invalid")
    mapping = document["right_to_left"]
    if (type(mapping) is not dict or len(mapping) > MAX_ROWS or
            any(not _text(key) or not _text(value) for key, value in mapping.items()) or
            len(set(mapping.values())) != len(mapping)):
        raise ValueError("candidate crosswalk must be an injective map of exact labels")
    left, lu = _rows(document["left"], translate=None, possible_left=set(mapping.values()))
    right, ru = _rows(document["right"], translate=mapping, possible_left=set(mapping.values()))
    counts = Counter()
    for key in left.keys() | right.keys():
        ls, rs = left.get(key, ()), right.get(key, ())
        if not ls:
            counts["unmatched_right_rows"] += len(rs)
        elif not rs:
            counts["unmatched_left_rows"] += len(ls)
        elif len(ls) != 1 or len(rs) != 1:
            counts["ambiguous_signature_groups"] += 1
            counts["left_rows_in_ambiguous_groups"] += len(ls)
            counts["right_rows_in_ambiguous_groups"] += len(rs)
        else:
            counts["unique_signature_candidates"] += 1
            lc, lr = ls[0]
            rc, rr = rs[0]
            counts["opposite_side_order_candidates"] += int(lr != rr)
            if lc is None or rc is None:
                counts["clock_label_missing_candidates"] += 1
            elif lc == rc:
                counts["clock_label_equal_candidates"] += 1
            else:
                counts["clock_label_different_candidates"] += 1
    fields = ("unique_signature_candidates", "ambiguous_signature_groups",
              "left_rows_in_ambiguous_groups", "right_rows_in_ambiguous_groups",
              "unmatched_left_rows", "unmatched_right_rows", "opposite_side_order_candidates",
              "clock_label_equal_candidates", "clock_label_different_candidates",
              "clock_label_missing_candidates")
    return {"schema": "research.tt_cross_source_score_candidates.v1",
            "transcription_sha256": actual, "source_rows": {"left": len(document["left"]),
                                                               "right": len(document["right"])},
            "unmapped_rows": {"left": lu, "right": ru},
            "counts": {key: counts[key] for key in fields},
            "best_of_five_assumed": True, "transcription_independently_verified": False,
            "crosswalk_verified": False, "physical_identity_verified": False,
            "physical_match_verified": False, "source_independence_verified": False,
            "timezone_verified": False, "result_availability_verified": False,
            "rights_verified": False, "training_authorized": False,
            "bookmaker_execution": False, "profitability_claim": False,
            "note": "Exact source-claim signatures under a proposed mapping only; "
                    "clock labels are compared literally, not as instants. "
                    "No candidate is a confirmed identity or unique physical match."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    report = audit(args.input.read_bytes(), expected_sha256=args.expected_sha256)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
