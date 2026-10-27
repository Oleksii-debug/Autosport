import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("recorded_team_agent", SCRIPTS / "evaluate_recorded_team_agent.py")
agent = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SCRIPTS))
try:
    spec.loader.exec_module(agent)
finally:
    sys.path.pop(0)


def config():
    return {"split_date": "2026-01-11", "outcome_lag_hours": 48,
            "cost_bps_per_stake": 100, "minimum_net_return": "0.05", "maximum_odds": "10",
            "model": {"learning_rate": "0.1", "regularization": "0.01", "maximum_weight": "0.5",
                      "minimum_team_observations": 2}}


def source(path, changes=None, missing_team=False):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for day in range(18):
        rows.append({"id": str(day), "kickoff_utc": (start + timedelta(days=day)).isoformat(),
                     "country": "test-country", "home": "" if missing_team else "a", "away": "b",
                     "result": (changes or {}).get(day, "1"), "void_reason": "",
                     "pin_open_home": "2.5", "pin_open_draw": "5", "pin_open_away": "2.5",
                     "prediction_pick": "1", "prediction_odds_taken": "2.5"})
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


def test_current_future_and_exact_lag_boundary_cannot_change_prediction(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    source(a)
    source(b, {10: "2", 11: "2", 12: "2", 13: "2", 14: "2", 15: "2", 16: "2", 17: "2"})
    ra, rb = agent.evaluate(a, config()), agent.evaluate(b, config())
    assert ra["training_checkpoint"] == rb["training_checkpoint"]
    # First changed result at Jan 11 is released AFTER Jan 13, not on that boundary.
    assert ra["decisions"][:3] == rb["decisions"][:3]
    assert ra["decisions"][3]["variants"]["online_team"] != rb["decisions"][3]["variants"]["online_team"]
    assert Decimal(rb["decisions"][3]["variants"]["online_team"]["probabilities"][0]) < Decimal(
        ra["decisions"][3]["variants"]["online_team"]["probabilities"][0])
    assert [d["variants"]["frozen_team"] for d in ra["decisions"]] == [d["variants"]["frozen_team"] for d in rb["decisions"]]
    assert ra["validation"]["frozen_team"]["net_units"] != rb["validation"]["frozen_team"]["net_units"]


def test_costs_account_only_chosen_bets_and_no_promotion(tmp_path):
    path = tmp_path / "source.csv"
    source(path)
    cfg = config()
    positive = agent.evaluate(path, cfg)
    cfg["cost_bps_per_stake"] = 200
    expensive = agent.evaluate(path, cfg)
    first, second = positive["validation"]["frozen_team"], expensive["validation"]["frozen_team"]
    assert first["bets"] == second["bets"] > 0
    assert Decimal(first["net_units"]) - Decimal(second["net_units"]) == Decimal("0.01") * first["bets"]
    assert positive["validation"]["market"]["bets"] == 0
    assert positive["candidate_assessments"]["frozen_team"]["research_verdict"] == "REQUIRES_INDEPENDENT_FORWARD_EVIDENCE"
    assert positive["candidate_assessments"]["frozen_team"]["promotion_authorized"] is False
    assert all(positive[key] is False for key in ("profitability_claim", "promotion_authorized", "forward_evidence_eligible", "real_money_execution"))


def test_sparse_team_evidence_forces_wait_even_with_available_prices(tmp_path):
    path = tmp_path / "source.csv"
    source(path)
    cfg = config()
    cfg["model"]["minimum_team_observations"] = 100
    report = agent.evaluate(path, cfg)
    assert report["validation"]["online_team"]["bets"] == 0
    assert report["validation"]["online_team"]["wait_count"] == report["validation_matches"]
    assert report["candidate_assessments"]["online_team"]["research_verdict"] == "REJECTED_ON_THIS_SAMPLE"


def test_missing_context_cannot_become_one_shared_anonymous_team(tmp_path):
    path = tmp_path / "source.csv"
    source(path, missing_team=True)
    with pytest.raises(ValueError, match="cohorts"):
        agent.evaluate(path, config())


def test_config_validation_precedes_dataset_read(tmp_path):
    cfg = config()
    cfg["cost_bps_per_stake"] = True
    with pytest.raises(ValueError, match="cost"):
        agent.evaluate(tmp_path / "absent", cfg)
