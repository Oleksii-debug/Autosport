import csv
import importlib.util
from pathlib import Path
from decimal import Decimal

import pytest


spec = importlib.util.spec_from_file_location(
    "recorded_benchmark", Path(__file__).resolve().parents[1] / "scripts/benchmark_recorded_match_returns.py"
)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def write_rows(path, results=("1", "1", "2", "X")):
    rows = []
    for index, result in enumerate(results):
        rows.append({"id": str(index), "kickoff_utc": (
            f"2026-09-{19 + index:02d}T12:00:00+00:00"),
            "result": result, "void_reason": "", "pin_open_home": "2",
            "pin_open_draw": "3", "pin_open_away": "5",
            "prediction_pick": "1X", "prediction_odds_taken": "1.4",
            "pin_close_home": "999", "best_soft_home": "999"})
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    return rows


def test_results_settle_real_outcome_not_price_movement(tmp_path):
    path = tmp_path / "source.csv"
    write_rows(path)
    rows, info = benchmark.parse_rows(path)
    assert rows[0]["rewards"] == {"LOW_ODDS": Decimal("1"), "HIGH_ODDS": Decimal("-1"),
                                   "SOURCE_PICK": Decimal("0.4")}
    assert rows[2]["rewards"]["HIGH_ODDS"] == 4
    assert rows[3]["rewards"]["SOURCE_PICK"] == Decimal("0.4")
    assert info["eligible_rows"] == 4
    assert benchmark.summarize(rows, "LOW_ODDS")["net_units"] == "0"
    assert benchmark.summarize(rows, "LOW_ODDS", 100)["net_units"] == "-0.04"


def test_validation_cannot_choose_training_strategy(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    write_rows(a)
    write_rows(b, ("1", "1", "1", "1"))
    ra = benchmark.benchmark(a, minimum_training=2)
    rb = benchmark.benchmark(b, minimum_training=2)
    assert ra["training_selected_strategy"] == rb["training_selected_strategy"] == "LOW_ODDS"
    assert ra["training"] == rb["training"]
    assert ra["validation"] != rb["validation"]
    assert ra["quote_timing_verified"] is False
    assert ra["profitability_claim"] is False


def test_duplicate_match_cannot_inflate_return(tmp_path):
    path = tmp_path / "source.csv"
    write_rows(path)
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines + [lines[1]]) + "\n")
    with pytest.raises(ValueError, match="duplicate"):
        benchmark.parse_rows(path)


def test_voids_and_missing_odds_are_counted_not_silently_dropped(tmp_path):
    path = tmp_path / "source.csv"
    write_rows(path, ("VOID", "1", "2", "X"))
    lines = path.read_text().splitlines()
    lines[2] = lines[2].replace(",2,3,5,", ",NaN,3,5,")
    path.write_text("\n".join(lines) + "\n")
    _, info = benchmark.parse_rows(path)
    assert info["source_rows"] == 4
    assert info["exclusions"]["void_or_unsettled"] == 1
    assert info["exclusions"]["missing_or_invalid_common_price_cohort"] == 1
    assert info["eligible_rows"] == 2


def test_insufficient_training_never_claims_strategy_winner(tmp_path):
    path = tmp_path / "source.csv"
    write_rows(path)
    with pytest.raises(ValueError, match="insufficient"):
        benchmark.benchmark(path)
