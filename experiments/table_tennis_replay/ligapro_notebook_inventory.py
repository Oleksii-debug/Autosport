"""Bounded descriptive audit of a pinned public LigaPro example.

No model fit, prediction, identity resolution, data-rights assertion or
execution occurs. Time values and names are publisher strings, not evidence
of physical identity or point-in-time availability.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path

import pandas as pd
import sklearn
from sklearn.model_selection import train_test_split
from wtt_archive_inventory import _present

FIELDS = ("Date", "Player1", "Player2", "Sets_P1", "Sets_P2",
          "P1_G1", "P2_G1", "P1_G2", "P2_G2", "P1_G3", "P2_G3",
          "P1_G4", "P2_G4", "P1_G5", "P2_G5", "HomeWinner")
SCORE_FIELDS = tuple(f"P{p}_G{g}" for g in range(1, 6) for p in (1, 2))
MAX_BYTES = 2_000_000
MAX_ROWS = 10_000
REFERENCE_PAIR_CAP = 5_000_000


def _canonical_name(value):
    return _present(value) and value == value.strip()


def _pair(row):
    a, b = row["Player1"], row["Player2"]
    if not (_canonical_name(a) and _canonical_name(b)) or a == b:
        return None
    return tuple(sorted((a, b)))


def _signature(row):
    """Exact text plus a reversible side orientation; blank is not zero."""
    pair = _pair(row)
    if pair is None or row["HomeWinner"] not in ("0", "1") or not _present(row["Date"]):
        return None
    reversed_sides = row["Player1"] != pair[0]
    def oriented(left, right):
        return (right, left) if reversed_sides else (left, right)
    sets = oriented(row["Sets_P1"], row["Sets_P2"])
    games = tuple(oriented(row[f"P1_G{g}"], row[f"P2_G{g}"]) for g in range(1, 6))
    winner = str(1 - int(row["HomeWinner"])) if reversed_sides else row["HomeWinner"]
    return row["Date"], pair, sets, games, winner


def _reference(train, test):
    """Quadratic comparison on identical parsed rows; not an independent source."""
    names_train = {name for row in train if row["pair"] for name in row["pair"]}
    names_test = {name for row in test if row["pair"] for name in row["pair"]}
    out = {"exact_names_in_both_splits": len(names_train & names_test),
           "test_rows_with_any_train_name": 0, "test_rows_with_both_train_names": 0,
           "test_rows_with_same_train_pair": 0, "test_rows_with_any_later_train_name": 0,
           "test_rows_with_later_same_train_pair": 0,
           "test_rows_with_identical_raw_row_in_train": 0,
           "test_rows_with_oriented_signature_in_train": 0,
           "test_rows_with_same_time_and_pair_in_train": 0}
    for item in test:
        if item["pair"]:
            found = {name for other in train if other["pair"]
                     for name in item["pair"] if name in other["pair"]}
            out["test_rows_with_any_train_name"] += bool(found)
            out["test_rows_with_both_train_names"] += len(found) == 2
            out["test_rows_with_same_train_pair"] += any(
                item["pair"] == other["pair"] for other in train)
            if item["time"] is not None:
                out["test_rows_with_any_later_train_name"] += any(
                    other["pair"] and other["time"] is not None and other["time"] > item["time"]
                    and bool(set(item["pair"]) & set(other["pair"])) for other in train)
                out["test_rows_with_later_same_train_pair"] += any(
                    other["pair"] == item["pair"] and other["time"] is not None
                    and other["time"] > item["time"] for other in train)
            out["test_rows_with_same_time_and_pair_in_train"] += any(
                other["pair"] == item["pair"] and other["raw_time"] == item["raw_time"]
                for other in train)
        out["test_rows_with_identical_raw_row_in_train"] += any(
            other["raw"] == item["raw"] for other in train)
        if item["signature"] is not None:
            out["test_rows_with_oriented_signature_in_train"] += any(
                other["signature"] == item["signature"] for other in train)
    return out


def _count(train, test):
    names = {name for item in train if item["pair"] for name in item["pair"]}
    names_test = {name for item in test if item["pair"] for name in item["pair"]}
    pairs = {item["pair"] for item in train if item["pair"]}
    raw = {item["raw"] for item in train}
    signatures = {item["signature"] for item in train if item["signature"] is not None}
    pair_times = {(item["raw_time"], item["pair"]) for item in train if item["pair"]}
    latest_name, latest_pair = {}, {}
    for item in train:
        pair, time = item["pair"], item["time"]
        if not pair or time is None:
            continue
        for key in pair:
            if key not in latest_name or time > latest_name[key]:
                latest_name[key] = time
        if pair not in latest_pair or time > latest_pair[pair]:
            latest_pair[pair] = time
    return {"exact_names_in_both_splits": len(names & names_test),
            "test_rows_with_any_train_name": sum(bool(item["pair"] and set(item["pair"]) & names) for item in test),
            "test_rows_with_both_train_names": sum(bool(item["pair"] and set(item["pair"]) <= names) for item in test),
            "test_rows_with_same_train_pair": sum(bool(item["pair"] and item["pair"] in pairs) for item in test),
            "test_rows_with_any_later_train_name": sum(bool(item["pair"] and item["time"] is not None and
                any(name in latest_name and latest_name[name] > item["time"] for name in item["pair"])) for item in test),
            "test_rows_with_later_same_train_pair": sum(bool(item["pair"] and item["time"] is not None and
                item["pair"] in latest_pair and latest_pair[item["pair"]] > item["time"]) for item in test),
            "test_rows_with_identical_raw_row_in_train": sum(item["raw"] in raw for item in test),
            "test_rows_with_oriented_signature_in_train": sum(item["signature"] is not None and
                item["signature"] in signatures for item in test),
            "test_rows_with_same_time_and_pair_in_train": sum(bool(item["pair"] and
                (item["raw_time"], item["pair"]) in pair_times) for item in test)}


def _load_notebook_population(raw, *, expected_sha256):
    """Read pinned data and apply the notebook's cleaning; no fit or prediction."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        raise ValueError("empty or oversized CSV")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError("CSV differs from pinned SHA-256")
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise ValueError("CSV header differs from declared source fields")
        rows = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("invalid CSV row shape")
            rows.append(row)
            if len(rows) > MAX_ROWS:
                raise ValueError("CSV row cap exceeded")
    except (UnicodeError, csv.Error) as exc:
        raise ValueError("invalid CSV encoding or structure") from exc
    if not rows:
        raise ValueError("empty CSV population")
    df = pd.read_csv(io.BytesIO(raw))
    if len(df) != len(rows) or tuple(df.columns) != FIELDS:
        raise ValueError("pandas and CSV reader disagree on source shape")
    for col in SCORE_FIELDS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    clean = df.dropna(subset=["HomeWinner", "P1_G1", "P2_G1", "P1_G2", "P2_G2",
                               "Player1", "Player2", "Date"]).copy()
    clean["HomeWinner"] = clean["HomeWinner"].astype(int)
    clean["Date"] = pd.to_datetime(clean["Date"], errors="coerce")
    clean = clean.dropna(subset=["Date"])
    if clean.empty or not set(clean["HomeWinner"].unique()) <= {0, 1}:
        raise ValueError("notebook-cleaned labels must be binary and nonempty")
    return digest, rows, clean


def _notebook_split(clean):
    """Verify local index and original two-margin frame splits coincide."""
    indices = clean.index.to_numpy()
    train_idx, test_idx = train_test_split(indices, test_size=0.2, random_state=42,
                                           stratify=clean["HomeWinner"])
    # Independently invoke the notebook's original feature-frame split. The
    # feature values are not fitted or scored; index membership must agree.
    features = pd.DataFrame({"g1_margin": clean["P1_G1"] - clean["P2_G1"],
                             "g2_margin": clean["P1_G2"] - clean["P2_G2"]}, index=clean.index)
    x_train, x_test, _, _ = train_test_split(features, clean["HomeWinner"], test_size=0.2,
                                             random_state=42, stratify=clean["HomeWinner"])
    if list(train_idx) != list(x_train.index) or list(test_idx) != list(x_test.index):
        raise ValueError("index split differs from notebook feature-frame split")
    return train_idx, test_idx


def audit(raw, *, expected_sha256, reference_pair_cap=REFERENCE_PAIR_CAP):
    """Match the notebook's cleaning/split in installed versions; no fit."""
    if type(reference_pair_cap) is not int or not 0 <= reference_pair_cap <= REFERENCE_PAIR_CAP:
        raise ValueError("invalid reference pair cap")
    digest, rows, clean = _load_notebook_population(raw, expected_sha256=expected_sha256)
    train_idx, test_idx = _notebook_split(clean)

    def materialize(indices):
        result = []
        for index in indices:
            row = rows[int(index)]
            pair = _pair(row)
            time = clean.loc[index, "Date"]
            # A timezone-aware value is excluded from naive time comparisons.
            time = time if getattr(time, "tzinfo", None) is None else None
            result.append({"raw": tuple(row[k] for k in FIELDS), "raw_time": row["Date"],
                           "pair": pair, "signature": _signature(row), "time": time})
        return result

    train, test = materialize(train_idx), materialize(test_idx)
    measures = _count(train, test)
    comparisons = len(train) * len(test)
    if comparisons <= reference_pair_cap:
        if _reference(train, test) != measures:
            raise ValueError("direct pairwise reference differs from indexed counts")
        reference = "matched_direct_pairwise"
    else:
        reference = "skipped_pair_cap"
    raw_groups = Counter(item["raw"] for item in train + test)
    pair_time_groups = Counter((item["raw_time"], item["pair"]) for item in train + test if item["pair"])
    pairs = Counter(item["pair"] for item in train + test if item["pair"])
    names = Counter(name for item in train + test if item["pair"] for name in item["pair"])
    source_dates = [value.date() for value in clean["Date"]]
    return {"schema": "research.tt_ligapro_notebook_inventory.v1",
            "source_sha256": digest, "source_bytes": len(raw), "source_rows": len(rows),
            "source_columns": list(FIELDS), "explicit_match_id_column": False,
            "explicit_participant_id_column": False,
            "notebook_clean_rows": len(clean), "excluded_by_notebook_cleaning": len(rows) - len(clean),
            "split": {"method": "stratified_random", "random_state": 42, "test_size": "0.2",
                      "train_rows": len(train), "test_rows": len(test),
                      "pandas_version": pd.__version__, "sklearn_version": sklearn.__version__,
                      "notebook_feature_frame_indices_match": True},
            "date_labels": {"earliest": min(source_dates).isoformat(),
                            "latest": max(source_dates).isoformat(),
                            "naive_rows": sum(item["time"] is not None for item in train + test),
                            "timezone_aware_excluded_from_time_comparison": sum(item["time"] is None for item in train + test)},
            "pair_signature_excluded_rows": sum(item["signature"] is None for item in train + test),
            "name_pair_excluded_rows": sum(item["pair"] is None for item in train + test),
            "exact_names_in_name_pair_rows": len(names),
            "exact_names_repeated_in_name_pair_rows": sum(n > 1 for n in names.values()),
            "exact_name_pair_groups": len(pairs),
            "repeated_name_pair_groups": sum(n > 1 for n in pairs.values()),
            "rows_in_repeated_name_pair_groups": sum(n for n in pairs.values() if n > 1),
            "raw_exact_duplicate_groups": sum(count > 1 for count in raw_groups.values()),
            "raw_rows_in_duplicate_groups": sum(count for count in raw_groups.values() if count > 1),
            "same_time_name_pair_groups": sum(count > 1 for count in pair_time_groups.values()),
            "same_time_name_pair_rows": sum(count for count in pair_time_groups.values() if count > 1),
            "split_overlap": measures, "reference": {"status": reference, "candidate_pairs": comparisons,
                "same_source_rows_and_parsing": True},
            "source_names_verified_as_people": False, "source_times_verified": False,
            "cross_provider_identity_verified": False, "underlying_data_rights_verified": False,
            "historical_result_availability_verified": False, "actual_notebook_run_verified": False,
            "independent_holdout": False, "model_fit_or_predict": False,
            "bookmaker_acceptance_verified": False, "real_money_execution": False,
            "profitability_claim": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    report = audit(args.csv.read_bytes(), expected_sha256=args.expected_sha256)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
