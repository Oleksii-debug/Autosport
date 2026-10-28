from __future__ import annotations

import csv
import io
import runpy
from datetime import date, timedelta
from pathlib import Path

import pytest


MODULE = runpy.run_path(str(Path(__file__).parents[1] / "scripts" /
                           "audit_account_c_outcome_calibration.py"))


def sample() -> list[dict]:
    return [{"match_id": f"match-{day}-{i}",
             "date": (date(2026, 1, 1)+timedelta(days=day)).isoformat(),
             "league": "fixture", "player_a": "a", "player_b": "b",
             "p_model": "0.8", "winner": "B", "sets_a": "1", "sets_b": "3"}
            for day in range(35) for i in range(2)]


def encode(rows: list[dict]) -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def test_same_day_and_future_results_cannot_change_current_predictions():
    rows = MODULE["load_rows"](encode(sample()))
    original, _ = MODULE["predict_by_date"](rows)
    boundary = date(2026, 1, 30)
    changed = [{**row, "y": 1-row["y"]} if row["day"] >= boundary else row
               for row in rows]
    changed_predictions, _ = MODULE["predict_by_date"](changed)
    for before, after in zip(original, changed_predictions, strict=True):
        if before["day"] <= boundary:
            assert before["predictions"] == after["predictions"]


def test_order_invariance_and_common_evaluation_denominator():
    left = MODULE["audit"](encode(sample()))
    right = MODULE["audit"](encode(list(reversed(sample()))))
    for key in ("overall", "by_day", "by_week", "by_league", "learning_history"):
        assert left[key] == right[key]
    assert left["warmup_count"] == 56
    assert left["overall"]["count"] == 14
    assert left["evaluation_first_date"] == "2026-01-29"
    assert sum(r["count"] for r in left["by_league"].values()) == 14
    assert set(left["overall"]["arms"]) == set(MODULE["ARMS"])


def test_learning_reduces_confidence_after_consistent_incorrect_predictions():
    report = MODULE["audit"](encode(sample()))
    assert report["learning_history"][-1]["slope"] == 0
    assert report["overall"]["arms"]["publisher"]["brier"] == pytest.approx(.64)
    assert report["overall"]["arms"]["fixed_half"]["brier"] == pytest.approx(.4225)
    assert report["overall"]["arms"]["past_dates_ridge"]["brier"] == .25
    assert report["economic_metrics"]["net_profit"] is None
    assert report["truth"]["promotion_authorized"] is False
    assert report["truth"]["profitability_claim"] is False
    assert report["truth"]["forward_evidence_eligible"] is False


@pytest.mark.parametrize("value", ["NaN", "inf", "-0.1", "0", "1", "1.1"])
def test_invalid_probability_is_not_silently_dropped(value):
    rows = sample()
    rows[0]["p_model"] = value
    with pytest.raises(ValueError, match="probability"):
        MODULE["audit"](encode(rows))


def test_duplicate_event_rejected():
    rows = sample()
    with pytest.raises(ValueError, match="duplicate match_id"):
        MODULE["audit"](encode(rows + [rows[0]]))


def test_result_disagreement_rejected():
    rows = sample()
    rows[0]["winner"] = "A"
    with pytest.raises(ValueError, match="inconsistent match result"):
        MODULE["audit"](encode(rows))


def test_missing_evaluation_period_is_not_reported_as_zero_error():
    with pytest.raises(ValueError, match="beyond fixed warmup"):
        MODULE["audit"](encode(sample()[:10]))


def test_adding_future_rows_does_not_rewrite_learning_history():
    rows = MODULE["load_rows"](encode(sample()))
    short, short_history = MODULE["predict_by_date"](rows[:40])
    long, long_history = MODULE["predict_by_date"](rows)
    assert long[:40] == short
    assert long_history[:len(short_history)] == short_history
