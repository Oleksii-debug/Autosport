import csv
import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("recorded_shadow_agent", SCRIPTS / "evaluate_recorded_shadow_agent.py")
agent = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SCRIPTS))
try:
    spec.loader.exec_module(agent)
finally:
    sys.path.pop(0)


def config():
    return {"split_date": "2026-09-21", "outcome_lag_hours": 48,
            "selector": {"window": 2, "minimum_observations": 2,
                         "minimum_net_return": "0", "cost_bps_per_stake": 100}}


def write_source(path, changes=None):
    values = [
        ("train-a", "2026-09-17T12:00:00+00:00", "1"),
        ("train-b", "2026-09-18T12:00:00+00:00", "1"),
        ("same-a", "2026-09-21T12:00:00+00:00", "2"),
        ("same-b", "2026-09-21T12:00:00+00:00", "2"),
        ("boundary", "2026-09-23T12:00:00+00:00", "2"),
        ("released", "2026-09-24T12:00:00+00:00", "2"),
    ]
    rows = [{"id": identity, "kickoff_utc": at, "result": (changes or {}).get(identity, result),
             "void_reason": "", "pin_open_home": "2", "pin_open_draw": "3", "pin_open_away": "4",
             "prediction_pick": "1", "prediction_odds_taken": "2"}
            for identity, at, result in values]
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


def test_same_time_and_exact_lag_boundary_results_cannot_change_decisions(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    write_source(a)
    write_source(b, {"same-a": "1", "same-b": "1"})
    ra, rb = agent.evaluate(a, config()), agent.evaluate(b, config())
    assert ra["decisions"][:3] == rb["decisions"][:3]
    assert ra["decisions"][3]["selection"] == "HIGH_ODDS"
    assert rb["decisions"][3]["selection"] == "LOW_ODDS"
    assert ra["decisions"][2]["released_shadow_matches"] == 2
    assert ra["decisions"][3]["released_shadow_matches"] == 4


def test_current_and_future_results_cannot_change_current_action(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    write_source(a)
    write_source(b, {"released": "1"})
    ra, rb = agent.evaluate(a, config()), agent.evaluate(b, config())
    assert ra["decisions"] == rb["decisions"]
    assert ra["validation"]["chosen_actions"] != rb["validation"]["chosen_actions"]


def test_no_bet_accounting_does_not_add_profitable_shadow_returns(tmp_path):
    path = tmp_path / "rows.csv"
    write_source(path)
    cfg = config()
    cfg["selector"].update(window=100, minimum_observations=100)
    report = agent.evaluate(path, cfg)
    assert report["validation"]["wait_count"] == report["validation_matches"]
    assert report["validation"]["chosen_actions"]["net_units"] == "0"
    assert report["validation"]["chosen_actions"]["bets"] == 0
    assert all(report[key] is False for key in (
        "forward_evidence_eligible", "promotion_authorized", "profitability_claim",
        "real_money_execution", "quote_timing_verified", "outcome_availability_verified"))


def test_repeat_replay_is_deterministic(tmp_path):
    path = tmp_path / "rows.csv"
    write_source(path)
    assert agent.evaluate(path, config()) == agent.evaluate(path, config())


@pytest.mark.parametrize("lag", [True, 0, 23, 721])
def test_invalid_release_assumptions_rejected(tmp_path, lag):
    cfg = config()
    cfg["outcome_lag_hours"] = lag
    with pytest.raises(ValueError, match="lag"):
        agent.evaluate(tmp_path / "not-read.csv", cfg)
