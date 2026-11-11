from __future__ import annotations

import csv
import io
import sys
from datetime import date, timedelta
from pathlib import Path

SCRIPTS = str(Path(__file__).parents[1] / "scripts")
sys.path.insert(0, SCRIPTS)
from audit_account_c_league_calibration import audit, league_predictions  # noqa: E402
from audit_account_c_outcome_calibration import load_rows  # noqa: E402


def sample():
    data = []
    for day in range(34):
        for league, winner in (("A", "A"), ("B", "B")):
            data.append({"match_id": f"{league}-{day}", "date": str(date(2026, 1, 1)+timedelta(days=day)),
                         "league": league, "player_a": "a", "player_b": "b", "p_model": "0.8",
                         "winner": winner, "sets_a": "3" if winner == "A" else "1",
                         "sets_b": "1" if winner == "A" else "3"})
    return data


def encode(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


def test_learns_league_differences_and_retains_common_denominator():
    result = audit(encode(sample()))
    assert result["overall"]["count"] == 12
    assert result["warmup_count"] == 56
    h = result["learning_history"]
    a = [x for x in h if x["league"] == "A"][-1]
    b = [x for x in h if x["league"] == "B"][-1]
    assert a["league_slope"] > b["league_slope"]
    assert result["economic_metrics"]["net_profit"] is None
    assert result["truth"]["promotion_authorized"] is False


def test_current_and_future_outcomes_do_not_influence_current_prediction():
    rows = load_rows(encode(sample()))
    original, _ = league_predictions(rows)
    boundary = date(2026, 1, 30)
    changed = [{**r, "y": 1-r["y"]} if r["day"] >= boundary else r for r in rows]
    modified, _ = league_predictions(changed)
    for first, second in zip(original, modified, strict=True):
        if first["day"] <= boundary:
            assert first["predictions"] == second["predictions"]


def test_csv_order_does_not_change_metrics():
    left = audit(encode(sample()))
    right = audit(encode(list(reversed(sample()))))
    for key in ("overall", "by_week", "by_league", "learning_history"):
        assert left[key] == right[key]


def test_unseen_league_uses_global_prior():
    rows = load_rows(encode(sample()))
    _, history = league_predictions(rows)
    for entry in history:
        if entry["prior_rows"] == 0:
            assert entry["league_slope"] == entry["global_slope"]
