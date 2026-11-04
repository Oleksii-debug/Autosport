"""Quadratic reference for Setka exact-label time counts; no model or outputs by name.

Uses the audit's validated input tuples, but a separate pairwise counting
algorithm. This checks the aggregation, not the input parser or source truth.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import io
import json
from pathlib import Path

from setka_source_time_audit import _read


def date_interpretation_reference(raw):
    """Independently try both slash conventions with datetime.strptime."""
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
    result = {}
    for label, fmt in (("DMY", "%d/%m/%Y"), ("MDY", "%m/%d/%Y")):
        months, dates, invalid = Counter(), [], 0
        for row in rows:
            try:
                value = datetime.strptime(row["Date"], fmt).date()
            except ValueError:
                invalid += 1
                continue
            dates.append(value)
            months[value.strftime("%Y-%m")] += 1
        result[label] = {"valid": len(dates), "invalid": invalid,
                         "earliest": min(dates).isoformat() if dates else None,
                         "latest": max(dates).isoformat() if dates else None,
                         "year_month_counts": dict(sorted(months.items()))}
    return result


def temporal_counts_reference(records):
    days = defaultdict(list)
    for label, seconds, a, b in records:
        days[label].append((seconds, a, b))
    keys = ("same_time_shared_name_rows", "same_time_same_pair_rows", "earlier_any_name_rows",
            "earlier_both_names_rows", "earlier_same_pair_rows", "earlier_any_name_within_30m_rows",
            "earlier_same_pair_within_30m_rows", "earlier_any_name_within_60m_rows",
            "earlier_same_pair_within_60m_rows")
    totals = dict.fromkeys(keys, 0)
    for day in days.values():
        for i, (t, a, b) in enumerate(day):
            pair = frozenset((a, b))
            earlier = [(t - u, frozenset((x, y))) for u, x, y in day if u < t]
            simultaneous = [frozenset((x, y)) for j, (u, x, y) in enumerate(day) if j != i and u == t]
            totals["same_time_shared_name_rows"] += int(any(pair & s for s in simultaneous))
            totals["same_time_same_pair_rows"] += int(pair in simultaneous)
            totals["earlier_any_name_rows"] += int(any(pair & s for _, s in earlier))
            totals["earlier_both_names_rows"] += int(
                any(a in s for _, s in earlier) and any(b in s for _, s in earlier))
            totals["earlier_same_pair_rows"] += int(any(s == pair for _, s in earlier))
            for minutes in (30, 60):
                recent = [(age, s) for age, s in earlier if age <= minutes * 60]
                totals[f"earlier_any_name_within_{minutes}m_rows"] += int(any(pair & s for _, s in recent))
                totals[f"earlier_same_pair_within_{minutes}m_rows"] += int(any(s == pair for _, s in recent))
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    raw = args.sample.read_bytes()
    info, records = _read(raw, expected_sha256=args.expected_sha256)
    print(json.dumps({"source_sha256": info["source_sha256"], "eligible_temporal_rows": len(records),
                      "temporal_counts": temporal_counts_reference(records),
                      "date_interpretation_reference": date_interpretation_reference(raw),
                      "independent_temporal_input_parser": False}, sort_keys=True))


if __name__ == "__main__":
    main()
