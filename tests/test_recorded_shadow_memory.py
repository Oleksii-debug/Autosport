import csv
import importlib.util
from pathlib import Path
import sys


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("shadow_experiment", SCRIPTS / "evaluate_recorded_shadow_memory.py")
experiment = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SCRIPTS))
try:
    spec.loader.exec_module(experiment)
finally:
    sys.path.pop(0)


def source(path, last_result):
    rows = []
    for index, (day, result) in enumerate(((18, "1"), (19, "1"), (21, "1"), (22, last_result), (23, "2"))):
        rows.append(dict(id=str(index), kickoff_utc=f"2026-09-{day}T12:00:00+00:00",
                         result=result, void_reason="", pin_open_home="2", pin_open_draw="3", pin_open_away="4",
                         prediction_pick="1", prediction_odds_taken="2"))
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


def test_later_match_result_cannot_change_same_day_or_next_day_choices(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    source(a, "1")
    source(b, "2")
    config = dict(split_date="2026-09-21", window=3, minimum_observations=1,
                  minimum_mean_return="0.02", feedback_delay_hours=48, cost_bps_per_stake=100)
    left, right = experiment.evaluate(a, config), experiment.evaluate(b, config)
    assert left["decisions"] == right["decisions"]
    assert left["variants"] != right["variants"]
    assert left["quote_timing_verified"] is False
    assert left["profitability_claim"] is False
    assert left["pending_feedback_count"] > 0
