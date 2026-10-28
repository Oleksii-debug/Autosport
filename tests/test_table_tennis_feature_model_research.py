import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_table_tennis_feature_model import audit, fit, sigmoid


def test_later_outcome_and_features_cannot_influence_fit():
    rows = [("a", date(2026, 8, 1), (1., .2, .1), 1, .6),
            ("b", date(2026, 8, 2), (1., -.3, -.1), 0, .4),
            ("c", date(2026, 9, 2), (1., .5, .3), 1, .7)]
    changed = [*rows[:-1], ("c", date(2026, 9, 2), (1., -5., -1.), 0, .1)]
    assert fit(rows) == fit(changed)
    assert 0 < sigmoid(-1000) < 1 and 0 < sigmoid(1000) < 1


def test_feature_audit_is_not_a_profit_or_promotion_claim():
    result = audit()
    assert result["training_count"] == 1181 and result["evaluation_count"] == 819
    assert result["odds_present"] is False
    assert result["evaluation_is_untouched_holdout"] is False
    assert result["profitability_claim"] is False and result["promotion_authorized"] is False
