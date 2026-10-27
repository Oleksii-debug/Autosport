import csv
from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("value_experiment", SCRIPTS / "evaluate_recorded_value_agent.py")
agent = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SCRIPTS))
try:
    spec.loader.exec_module(agent)
finally:
    sys.path.pop(0)


def config(**changes):
    return {"split_date": "2026-09-21", "bins": 10, "prior_observations": 100,
            "minimum_bin_observations": 1, "minimum_expected_return": "0.05",
            "maximum_odds": "10", **changes}


def test_no_edge_means_wait_instead_of_forced_bet():
    odds = {"1": Decimal("2"), "X": Decimal("3"), "2": Decimal("4")}
    probabilities = agent.fair_probabilities(odds)
    assert sum(probabilities.values()) == 1
    assert agent.select(odds, probabilities, True, config()) is None
    assert agent.select(odds, {"1": Decimal("0.8")}, False, config()) is None


def test_edge_selects_one_single_and_honors_odds_cap():
    odds = {"1": Decimal("2"), "X": Decimal("3"), "2": Decimal("20")}
    probabilities = {"1": Decimal("0.6"), "X": Decimal("0.2"), "2": Decimal("0.2")}
    assert agent.select(odds, probabilities, True, config()) == "1"
    assert agent.select(odds, probabilities, True, config(maximum_odds="1.5")) is None


def test_sparse_memory_abstains_and_forecasts_remain_probabilities():
    odds = {"1": Decimal("2"), "X": Decimal("3"), "2": Decimal("4")}
    model = agent.fit([{"odds": odds, "result": "1"}], config())
    base, calibrated, supported = agent.forecast(odds, model, config(minimum_bin_observations=100))
    assert not supported
    assert abs(sum(calibrated.values()) - 1) < Decimal("1e-25")
    assert all(0 < value < 1 for value in calibrated.values())
    assert calibrated["1"] > base["1"]


def source(path, validation_result):
    rows = []
    for index in range(50):
        rows.append({"id": str(index), "kickoff_utc": "2026-09-20T12:00:00+00:00",
                     "result": "1", "void_reason": "", "pin_open_home": "2",
                     "pin_open_draw": "3", "pin_open_away": "4",
                     "prediction_pick": "1", "prediction_odds_taken": "2"})
    rows.append({**rows[0], "id": "validation", "kickoff_utc": "2026-09-22T12:00:00+00:00",
                 "result": validation_result})
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


def test_future_outcomes_cannot_change_fit_forecasts_or_choices(tmp_path):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    source(left, "1")
    source(right, "2")
    a, b = agent.evaluate(left, config()), agent.evaluate(right, config())
    assert a["model"] == b["model"]
    assert a["decisions"] == b["decisions"]
    assert a["decisions"][0]["selection"] == "1"
    assert Decimal(a["validation"]["zero_cost"]["net_units"]) == 1
    assert Decimal(b["validation"]["zero_cost"]["net_units"]) == -1
    assert Decimal(a["validation"]["one_percent_stake_cost"]["net_units"]) == Decimal("0.99")
    assert a["profitability_claim"] is False
    assert a["promotion_authorized"] is False


@pytest.mark.parametrize("change", [{"bins": True}, {"minimum_expected_return": "NaN"},
                                   {"maximum_odds": "1"}, {"prior_observations": 0}])
def test_invalid_config_rejected(change):
    with pytest.raises(ValueError):
        agent.validate_config(config(**change))
