import csv
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from evaluate_market_residual import MarketResidual, evaluate

CONFIG = json.loads((Path(__file__).parents[1] / "examples/recorded_cross_book_value.json").read_text())


def row(identity, kickoff, result="1", home="Home", away="Away", void=""):
    return {"id": str(identity), "kickoff_utc": kickoff, "result": result, "void_reason": void,
            "country": "Country", "league": "League", "home": home, "away": away,
            "fair_prob_home": ".5", "fair_prob_draw": ".3", "fair_prob_away": ".2",
            "best_soft_home": "2.04", "best_soft_draw": "3", "best_soft_away": "1.8",
            "best_soft_book_home": "book", "best_soft_book_draw": "", "best_soft_book_away": "book"}


def csv_file(tmp_path, rows, name):
    path = tmp_path / name
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_team_update_waits_for_delayed_result_and_never_sees_current_outcome(tmp_path):
    rows = [row(i, "2026-09-21T00:00:00+00:00") for i in range(5)]
    rows += [row("early", "2026-09-22T23:59:00+00:00"), row("later", "2026-09-23T00:00:00+00:00")]
    report = evaluate(csv_file(tmp_path, rows, "a.csv"), CONFIG)
    entries = {x["id"]: x for x in report["decisions"]}
    assert entries["early"]["team_support"] == [0, 0]
    assert entries["later"]["team_support"] == [5, 5]
    assert entries["later"]["decisions"]["baseline"] is None
    assert entries["later"]["decisions"]["team_residual"]["selection"] == "1"
    rows[-1]["result"] = "2"
    changed = evaluate(csv_file(tmp_path, rows, "b.csv"), CONFIG)
    later = {x["id"]: x for x in changed["decisions"]}["later"]
    assert later["probabilities"] == entries["later"]["probabilities"]
    assert later["decisions"]["team_residual"]["selection"] == "1"
    assert report["promotion_authorized"] is False


def test_extreme_probabilities_stay_positive_and_normalized():
    model = MarketResidual()
    match = row(1, "2026-09-21T00:00:00+00:00")
    for _ in range(100):
        model.update(match, {"home": Decimal(".2"), "draw": Decimal(".3"), "away": Decimal(".5")}, "1")
    p = {"home": Decimal(".0001"), "draw": Decimal(".9998"), "away": Decimal(".0001")}
    forecast = model.predict(match, p)
    assert all(x > 0 for x in forecast.values())
    assert sum(forecast.values()) == 1
    match["away"] = match["home"]
    assert model.predict(match, p) == p


def test_duplicate_match_rejected(tmp_path):
    data = [row(1, "2026-09-21T00:00:00+00:00"), row(1, "2026-09-22T00:00:00+00:00")]
    with pytest.raises(ValueError, match="duplicate"):
        evaluate(csv_file(tmp_path, data, "duplicate.csv"), CONFIG)
