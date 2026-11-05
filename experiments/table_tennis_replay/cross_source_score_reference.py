"""Independent quadratic arithmetic check for one pinned manual transcription.

This is deliberately not an input validator or event/identity resolver. It
does not verify that the transcription reflects either source or that the
two publishers have independent data. Duplicate claims count multiplicatively.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

EXPECTED = "940bde1036d591af40998ce76ac68c0746dafa5c805a20d96ab49e3d55c8a580"


def count_pairs(document):
    counts = Counter()
    for left in document["left"]:
        for right in document["right"]:
            ra, rb = (document["right_to_left"][right[key]] for key in ("a", "b"))
            if left["date"] != right["date"] or {left["a"], left["b"]} != {ra, rb}:
                continue
            reverse = (left["a"], left["b"]) != (ra, rb)
            right_sets = [tuple(map(int, text.split("-"))) for text in right["score"].split(",")]
            if reverse:
                right_sets = [(b, a) for a, b in right_sets]
            left_sets = [tuple(map(int, text.split("-"))) for text in left["score"].split(",")]
            if left_sets != right_sets:
                continue
            counts["raw_pairwise_score_matches"] += 1
            counts["opposite_order"] += int(reverse)
            if left["clock"] is None or right["clock"] is None:
                counts["missing_clock_label"] += 1
            elif left["clock"] == right["clock"]:
                counts["same_clock_label"] += 1
            else:
                counts["different_clock_label"] += 1
    return dict(sorted(counts.items()))


def main():
    raw = Path(sys.argv[1]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED:
        raise ValueError("not the pinned two-source transcription")
    print(json.dumps(count_pairs(json.loads(raw)), sort_keys=True))


if __name__ == "__main__":
    main()
