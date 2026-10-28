import csv
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import evaluate_independent_team_agent as experiment


def row(date, result="H", home="A", away="B"):
    return {"Div": "E0", "Date": date, "Time": "15:00", "HomeTeam": home, "AwayTeam": away,
            "PSH": "2.5", "PSD": "5", "PSA": "2.5", "FTR": result}


def write(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def config():
    return {"split_date": "2023-08-01", "outcome_lag_hours": 48, "cost_bps_per_stake": 100,
            "minimum_net_return": "0.05", "maximum_odds": "10", "model": {"learning_rate": "0.1",
            "regularization": "0.01", "maximum_weight": "0.5", "minimum_team_observations": 2}}


def test_future_result_cannot_rewrite_prior_decisions_or_training(tmp_path):
    train, a, b = (tmp_path / name for name in ("train.csv", "a.csv", "b.csv"))
    write(train, [row("01/01/2023"), row("04/01/2023"), row("07/01/2023")])
    write(a, [row("01/08/2023"), row("02/08/2023"), row("04/08/2023")])
    write(b, [row("01/08/2023"), row("02/08/2023"), row("04/08/2023", "A")])
    first = experiment.evaluate({"2223": train, "2324": a}, config(), ("2324",))
    second = experiment.evaluate({"2223": train, "2324": b}, config(), ("2324",))
    assert first["decision_journal_sha256"] == second["decision_journal_sha256"]
    assert first["training_matches"] == 3
    assert first["evaluation"]["2324"]["variants"]["frozen_team"]["net_units"] != second["evaluation"]["2324"]["variants"]["frozen_team"]["net_units"]
    assert first["profitability_claim"] is first["promotion_authorized"] is first["real_money_execution"] is False


def test_duplicate_event_cannot_be_duplicated_across_seasons(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    write(a, [row("01/01/2023")])
    write(b, [row("01/01/2023")])
    with pytest.raises(ValueError, match="duplicate"):
        experiment.load_seasons({"2223": a, "2324": b})


def test_clock_rules_reject_ambiguous_hours_and_preserve_utc_offsets():
    assert experiment.assumed_london_kickoff("01/08/2023", "15:00") == datetime(2023, 8, 1, 14, tzinfo=timezone.utc)
    assert experiment.assumed_london_kickoff("01/01/2023", "15:00") == datetime(2023, 1, 1, 15, tzinfo=timezone.utc)
    for day in ("26/03/2023", "29/10/2023"):
        with pytest.raises(ValueError, match="clock-change"):
            experiment.assumed_london_kickoff(day, "01:30")


def test_missing_quotes_are_explicit_exclusions(tmp_path):
    a = tmp_path / "a.csv"
    write(a, [row("01/01/2023"), {**row("02/01/2023"), "PSH": ""}])
    rows, metadata = experiment.load_seasons({"2223": a})
    assert len(rows) == 1
    assert metadata["exclusions"] == {"2223": 1}
    assert metadata["source_rows_by_season"] == {"2223": 2}
    assert metadata["eligible_rows_by_season"] == {"2223": 1}


def test_fixed_bookmaker_is_explicit_and_never_falls_back(tmp_path):
    path = tmp_path / "prices.csv"
    values = [{**row("01/01/2023"), "PSH": "", "B365H": "2", "B365D": "4", "B365A": "5"}]
    write(path, values)
    pinnacle, original = experiment.load_seasons({"2223": path}, "pinnacle")
    bet365, alternative = experiment.load_seasons({"2223": path}, "bet365")
    assert not pinnacle and original["exclusions"] == {"2223": 1}
    assert len(bet365) == 1 and str(bet365[0]["odds"]["1"]) == "2"
    assert alternative["fixed_price_source"] == "bet365"
    assert alternative["price_columns"] == ["B365H", "B365D", "B365A"]
    with pytest.raises(ValueError, match="source"):
        experiment.load_seasons({"2223": path}, "unknown")


def test_protocol_hash_binds_fixed_bookmaker(tmp_path):
    train, evaluation = tmp_path / "train.csv", tmp_path / "evaluation.csv"
    extra = {"B365H": "2.5", "B365D": "5", "B365A": "2.5"}
    write(train, [{**row(d), **extra} for d in ("01/01/2023", "04/01/2023", "07/01/2023")])
    write(evaluation, [{**row(d), **extra} for d in ("01/08/2023", "02/08/2023", "04/08/2023")])
    files = {"2223": train, "2324": evaluation}
    pinnacle = experiment.evaluate(files, config(), ("2324",), "pinnacle")
    alternative = experiment.evaluate(files, config(), ("2324",), "bet365")
    assert pinnacle["config_sha256"] == alternative["config_sha256"]
    assert pinnacle["protocol_sha256"] != alternative["protocol_sha256"]
