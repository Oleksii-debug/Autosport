import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_table_tennis_day_batch_elo import audit, fit_k_before_cutoff, predict_day_batched


def test_same_day_outcome_cannot_influence_same_day_prediction():
    day1, day2 = date(2026, 8, 1), date(2026, 8, 2)
    rows = [("a", day1, "one", "two", 1), ("b", day1, "one", "three", 0),
            ("c", day2, "one", "two", 1)]
    changed = [("a", day1, "one", "two", 0), *rows[1:]]
    first, second = predict_day_batched(rows), predict_day_batched(changed)
    assert first["a"] == second["a"] == 0.5
    assert first["b"] == second["b"] == 0.5
    assert first["c"] != second["c"]
    assert first == predict_day_batched(list(reversed(rows)))


def test_duplicate_and_self_matches_are_rejected():
    row = ("x", date(2026, 8, 1), "one", "two", 1)
    with pytest.raises(ValueError):
        predict_day_batched([row, row])
    with pytest.raises(ValueError):
        predict_day_batched([("x", date(2026, 8, 1), "one", "one", 1)])


def test_selecting_learning_rate_cannot_see_later_results():
    rows = [("a", date(2026, 8, 1), "one", "two", 1),
            ("b", date(2026, 8, 2), "one", "two", 1),
            ("c", date(2026, 9, 1), "one", "two", 1)]
    changed = [*rows[:-1], ("c", date(2026, 9, 1), "one", "two", 0)]
    assert fit_k_before_cutoff(rows) == fit_k_before_cutoff(changed)


def test_audit_does_not_claim_profit_or_complete_input():
    result = audit()
    assert result["evaluation_count"] == 819
    assert result["sample_is_incomplete"] is True
    assert result["odds_present"] is False
    assert result["profitability_claim"] is False
