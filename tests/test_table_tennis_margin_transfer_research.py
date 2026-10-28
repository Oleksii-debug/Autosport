import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_table_tennis_margin_transfer import audit


def test_transfer_keeps_all_evaluation_matches_and_refuses_profit_claim():
    result = audit()
    assert result["evaluation_count"] == 819
    assert result["clipped_margin_count_total"] == 3
    assert result["day_count"] == 50
    assert result["posthoc_transfer"] is True
    assert result["odds_present"] is False
    assert result["profitability_claim"] is False
    assert result["promotion_authorized"] is False
