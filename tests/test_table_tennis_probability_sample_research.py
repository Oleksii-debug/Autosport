"""Source provenance and chronology checks, without a profitability claim."""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_table_tennis_probability_sample import audit, bootstrap_day_difference, fit_shrink, load_rows


def test_later_labels_do_not_change_train_only_shrink():
    rows = [("a", date(2026, 8, 1), "test", .7, 1),
            ("b", date(2026, 8, 2), "test", .6, 0),
            ("c", date(2026, 9, 2), "test", .9, 1)]
    changed = [*rows[:-1], ("c", date(2026, 9, 2), "test", .9, 0)]
    assert fit_shrink(rows) == fit_shrink(changed)


def test_sample_audit_disclaims_financial_evidence_and_has_fixed_identity():
    result = audit()
    assert result["source_sha256"] == "bbb2ab531f269a1eafff89e240086a4d17999114b582825ba4338986c6770fde"
    assert result["training_count"] + result["evaluation_count"] == 2000
    assert result["odds_present"] is False
    assert result["profitability_claim"] is False
    assert result["evaluation_is_untouched_holdout"] is False


def test_identical_forecasts_have_zero_bootstrap_difference():
    rows = [("a", date(2026, 9, 1), "test", .7, 1),
            ("b", date(2026, 9, 2), "test", .6, 0)]
    result = bootstrap_day_difference(rows, 1.0, draws=10)
    assert result["days"] == 2
    assert result["lower_2_5_percent"] == result["upper_97_5_percent"] == 0


@pytest.mark.parametrize("probability,winner,sets", [("nan", "A", "3,1"),
                                                   ("1.2", "A", "3,1"),
                                                   ("0.7", "B", "3,1")])
def test_invalid_probability_or_result_fails_closed(tmp_path, probability, winner, sets):
    path = tmp_path / "sample.csv"
    path.write_text("match_id,date,league,p_model,winner,sets_a,sets_b\n"
                    f"m,2026-08-01,test,{probability},{winner},{sets}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_rows(path)
