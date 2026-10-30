from copy import deepcopy

import pytest

from causal_h2h_longitudinal import compare
from test_causal_h2h_score import observation
from test_delayed_portfolio_replay import followup, later_result
from test_quote_delay_audit import stamp


def pair(previous, current):
    return (*followup(previous), *followup(current, requested=150, received=160))


def test_fixed_cohort_and_previous_as_of_score_are_preserved(tmp_path):
    observation(tmp_path, ids=("a", "b", "c"))
    before = later_result("a")
    report = compare(tmp_path, *pair([before], [before, later_result("b", "away")]))
    assert report["reconciliation_counts"] == {
        "unchanged_eligible": 1, "newly_eligible": 1,
        "previously_eligible_missing": 0, "quarantined": 0, "unresolved": 1}
    assert report["previous_report"]["counts"]["eligible"] == 1
    assert report["previous_report"]["market"]["brier"] == pytest.approx(.0625)
    assert report["current_eligible"] == 2
    assert report["current_market"]["brier"] == pytest.approx(.3125)
    assert report["current_neutral"]["brier"] == pytest.approx(.25)
    assert report["real_money_execution"] is report["model_training_authorized"] is False


@pytest.mark.parametrize("change", ["winner", "same_winner_score", "participant", "league", "status", "last_live"])
def test_changed_or_invalid_previous_result_is_quarantined(tmp_path, change):
    observation(tmp_path)
    old = later_result("a")
    new = deepcopy(old)
    if change == "winner":
        new = later_result("a", "away")
    elif change == "same_winner_score":
        new["periods"][0] = {"score": "11-3"}
        new["final_score"] = "11-3,11-2,11-2"
    elif change == "participant":
        new["home"] = "another player"
    elif change == "league":
        new["league"] = "another league"
    elif change == "status":
        new["status"] = "live"
    else:
        new["finished_at"] = stamp(61)
    report = compare(tmp_path, *pair([old], [new]))
    assert report["reconciliation_counts"]["quarantined"] == 1
    assert report["current_eligible"] == 0
    assert report["current_market"] is report["current_neutral"] is None
    assert report["previous_report"]["market"]["brier"] == pytest.approx(.0625)


def test_disappearance_remains_unknown_and_does_not_erase_past(tmp_path):
    observation(tmp_path)
    report = compare(tmp_path, *pair([later_result("a")], []))
    assert report["reconciliation_counts"]["previously_eligible_missing"] == 1
    assert report["previous_report"]["counts"]["eligible"] == 1
    assert report["current_eligible"] == 0
    assert report["current_market"] is None


def test_previously_finished_but_invalid_then_repaired_is_quarantined(tmp_path):
    observation(tmp_path)
    old = later_result("a")
    old["final_score"] = None
    report = compare(tmp_path, *pair([old], [later_result("a")]))
    assert report["previous_report"]["counts"]["eligible"] == 0
    assert report["current_scorer_counts_before_reconciliation"]["eligible"] == 1
    assert report["reconciliation_counts"]["quarantined"] == 1
    assert report["current_eligible"] == 0


def test_unfinished_result_can_become_newly_eligible_at_later_receipt(tmp_path):
    observation(tmp_path)
    old = later_result("a")
    old["status"] = "live"
    report = compare(tmp_path, *pair([old], [later_result("a")]))
    assert report["reconciliation_counts"]["newly_eligible"] == 1
    assert report["previous_report"]["market"] is None
    assert report["current_eligible"] == 1


def test_update_timestamp_alone_is_not_sporting_correction(tmp_path):
    observation(tmp_path)
    old = later_result("a")
    new = later_result("a", updated=120)
    report = compare(tmp_path, *pair([old], [new]))
    assert report["reconciliation_counts"]["unchanged_eligible"] == 1


def test_chronology_and_tampered_body_are_rejected(tmp_path):
    observation(tmp_path)
    previous_raw, previous_receipt, current_raw, current_receipt = pair([], [])
    too_early = deepcopy(current_receipt)
    too_early["requested_at"] = previous_receipt["received_at"]
    with pytest.raises(ValueError, match="strictly after"):
        compare(tmp_path, previous_raw, previous_receipt, current_raw, too_early)
    with pytest.raises(ValueError, match="receipt mismatch"):
        compare(tmp_path, previous_raw, previous_receipt, current_raw + b" ", current_receipt)


def test_duplicate_results_are_not_silently_merged(tmp_path):
    observation(tmp_path)
    a = later_result("a")
    with pytest.raises(ValueError, match="duplicate fixture ID"):
        compare(tmp_path, *pair([], [a, deepcopy(a)]))
