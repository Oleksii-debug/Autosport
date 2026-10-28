from __future__ import annotations

import csv
import io
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import audit_account_c_outcome_odds as research  # noqa: E402


def fixtures():
    return [
        {"match_id": "warm", "date": "2021-12-01 10:00:00", "joueur1_id": "A",
         "joueur2_id": "B", "vainqueur_id": "A", "score": "6-4 6-3",
         "cote1_ouverture": "1.8", "cote2_ouverture": "2.1"},
        {"match_id": "first", "date": "2022-01-01 10:00:00", "joueur1_id": "A",
         "joueur2_id": "B", "vainqueur_id": "A", "score": "6-4 6-3",
         "cote1_ouverture": "2", "cote2_ouverture": "2"},
        {"match_id": "second", "date": "2022-01-01 18:00:00", "joueur1_id": "A",
         "joueur2_id": "C", "vainqueur_id": "C", "score": "4-6 3-6",
         "cote1_ouverture": "1.5", "cote2_ouverture": "3"},
        {"match_id": "retired", "date": "2022-01-02 10:00:00", "joueur1_id": "A",
         "joueur2_id": "B", "vainqueur_id": "A", "score": "RET",
         "cote1_ouverture": "1.8", "cote2_ouverture": "2"},
        {"match_id": "missing", "date": "2022-01-02 15:00:00", "joueur1_id": "A",
         "joueur2_id": "B", "vainqueur_id": "B", "score": "4-6 3-6",
         "cote1_ouverture": "", "cote2_ouverture": "2"},
    ]


def encode(rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def test_exact_payouts_fee_exclusions_and_no_bet_denominator():
    result = research.audit([("fixture.csv", encode(fixtures()))])
    assert result["counts"]["evaluation_eligible"] == 2
    assert result["counts"]["evaluation_exclusions"] == {
        "incomplete_or_ambiguous_score": 1, "missing_or_invalid_opening_odds": 1}
    market = result["arms"]["market_favorite"]
    assert market["bets"] == 2 and market["wins"] == 1 and market["losses"] == 1
    assert market["gross_profit"] == "0" and market["fees"] == "0.02"
    assert market["net_profit"] == "-0.02" and market["roi"] == "-0.01"
    assert result["arms"]["no_bet"]["roi"] is None
    assert result["truth"]["profitability_claim"] is False
    assert result["truth"]["forward_evidence_eligible"] is False


def test_same_day_outcome_cannot_change_another_decision(monkeypatch):
    observed = []
    original = research.record
    def spy(arm, selected, row):
        observed.append((row["id"], selected))
        return original(arm, selected, row)
    monkeypatch.setattr(research, "record", spy)
    rows = fixtures()
    research.audit([("fixture.csv", encode(rows))])
    before = [choice for key, choice in observed if key == "second"]
    observed.clear()
    rows[1]["vainqueur_id"] = "B"
    research.audit([("fixture.csv", encode(rows))])
    after = [choice for key, choice in observed if key == "second"]
    assert before == after


def test_input_order_does_not_change_outcome():
    rows = fixtures()
    before = research.audit([("fixture.csv", encode(rows))])
    after = research.audit([("fixture.csv", encode(list(reversed(rows))))])
    assert before["counts"] == after["counts"]
    assert before["arms"] == after["arms"]


def test_elo_challenger_books_assumed_costs_for_its_own_decisions():
    rows = fixtures()
    rows[1]["cote1_ouverture"] = "3"
    rows[1]["cote2_ouverture"] = "1.3"
    result = research.audit([("fixture.csv", encode(rows))])
    challenger = result["arms"]["elo_threshold"]
    assert challenger["bets"] == 2 and challenger["wins"] == 2
    assert challenger["turnover"] == "2"
    assert challenger["gross_profit"] == "4"
    assert challenger["fees"] == "0.08"
    assert challenger["net_profit"] == "3.92"
    assert challenger["roi"] == "1.96"
    assert result["truth"]["actual_price_fills_verified"] is False


def test_duplicate_match_or_invalid_winner_fails_closed():
    rows = fixtures()
    with pytest.raises(ValueError, match="duplicate"):
        research.audit([("duplicate.csv", encode(rows + [rows[0]]))])
    rows[1]["vainqueur_id"] = "unknown"
    with pytest.raises(ValueError, match="winner"):
        research.audit([("invalid.csv", encode(rows))])


def test_nonfinite_odds_are_excluded_not_claimed_as_profit():
    rows = fixtures()
    rows[1]["cote1_ouverture"] = "NaN"
    result = research.audit([("fixture.csv", encode(rows))])
    assert result["counts"]["evaluation_eligible"] == 1
    assert result["counts"]["evaluation_exclusions"]["missing_or_invalid_opening_odds"] == 2


def test_custom_date_window_does_not_admit_later_data():
    rows = fixtures()
    later = dict(rows[1], match_id="later", date="2023-01-01 12:00:00")
    result = research.audit([("fixture.csv", encode(rows + [later]))],
                            warmup_start=date(2021, 1, 1),
                            evaluation_start=date(2022, 1, 1),
                            evaluation_end=date(2023, 1, 1))
    assert result["counts"]["all_input"] == 6
    assert result["counts"]["evaluation_eligible"] == 2
    with pytest.raises(ValueError, match="dates"):
        research.audit([("fixture.csv", encode(rows))],
                       warmup_start=date(2022, 1, 1), evaluation_start=date(2022, 1, 1))
