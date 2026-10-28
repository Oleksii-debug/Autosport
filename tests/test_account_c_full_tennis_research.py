from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import audit_account_c_full_tennis as full  # noqa: E402


def rows():
    return [
        {"match_id": "warm", "start_time_utc": "2025-01-01T12:00:00Z",
         "player1_id": "A", "player2_id": "B", "status": "finished",
         "winner": "1", "score": "6-2 6-2", "best_close_odds1": "2", "best_close_odds2": "2"},
        {"match_id": "eval", "start_time_utc": "2025-07-02T12:00:00Z",
         "player1_id": "A", "player2_id": "B", "status": "finished",
         "winner": "1", "score": "6-4 6-3", "best_close_odds1": "3", "best_close_odds2": "1.3"},
        {"match_id": "ret", "start_time_utc": "2025-07-03T12:00:00Z",
         "player1_id": "A", "player2_id": "B", "status": "retired",
         "winner": "1", "score": "6-2 2-0", "best_close_odds1": "2", "best_close_odds2": "2"},
    ]


def encode(data):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=data[0].keys())
    writer.writeheader()
    writer.writerows(data)
    return out.getvalue().encode()


def test_real_source_adapter_retains_retirements_as_exclusions():
    result = full.evaluate(encode(rows()))
    assert result["adapter_counts"]["source_rows"] == 3
    assert result["counts"]["warmup_usable"] == 1
    assert result["counts"]["evaluation_eligible"] == 1
    assert result["counts"]["evaluation_exclusions"] == {"incomplete_or_ambiguous_score": 1}
    assert result["arms"]["elo_history_guarded"]["net_profit"] == "1.96"
    assert result["truth"]["profitability_claim"] is False
    assert result["truth"]["actual_price_fills_verified"] is False


def test_missing_winner_counted_and_duplicates_rejected():
    source = rows()
    source.append(dict(source[1], match_id="unknown", winner=""))
    _, counts = full.adapt(encode(source))
    assert counts["adapter_exclusions"] == {"missing_or_unknown_winner": 1}
    source.append(source[0])
    with pytest.raises(ValueError, match="unique"):
        full.adapt(encode(source))


def test_unknown_status_never_becomes_a_completed_win():
    source = rows()
    source[1]["status"] = "unknown"
    adapted, _ = full.adapt(encode(source))
    assert "eval,2025-07-02T12:00:00Z,A,B,A,RET" in adapted.decode()
