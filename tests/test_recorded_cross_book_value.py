import csv
import importlib.util
from pathlib import Path
import sys
from decimal import Decimal

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("cross_book_experiment", SCRIPTS / "evaluate_recorded_cross_book_value.py")
experiment = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SCRIPTS))
try:
    spec.loader.exec_module(experiment)
finally:
    sys.path.pop(0)


def config(**changes):
    return dict(earliest_quote_date="2026-09-21", maximum_candidates_per_kickoff_day=5,
                minimum_probability="0.30", minimum_model_value="0.05", maximum_model_value="0.12",
                maximum_odds="3.5", stress_cost_bps_per_stake=100, **changes)


def item(**changes):
    return dict(id="one", kickoff_utc="2026-09-22T12:00:00+00:00", result="1", void_reason="",
                fair_prob_home="0.5", fair_prob_draw="0.3", fair_prob_away="0.2",
                best_soft_home="2.2", best_soft_draw="3.3", best_soft_away="6",
                best_soft_book_home="book-a", best_soft_book_draw="book-b", best_soft_book_away="book-c", **changes)


def write(path, rows):
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


def test_selection_does_not_use_result_and_excludes_implausible_gaps():
    first = item()
    second = {**first, "result": "2", "best_soft_home": "2.2"}
    assert experiment.pick_quote(first, config()) == experiment.pick_quote(second, config())
    assert experiment.pick_quote(first, config())[1] == "1"
    assert experiment.pick_quote({**first, "best_soft_home": "20"}, config()) is None


def test_real_result_settlement_and_fee_drag(tmp_path):
    path = tmp_path / "prices.csv"
    write(path, [item(), {**item(), "id": "two", "result": "2"}])
    report = experiment.evaluate(path, config())
    assert Decimal(report["zero_cost"]["net_units"]) == Decimal("0.2")
    assert Decimal(report["stress_cost"]["net_units"]) == Decimal("0.18")
    assert report["selected"] == 2
    assert report["profitability_claim"] is False


def test_void_and_pending_candidates_consume_cap_without_hindsight(tmp_path):
    path = tmp_path / "prices.csv"
    rows = [{**item(), "id": "0", "void_reason": "postponed"},
            {**item(), "id": "1", "result": ""}, {**item(), "id": "2"}]
    write(path, rows)
    c = config()
    c["maximum_candidates_per_kickoff_day"] = 2
    report = experiment.evaluate(path, c)
    assert report["voids"] == report["pending"] == 1
    assert report["zero_cost"]["bets"] == 0
    assert report["exclusions"]["daily_cap"] == 1


def test_earlier_soft_quotes_and_duplicate_matches_rejected(tmp_path):
    path = tmp_path / "prices.csv"
    write(path, [{**item(), "kickoff_utc": "2026-09-20T12:00:00+00:00"}])
    assert experiment.evaluate(path, config())["selected"] == 0
    write(path, [item(), item()])
    with pytest.raises(ValueError, match="duplicate"):
        experiment.evaluate(path, config())
