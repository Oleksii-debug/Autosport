from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import audit_account_c_trained_calibration as trained  # noqa: E402


def fixture():
    return [
        {"match_id": "warm", "start_time_utc": "2025-01-01T10:00:00Z",
         "player1_id": "A", "player2_id": "B", "status": "finished",
         "winner": "1", "score": "6-3 6-2", "best_close_odds1": "1.8", "best_close_odds2": "2.3"},
        {"match_id": "eval1", "start_time_utc": "2025-07-01T10:00:00Z",
         "player1_id": "A", "player2_id": "B", "status": "finished",
         "winner": "1", "score": "6-4 6-4", "best_close_odds1": "2", "best_close_odds2": "2"},
        {"match_id": "eval2", "start_time_utc": "2025-07-01T18:00:00Z",
         "player1_id": "A", "player2_id": "B", "status": "finished",
         "winner": "2", "score": "4-6 4-6", "best_close_odds1": "2", "best_close_odds2": "2"},
    ]


def encode(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


def test_training_never_uses_evaluation_or_same_day_outcomes(monkeypatch):
    choices = []
    original = trained.record
    def spy(arm, selected, row):
        choices.append((row["id"], selected))
        return original(arm, selected, row)
    monkeypatch.setattr(trained, "record", spy)
    rows = fixture()
    before = trained.evaluate(encode(rows))
    eval2_before = [choice for identity, choice in choices if identity == "eval2"]
    choices.clear()
    rows[1]["winner"] = "2"
    after = trained.evaluate(encode(rows))
    eval2_after = [choice for identity, choice in choices if identity == "eval2"]
    assert before["training"] == after["training"]
    assert eval2_before == eval2_after
    assert before["truth"]["untouched_holdout"] is False
    assert before["truth"]["promotion_authorized"] is False


def test_input_order_invariance_and_common_denominator():
    rows = fixture()
    a = trained.evaluate(encode(rows))
    b = trained.evaluate(encode(list(reversed(rows))))
    assert a["training"] == b["training"]
    assert a["arms"] == b["arms"]
    assert a["evaluation_count"] == 2
    for arm in a["arms"].values():
        assert arm["bets"] + arm["no_bets"] == 2
    assert a["arms"]["no_bet"]["roi"] is None


def test_unverified_underround_is_a_counted_abstention_not_a_win(monkeypatch):
    rows = fixture()
    for row in rows[1:]:
        row["best_close_odds1"] = "3"
        row["best_close_odds2"] = "3"
    monkeypatch.setattr(trained, "fit", lambda examples: (2.0, 0.0))
    report = trained.evaluate(encode(rows))
    assert report["arms"]["trained_calibration"]["bets"] == 2
    assert report["arms"]["trained_price_guarded"]["bets"] == 0
    assert report["arms"]["trained_price_guarded"]["no_bets"] == 2
    assert report["price_diagnostics"]["underround_selected_by_original"] == 2
    assert report["price_diagnostics"]["simultaneous_quotes_verified"] is False
