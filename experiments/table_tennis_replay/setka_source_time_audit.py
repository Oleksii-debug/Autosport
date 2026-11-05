"""Inventory a pinned Setka-derived CSV's labels, without fitting a model.

Dates are not disambiguated, names are not resolved, and times do not prove
when any result was available. Output is aggregate; input labels remain private.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import re

from wtt_archive_inventory import _day, _present

FIELDS = ("X", "Date", "Time", "Player1", "Player2", "Sets_P1", "Sets_P2",
          "P1_G1", "P2_G1", "P1_G2", "P2_G2", "P1_G3", "P2_G3",
          "P1_G4", "P2_G4", "P1_G5", "P2_G5", "HomeWinner")
MAX_BYTES = 2_000_000
_SLASH = re.compile(r"^([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})$")
_TIME = re.compile(r"^([0-9]{1,2}):([0-9]{2}):([0-9]{2})$")


def _date_label(value):
    """Return both possible dates; never select a slash-date convention."""
    iso = _day(value)
    if iso is not None:
        return "iso", iso, iso
    match = _SLASH.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return "invalid", None, None
    first, second, year = map(int, match.groups())

    def candidate(month, day):
        try:
            return date(year, month, day)
        except ValueError:
            return None

    dmy, mdy = candidate(second, first), candidate(first, second)
    if dmy and mdy:
        return ("slash_both_same" if dmy == mdy else "slash_both_different"), dmy, mdy
    if dmy:
        return "slash_only_dmy", dmy, None
    if mdy:
        return "slash_only_mdy", None, mdy
    return "invalid", None, None


def _time_label(value):
    match = _TIME.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    hours, minutes, seconds = map(int, match.groups())
    if hours > 23 or minutes > 59 or seconds > 59:
        return None
    return 3600 * hours + 60 * minutes + seconds


def _read(raw, *, expected_sha256):
    """Read only structural fields and return private label tuples in memory."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        raise ValueError("empty or oversized Setka sample")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError("Setka sample SHA-256 mismatch")
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        fields = reader.fieldnames
        if not fields or len(fields) != len(set(fields)) or set(fields) != set(FIELDS):
            raise ValueError("invalid Setka sample CSV header")
        counts, date_classes = Counter(), Counter()
        dmy_months, mdy_months = Counter(), Counter()
        source_indices, names, pending = Counter(), set(), []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("invalid Setka sample CSV row shape")
            counts["rows"] += 1
            index, label, time, a, b = (row[k] for k in ("X", "Date", "Time", "Player1", "Player2"))
            index_ok = _present(index) and index == index.strip()
            if index_ok:
                source_indices[index] += 1
            else:
                counts["missing_or_noncanonical_source_row_label"] += 1
            kind, dmy, mdy = _date_label(label)
            date_classes[kind] += 1
            if dmy is not None:
                dmy_months[dmy.month] += 1
            if mdy is not None:
                mdy_months[mdy.month] += 1
            seconds = _time_label(time)
            counts["invalid_time_labels"] += int(seconds is None)
            names_ok = all(_present(x) and x == x.strip() for x in (a, b))
            counts["missing_or_noncanonical_player_label_rows"] += int(not names_ok)
            counts["same_exact_label_both_sides_rows"] += int(names_ok and a == b)
            if names_ok:
                names.update((a, b))
            if index_ok and kind != "invalid" and seconds is not None and names_ok and a != b:
                pending.append((index, label, seconds, a, b))
        if not counts["rows"]:
            raise ValueError("empty Setka sample CSV")
    except (UnicodeError, csv.Error) as exc:
        raise ValueError("invalid Setka sample CSV encoding or structure") from exc
    repeated = {index for index, n in source_indices.items() if n > 1}
    records = [(label, seconds, a, b) for index, label, seconds, a, b in pending if index not in repeated]
    info = {
        "source_sha256": digest, "source_bytes": len(raw), "columns": fields,
        "rows": counts["rows"], "exact_name_strings": len(names),
        "missing_or_noncanonical_source_row_label": counts["missing_or_noncanonical_source_row_label"],
        "duplicate_source_row_label_values": len(repeated),
        "rows_with_duplicate_source_row_label": sum(source_indices[i] for i in repeated),
        "date_label_classes": {k: date_classes[k] for k in ("iso", "slash_both_same", "slash_both_different",
                                                            "slash_only_dmy", "slash_only_mdy", "invalid")},
        "month_counts_if_dmy": {str(m): dmy_months[m] for m in range(1, 13)},
        "month_counts_if_mdy": {str(m): mdy_months[m] for m in range(1, 13)},
        "invalid_time_labels": counts["invalid_time_labels"],
        "missing_or_noncanonical_player_label_rows": counts["missing_or_noncanonical_player_label_rows"],
        "same_exact_label_both_sides_rows": counts["same_exact_label_both_sides_rows"],
        "eligible_temporal_rows": len(records), "excluded_temporal_rows": counts["rows"] - len(records),
        "raw_date_groups": len({r[0] for r in records}),
    }
    return info, records


def temporal_counts(records):
    """Compute exact-label coincidences, batching ties before updating history."""
    days = defaultdict(lambda: defaultdict(list))
    for label, seconds, a, b in records:
        days[label][seconds].append((a, b))
    out = Counter()
    for times in days.values():
        players, pairs = {}, {}
        for seconds, batch in sorted(times.items()):
            simultaneous_players, simultaneous_pairs = Counter(), Counter()
            for a, b in batch:
                simultaneous_players.update((a, b))
                simultaneous_pairs[tuple(sorted((a, b)))] += 1
            for a, b in batch:
                pair = tuple(sorted((a, b)))
                age_a = seconds - players[a] if a in players else None
                age_b = seconds - players[b] if b in players else None
                age_pair = seconds - pairs[pair] if pair in pairs else None
                out["same_time_shared_name_rows"] += int(simultaneous_players[a] > 1 or simultaneous_players[b] > 1)
                out["same_time_same_pair_rows"] += int(simultaneous_pairs[pair] > 1)
                out["earlier_any_name_rows"] += int(age_a is not None or age_b is not None)
                out["earlier_both_names_rows"] += int(age_a is not None and age_b is not None)
                out["earlier_same_pair_rows"] += int(age_pair is not None)
                for minutes in (30, 60):
                    out[f"earlier_any_name_within_{minutes}m_rows"] += int(
                        any(age is not None and 0 < age <= 60 * minutes for age in (age_a, age_b)))
                    out[f"earlier_same_pair_within_{minutes}m_rows"] += int(
                        age_pair is not None and 0 < age_pair <= 60 * minutes)
            for a, b in batch:
                players[a] = players[b] = seconds
                pairs[tuple(sorted((a, b)))] = seconds
    keys = ("same_time_shared_name_rows", "same_time_same_pair_rows", "earlier_any_name_rows",
            "earlier_both_names_rows", "earlier_same_pair_rows", "earlier_any_name_within_30m_rows",
            "earlier_same_pair_within_30m_rows", "earlier_any_name_within_60m_rows",
            "earlier_same_pair_within_60m_rows")
    return {key: out[key] for key in keys}


def audit(raw, *, expected_sha256):
    info, records = _read(raw, expected_sha256=expected_sha256)
    return {"schema": "research.tt_setka_source_time_audit.v1", **info,
            "temporal_counts": temporal_counts(records),
            "comparison_scope": "same exact raw date label and exact player-name strings",
            "clock_windows": "strictly earlier, at most 30 or 60 minutes; tied times batched",
            "source_dates_and_timezone_verified": False, "players_identified": False,
            "physical_matches_deduplicated": False, "result_availability_verified": False,
            "source_rights_verified": False, "independent_holdout": False,
            "model_training_authorized": False, "bookmaker_execution": False,
            "profitability_claim": False,
            "note": "Label coincidences and illustrative spacing only; not event chronology or causal history."}


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
