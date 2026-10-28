import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_setka_result_history import audit, load_history


def test_unresolved_self_match_is_excluded_and_recorded(tmp_path):
    path = tmp_path / "history.csv"
    path.write_text(
        "X,Date,Player1,Player2,Sets_P1,Sets_P2,HomeWinner\n"
        "1,6/20/2022,A,B,3,1,1\n"
        "2,6/21/2022,A,A,1,3,0\n"
        "3,7/2/2022,A,B,3,2,1\n", encoding="utf-8")
    rows, excluded = load_history(path)
    assert len(rows) == 2 and excluded == ["2"]
    result = audit(path)
    assert result["training_count"] == result["evaluation_count"] == 1
    assert result["excluded_unresolved_match_ids"] == ["2"]
    assert result["source_url"] is None and result["known_source_hash_match"] is False
    assert result["source_rights"] == "UNKNOWN" and result["profitability_claim"] is False


def test_duplicate_or_contradictory_result_fails_closed(tmp_path):
    path = tmp_path / "history.csv"
    header = "X,Date,Player1,Player2,Sets_P1,Sets_P2,HomeWinner\n"
    path.write_text(header + "1,6/20/2022,A,B,3,1,1\n" * 2, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_history(path)
    path.write_text(header + "1,6/20/2022,A,B,3,1,0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="disagree"):
        load_history(path)
