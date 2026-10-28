import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_setka_margin_rating import day_predictions, fit_pre_cutoff


def test_set_margin_only_affects_later_days():
    day1, day2 = date(2022, 6, 20), date(2022, 6, 21)
    rows = [("a", day1, "one", "two", 1), ("b", day1, "one", "three", 0),
            ("c", day2, "one", "two", 1)]
    first = day_predictions(rows, {"a": 1, "b": 1, "c": 1}, k=20.0, weight=1.0)
    changed = day_predictions(rows, {"a": 3, "b": 1, "c": 1}, k=20.0, weight=1.0)
    assert first["a"] == changed["a"] == first["b"] == changed["b"] == 0.5
    assert first["c"] != changed["c"]


def test_parameter_selection_ignores_later_outcome_and_margin():
    rows = [("a", date(2022, 6, 20), "one", "two", 1),
            ("b", date(2022, 6, 21), "one", "two", 1),
            ("c", date(2022, 7, 2), "one", "two", 1)]
    changed = [*rows[:-1], ("c", date(2022, 7, 2), "one", "two", 0)]
    assert fit_pre_cutoff(rows, {"a": 1, "b": 2, "c": 1}) == fit_pre_cutoff(
        changed, {"a": 1, "b": 2, "c": 3})


def test_invalid_margin_is_rejected():
    rows = [("a", date(2022, 6, 20), "one", "two", 1)]
    with pytest.raises(ValueError, match="margin"):
        day_predictions(rows, {"a": 0}, k=20.0, weight=1.0)
