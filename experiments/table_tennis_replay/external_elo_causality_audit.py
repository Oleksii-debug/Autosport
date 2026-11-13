"""Inspect a pinned external TT pipeline on fabricated equal-time records.

The external definitions are verified by SHA-256 and extracted from reviewed
source ASTs; their top-level imports and main programs are not executed. No
credentials or network are used. The published prediction CSV inventory is
descriptive and proves neither input chronology nor predictive performance.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import io
import json
import logging
from pathlib import Path

from elo_split_graph_audit import _component_membership

SOURCE_COMMIT = "ebe19dd52f0bf6e7353f1fa5442afd2d5e361e40"
SOURCE_SHA = {
    "ratings/elo.py": "f27a61edce0c5f0ae2312f4e7077b8426b591289f2351d4ca2fbdfb74026b83b",
    "pipeline/compute_elo.py": "a3edf8791b0b1ff645ba7b8cdf6625f5019bbc523544799a05b3b08306d29f79",
    "pipeline/generate_features_v2.py": "3f3464c6ec3b1aed1940ff0829859e7fc16a293ecec99e439d3979120f13073b",
}
CSV_FIELDS = (
    "player_a_id", "player_b_id", "winner_id", "target", "cold_start_a", "cold_start_b",
    "elo_a_before", "elo_b_before", "elo_difference", "form_last_5_a", "form_last_10_a",
    "form_last_5_b", "form_last_10_b", "form_7_days_a", "form_7_days_b", "matches_last_7_a",
    "matches_last_7_b", "cumulative_matches_a", "cumulative_matches_b", "cumulative_wins_a",
    "cumulative_wins_b", "category", "p_elo", "p_rf_baseline", "p_rf_enhanced",
)
MAX_CSV_BYTES = 5_000_000


def _source(root, name):
    path = Path(root) / name
    if path.is_symlink() or not path.is_file():
        raise ValueError("expected pinned upstream source file")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA[name]:
        raise ValueError("upstream source hash mismatch")
    return raw.decode("utf-8")


def _definitions(source, wanted, namespace):
    module = ast.parse(source)
    nodes = [node for node in module.body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in wanted]
    if [node.name for node in nodes] != list(wanted):
        raise ValueError("pinned upstream definitions differ from expected")
    extracted = ast.Module(body=nodes, type_ignores=[])
    exec(compile(extracted, "<pinned-upstream-definitions>", "exec"), namespace)


def synthetic_equal_timestamp_demo(root):
    """Run exact pinned definitions only on two constructed match rows."""
    sources = {name: _source(root, name) for name in SOURCE_SHA}
    # Optional dependencies are needed only for this external-code experiment.
    import numpy as np
    import pandas as pd

    namespace = {"__name__": __name__, "dataclass": dataclass, "defaultdict": defaultdict,
                 "timedelta": timedelta, "logging": logging, "logger": None,
                 "log_structured": lambda *args, **kwargs: None, "pd": pd, "np": np}
    _definitions(sources["ratings/elo.py"],
                 ("EloConfig", "expected_score", "update_rating", "EloRatingEngine"), namespace)
    _definitions(sources["pipeline/compute_elo.py"], ("_compute_elo_ratings",), namespace)
    _definitions(sources["pipeline/generate_features_v2.py"],
                 ("_compute_recent_form_and_cumulative",), namespace)

    def trial(first_winner, *, reverse=False):
        day = pd.Timestamp("2026-01-01")
        first = {"fixture": "synthetic-first", "match_date": day, "player_a_id": 1,
                 "player_b_id": 2, "winner_id": first_winner}
        second = {"fixture": "synthetic-second", "match_date": day, "player_a_id": 1,
                  "player_b_id": 3, "winner_id": 3}
        rows = [second, first] if reverse else [first, second]
        result = namespace["_compute_elo_ratings"](pd.DataFrame(rows))
        forms = namespace["_compute_recent_form_and_cumulative"](result)
        index = int(result.index[result["fixture"] == "synthetic-second"][0])
        form = forms[0].iloc[index]
        return {"elo_a_before": float(result.iloc[index]["elo_a_before"]),
                "form_last_5_a": None if pd.isna(form) else float(form),
                "cumulative_matches_a": int(forms[8].iloc[index]),
                "cumulative_wins_a": int(forms[10].iloc[index])}

    win = trial(1)
    loss = trial(2)
    reordered = trial(1, reverse=True)
    return {"schema": "research.external_tt_equal_timestamp_probe.v1",
            "upstream_commit": SOURCE_COMMIT, "synthetic_only": True,
            "same_timestamp_on_both_input_rows": True,
            "first_row_a_wins": win, "first_row_a_loses": loss,
            "target_row_first_in_input": reordered,
            "equal_timestamp_outcome_changes_target_elo": win["elo_a_before"] != loss["elo_a_before"],
            "training_data_observed": False, "real_model_reproduced": False,
            "physical_match_verified": False, "real_money_execution": False,
            "note": "Identical match_date labels, not evidence about actual real-match times. "
                    "No upstream imports, network, credentials or real data are executed."}


def _numeric_id(value):
    return type(value) is str and bool(value) and value.isascii() and value.isdigit()


def prediction_inventory(raw, *, expected_sha256):
    """Count exact source-key structure only; do not evaluate or train a model."""
    if type(raw) is not bytes or not raw or len(raw) > MAX_CSV_BYTES:
        raise ValueError("CSV is empty or oversized")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError("prediction CSV hash mismatch")
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        if tuple(reader.fieldnames or ()) != CSV_FIELDS:
            raise ValueError("unexpected CSV columns or ordering")
        participants, pairs, identical = Counter(), Counter(), Counter()
        structural = []
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ValueError("invalid CSV row shape")
            a, b, winner, target = (row[k] for k in ("player_a_id", "player_b_id", "winner_id", "target"))
            if (not all(_numeric_id(x) for x in (a, b, winner)) or a == b or
                    winner not in (a, b) or target != ("1" if winner == a else "0")):
                raise ValueError("invalid source IDs, winner, or target")
            if row["category"] not in ("MT", "WT"):
                raise ValueError("unexpected source category")
            participants.update((a, b))
            pairs[tuple(sorted((a, b)))] += 1
            identical[tuple(row[k] for k in CSV_FIELDS)] += 1
            structural.append((None, "global", a, b))
            if len(structural) > 100_000:
                raise ValueError("too many CSV rows")
    except (UnicodeError, csv.Error) as exc:
        raise ValueError("invalid CSV") from exc
    if not structural:
        raise ValueError("empty CSV")
    components = Counter(_component_membership(structural, league_scoped=False))
    return {"schema": "research.external_tt_prediction_inventory.v1", "source_sha256": digest,
            "rows": len(structural), "columns": list(CSV_FIELDS),
            "distinct_exact_source_player_keys": len(participants),
            "source_player_keys_with_multiple_rows": sum(n > 1 for n in participants.values()),
            "distinct_unordered_source_key_pairs": len(pairs),
            "unordered_source_pairs_with_multiple_rows": sum(n > 1 for n in pairs.values()),
            "rows_in_repeated_source_pairs": sum(n for n in pairs.values() if n > 1),
            "exact_duplicate_value_groups": sum(n > 1 for n in identical.values()),
            "rows_in_exact_duplicate_value_groups": sum(n for n in identical.values() if n > 1),
            "global_source_key_components": len(components),
            "largest_component_rows": max(components.values()),
            "row_match_id_present": False, "row_match_time_present": False,
            "source_result_availability_present": False, "market_prices_present": False,
            "input_history_or_training_verified": False, "physical_identity_verified": False,
            "physical_event_deduplication_verified": False, "source_rights_verified": False,
            "independent_holdout_verified": False, "real_money_execution": False,
            "note": "Counts describe source-local key strings in a published selected CSV; "
                    "the full-sample components are retrospective, never causal features."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("synthetic")
    demo.add_argument("source_dir", type=Path)
    inv = commands.add_parser("inventory")
    inv.add_argument("csv_path", type=Path)
    inv.add_argument("--expected-sha256", required=True)
    for sub in (demo, inv):
        sub.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "synthetic":
        result = synthetic_equal_timestamp_demo(args.source_dir)
    else:
        result = prediction_inventory(args.csv_path.read_bytes(), expected_sha256=args.expected_sha256)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
