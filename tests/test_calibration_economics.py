import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from evaluate_calibration_economics import evaluate


CONFIG = json.loads((Path(__file__).parents[1] / "examples/recorded_cross_book_value.json").read_text())


def row(identity, kickoff, result="2", void=""):
    return {"id": str(identity), "kickoff_utc": kickoff, "result": result, "void_reason": void,
            "fair_prob_home": ".5", "fair_prob_draw": ".3", "fair_prob_away": ".2",
            "best_soft_home": "2.2", "best_soft_draw": "3", "best_soft_away": "1.8",
            "best_soft_book_home": "book", "best_soft_book_draw": "", "best_soft_book_away": "book"}


def csv_file(tmp_path, rows, filename):
    path = tmp_path / filename
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_calibration_uses_only_revealed_labels_for_choices_and_scores(tmp_path):
    rows = [row(i, "2026-09-21T00:00:00+00:00") for i in range(100)]
    rows += [row("early", "2026-09-22T23:59:00+00:00"),
             row("later", "2026-09-23T00:00:00+00:00", "1")]
    first = evaluate(csv_file(tmp_path, rows, "first.csv"), CONFIG)
    by_id = {item["id"]: item for item in first["decisions"]}
    assert by_id["early"]["training_events"] == 0
    assert by_id["later"]["training_events"] == 100
    assert by_id["later"]["decisions"]["baseline"]["selection"] == "1"
    assert by_id["later"]["decisions"]["calibrated"]["selection"] == "2"
    rows[-1]["result"] = "2"
    second = evaluate(csv_file(tmp_path, rows, "second.csv"), CONFIG)
    new = {item["id"]: item for item in second["decisions"]}["later"]
    assert new["decisions"]["calibrated"]["selection"] == "2"
    assert new["decisions"]["calibrated"]["model_value"] == by_id["later"]["decisions"]["calibrated"]["model_value"]
    assert first["variants"]["baseline"]["brier"] != first["variants"]["calibrated"]["brier"]
    assert first["profitability_claim"] is False


def test_void_and_pending_consume_cap_without_result_peeking(tmp_path):
    rows = [row(i, f"2026-09-23T0{i}:00:00+00:00", "", "cancelled" if i == 0 else "") for i in range(6)]
    report = evaluate(csv_file(tmp_path, rows, "voids.csv"), CONFIG)
    assert report["variants"]["baseline"]["candidates_including_void_and_pending"] == 5
    assert report["variants"]["baseline"]["one_percent_cost"]["bets"] == 0
    assert report["decisions"][-1]["decisions"]["baseline"] is None


def test_duplicate_match_rejected(tmp_path):
    rows = [row(1, "2026-09-21T00:00:00+00:00"), row(1, "2026-09-22T00:00:00+00:00")]
    with pytest.raises(ValueError, match="duplicate"):
        evaluate(csv_file(tmp_path, rows, "duplicate.csv"), CONFIG)
