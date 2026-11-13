"""Small brute-force arithmetic check on the same pinned transcription.

Shares the main parser; not an independent source, crosswalk or truth check.
It enumerates offsets first and all row permutations (maximum four rows).
"""
import argparse
from itertools import permutations
import json
from pathlib import Path

from cross_source_clock_candidates import OFFSET_LIMIT, TOLERANCE, _group, _parse


def audit(raw, *, expected_sha256):
    digest, left, right = _parse(raw, expected_sha256)
    if len(left) != len(right) or not left or len(left) > 4:
        raise ValueError("reference requires equal populations of at most four rows")
    unique_pairings, offsets, possible, forced = set(), set(), set(), None
    combinations = 0
    for offset in range(-OFFSET_LIMIT, OFFSET_LIMIT + 1):
        for perm in permutations(right):
            if not all(_group(l) == _group(r) and
                       abs(r["clock"] - l["clock"] - offset) <= TOLERANCE
                       for l, r in zip(left, perm)):
                continue
            edges = frozenset((l["id"], r["id"]) for l, r in zip(left, perm))
            unique_pairings.add(edges)
            offsets.add(offset)
            combinations += 1
            possible |= edges
            forced = set(edges) if forced is None else forced & edges
    return {"schema": "research.tt_cross_source_clock_reference.v1", "transcription_sha256": digest,
            "feasible_full_pairings": len(unique_pairings),
            "feasible_pairing_offset_combinations": combinations,
            "feasible_offset_minutes": sorted(offsets), "possible_row_pairs": len(possible),
            "forced_row_pairs_within_hypothesis": len(forced or ())}


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
