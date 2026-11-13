"""Compare manually transcribed date/clock labels; never merge matches or people.

A caller-proposed injective participant mapping, same-day labels, a bounded
constant clock offset and a tolerance are hypotheses, not verified identities,
timezones or event timing. Only aggregate candidate counts are published.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from itertools import permutations, product
import hashlib
import json
from math import factorial, prod
from pathlib import Path
import re

from autosport.json_integrity import strict_json_loads
from cross_source_score_candidates import _CLOCK, _day, _text

MAX_BYTES = 200_000
MAX_ROWS = 12
MAX_PAIRINGS = 10_000
OFFSET_LIMIT = 14 * 60
TOLERANCE = 1
_ELAPSED = re.compile(r"[0-9]{2}:[0-5][0-9]:[0-5][0-9]\Z")
_LEFT = frozenset({"row_id", "date", "clock", "a", "b"})
_RIGHT = _LEFT | {"elapsed"}


def _minutes(value):
    if type(value) is not str or _CLOCK.fullmatch(value) is None:
        raise ValueError("clock must be HH:MM without timezone")
    h, m = map(int, value.split(":"))
    return 60 * h + m


def _seconds(value):
    if type(value) is not str or _ELAPSED.fullmatch(value) is None:
        raise ValueError("chapter position must be HH:MM:SS")
    h, m, s = map(int, value.split(":"))
    return 3600 * h + 60 * m + s


def _parse(raw, expected_sha256):
    if type(raw) is not bytes or not raw or len(raw) > MAX_BYTES:
        raise ValueError("input is empty or oversized")
    digest = hashlib.sha256(raw).hexdigest()
    if type(expected_sha256) is not str or expected_sha256 != digest:
        raise ValueError("input SHA-256 mismatch")
    try:
        document = strict_json_loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid input JSON") from exc
    if type(document) is not dict or set(document) != {"left", "right", "right_to_left"}:
        raise ValueError("input fields are invalid")
    mapping = document["right_to_left"]
    if (type(mapping) is not dict or len(mapping) > MAX_ROWS * 2 or
            any(not _text(k) or not _text(v) for k, v in mapping.items()) or
            len(set(mapping.values())) != len(mapping)):
        raise ValueError("candidate crosswalk must be injective")
    output = []
    for name, fields in (("left", _LEFT), ("right", _RIGHT)):
        source = document[name]
        if type(source) is not list or len(source) > MAX_ROWS:
            raise ValueError("source rows must be a bounded list")
        rows, row_ids = [], set()
        for row in source:
            if type(row) is not dict or frozenset(row) != fields:
                raise ValueError("source row fields are invalid")
            if not all(_text(row[k]) for k in ("row_id", "a", "b")) or row["a"] == row["b"]:
                raise ValueError("row or participant labels are invalid")
            if row["row_id"] in row_ids:
                raise ValueError("duplicate local row reference")
            row_ids.add(row["row_id"])
            day = _day(row["date"])
            if day is None:
                raise ValueError("date must be a canonical date label")
            if name == "left":
                a, b = row["a"], row["b"]
                if a not in mapping.values() or b not in mapping.values():
                    raise ValueError("unmapped participant label")
            else:
                a, b = mapping.get(row["a"]), mapping.get(row["b"])
                if a is None or b is None:
                    raise ValueError("unmapped participant label")
            rows.append({"id": row["row_id"], "day": day.isoformat(),
                         "a": a, "b": b, "clock": _minutes(row["clock"]),
                         "elapsed": _seconds(row["elapsed"]) if name == "right" else None})
        output.append(rows)
    return digest, output[0], output[1]


def _group(row):
    return (row["day"], *sorted((row["a"], row["b"])))


def audit(raw, *, expected_sha256):
    digest, left, right = _parse(raw, expected_sha256)
    lgroup, rgroup = defaultdict(list), defaultdict(list)
    for row in left:
        lgroup[_group(row)].append(row)
    for row in right:
        rgroup[_group(row)].append(row)
    for rows in rgroup.values():
        positions = [row["elapsed"] for row in rows]
        if len(set(positions)) != len(positions):
            raise ValueError("duplicate chapter position in date-pair group")
    common = lgroup.keys() | rgroup.keys()
    mismatched = sum(len(lgroup[k]) != len(rgroup[k]) for k in common)
    output = {
        "schema": "research.tt_cross_source_clock_candidates.v1", "transcription_sha256": digest,
        "source_rows": {"comparison": len(left), "video_description": len(right)},
        "date_pair_groups": len(common), "mismatched_group_counts": mismatched,
        "status": None, "pairing_space": None, "feasible_full_pairings": None,
        "feasible_pairing_offset_combinations": None, "feasible_offset_minutes": None,
        "possible_row_pairs": None, "forced_row_pairs_within_hypothesis": None,
        "reversed_label_order_range": None, "chapter_gap_minus_label_gap_seconds": None,
        "tolerance_minutes": TOLERANCE, "offset_range_minutes": [-OFFSET_LIMIT, OFFSET_LIMIT],
        "midnight_wrap_modelled": False, "crosswalk_verified": False,
        "physical_people_or_events_verified": False, "source_independence_verified": False,
        "original_pages_or_video_bytes_retained": False, "timezone_or_clock_semantics_verified": False,
        "result_availability_verified": False, "rights_verified": False,
        "model_training_authorized": False, "real_money_execution": False,
        "note": "Conditional full bijections over caller-selected source claims; no actual identity or event resolution.",
    }
    if not left or not right or mismatched:
        output["status"] = "no_full_comparison_incomplete_groups"
        return output
    groups = sorted(common)
    pairing_space = prod(factorial(len(rgroup[k])) for k in groups)
    output["pairing_space"] = pairing_space
    if pairing_space > MAX_PAIRINGS:
        output["status"] = "not_computed_pairing_cap"
        return output
    pairings, offsets, possible, forced = 0, set(), set(), None
    reverse_counts, assignment_offsets = [], 0
    alternatives = (permutations(rgroup[key]) for key in groups)
    for choices in product(*alternatives):
        pairs = [(l, r) for key, selected in zip(groups, choices)
                 for l, r in zip(lgroup[key], selected)]
        deltas = [r["clock"] - l["clock"] for l, r in pairs]
        lo = max(-OFFSET_LIMIT, max(delta - TOLERANCE for delta in deltas))
        hi = min(OFFSET_LIMIT, min(delta + TOLERANCE for delta in deltas))
        if lo > hi:
            continue
        edges = {(l["id"], r["id"]) for l, r in pairs}
        possible |= edges
        forced = edges if forced is None else forced & edges
        reverse_counts.append(sum((l["a"], l["b"]) != (r["a"], r["b"]) for l, r in pairs))
        pairings += 1
        offsets.update(range(lo, hi + 1))
        assignment_offsets += hi - lo + 1
    output.update(feasible_full_pairings=pairings, feasible_pairing_offset_combinations=assignment_offsets,
                  feasible_offset_minutes=sorted(offsets), possible_row_pairs=len(possible),
                  forced_row_pairs_within_hypothesis=len(forced or ()),
                  reversed_label_order_range=None if not reverse_counts else [min(reverse_counts), max(reverse_counts)])
    gaps = []
    for key in groups:
        rows = sorted(rgroup[key], key=lambda row: row["elapsed"])
        if len(rows) == 2:
            gaps.append((rows[1]["elapsed"] - rows[0]["elapsed"]) -
                        60 * (rows[1]["clock"] - rows[0]["clock"]))
    output["chapter_gap_minus_label_gap_seconds"] = sorted(gaps)
    output["status"] = "conditional_candidates" if pairings else "no_full_pairing_under_hypotheses"
    return output


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
