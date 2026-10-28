import csv
import hashlib
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from evaluate_recorded_live_flags import evaluate, read_candidate


def row(identity, result="1", settled="1"):
    preimage = f"{identity}|home|2.200|book|2.000|2026-10-01T00:00:00Z|nonce"
    return {"id": str(identity), "value_origin": "live", "value_sealed_before_kickoff": "1",
            "value_recorded_at_utc": "2026-10-01T00:00:00+00:00", "kickoff_utc": "2026-10-02T00:00:00+00:00",
            "value_selection": "1", "value_bookmaker": "book", "value_preimage": preimage,
            "value_commitment": hashlib.sha256(preimage.encode()).hexdigest(), "value_odds_taken": "2.20",
            "settled": settled, "result": result, "value_won": "1" if result == "1" else "0",
            "value_profit_units": "1.20" if result == "1" else "-1", "value_realizable_odds": "2.00"}


def fixture(tmp_path, rows, name):
    path = tmp_path / name
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_frozen_decisions_ignore_outcomes_and_pending_consumes_limit(tmp_path):
    rows = [row(i) for i in range(6)]
    rows[0] = row(0, "", "0")
    first = evaluate(fixture(tmp_path, rows, "first.csv"))
    current = first["cohorts"]["current_rule"]
    assert current["candidates"] == 5
    assert current["states"] == {"PENDING": 1, "SETTLED": 4}
    assert current["cost_scenarios"]["100"]["net_units"] == "4.760"
    assert first["decisions"][-1]["current_rule_selected"] is False
    rows[0] = row(0, "2", "1")
    second = evaluate(fixture(tmp_path, rows, "second.csv"))
    assert [x["current_rule_selected"] for x in first["decisions"]] == [x["current_rule_selected"] for x in second["decisions"]]


def test_source_commitment_and_settlement_conflict_rejected(tmp_path):
    broken = row(1)
    broken["value_odds_taken"] = "2.30"
    with pytest.raises(ValueError, match="preimage_field_mismatch"):
        read_candidate(broken)
    conflict = row(2)
    conflict["value_won"] = "0"
    report = evaluate(fixture(tmp_path, [conflict], "conflict.csv"))
    assert report["cohorts"]["provider_flags"]["states"] == {"CONFLICT": 1}
    assert report["cohorts"]["provider_flags"]["cost_scenarios"]["0"]["bets"] == 0


def test_requote_comparison_is_a_future_conditioned_subset(tmp_path):
    rows = [row(1), row(2, "2")]
    rows[1]["value_realizable_odds"] = ""
    report = evaluate(fixture(tmp_path, rows, "quotes.csv"))
    pair = report["cohorts"]["conditional_later_requote"]
    assert pair["matched_settled"] == 1
    assert pair["initial_prices"]["100"]["net_units"] == "1.190"
    assert Decimal(pair["later_prices"]["100"]["net_units"]) == Decimal("0.99")
    assert report["requote_subgroup_future_conditioned"] is True
    assert report["quote_acceptance_verified"] is False


def test_late_or_changed_commitment_cannot_enter_candidate():
    late = row(1)
    late["kickoff_utc"] = "2026-10-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="late"):
        read_candidate(late)
    altered = row(2)
    altered["value_preimage"] += "x"
    with pytest.raises(ValueError, match="commitment"):
        read_candidate(altered)
