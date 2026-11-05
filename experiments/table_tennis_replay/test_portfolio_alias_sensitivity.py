from copy import deepcopy
from decimal import Decimal
import hashlib
import json

import pytest

from autosport.domain import TicketLeg
from autosport.paper import PaperBook
import portfolio_alias_sensitivity as module
from portfolio_alias_sensitivity import summarize_alias_sensitivity as summarize
from portfolio_alias_sensitivity_replay import audit
from portfolio_replay import replay
from test_portfolio_replay import fixture, frame


def leg(event, side="home", odds="2"):
    return TicketLeg(event, "match_winner", side, Decimal(odds), sport="table_tennis")


def tickets(*groups):
    book = PaperBook(Decimal("100"))
    opened = [book.open_ticket(tuple(group), Decimal("1"), reason="synthetic",
                               placed_at="2026-01-01T00:00:00+00:00") for group in groups]
    return book, opened


def test_two_ids_in_one_ticket_both_hypotheses_and_no_private_ids():
    book, opened = tickets((leg("private-a"), leg("private-b")))
    before = (book.balance, book.committed_stake, deepcopy(opened))
    report = summarize(opened)
    assert report["distinct_id_pairs"] == report["pairs_cooccurring_in_a_ticket"] == 1
    assert report["side_mappings"] == 2
    assert report["baseline_grid_states"] == 4
    assert list(map(Decimal, report["conditional_min_range_units"])) == [-1, -1]
    assert list(map(Decimal, report["conditional_max_range_units"])) == [-1, 3]
    assert report["mappings_with_lower_max"] == report["mappings_with_unwinnable_ticket"] == 1
    assert report["fraction_reference"] == "matched_each_mapping_within_1e-24_scaled"
    assert "private-a" not in json.dumps(report) and "private-b" not in json.dumps(report)
    assert before == (book.balance, book.committed_stake, opened)


def test_distinct_tickets_opposite_sides_and_no_always_losing_ticket():
    _, opened = tickets((leg("a"),), (leg("b", "away"),))
    report = summarize(opened)
    assert report["pairs_cooccurring_in_a_ticket"] == 0
    assert report["max_tickets_one_exact_id"] == 1
    assert report["max_tickets_either_id"] == 2
    assert Decimal(report["max_stake_either_id"]) == 2
    assert report["mappings_with_lower_max"] == 1
    assert report["mappings_with_unwinnable_ticket"] == 0
    assert list(map(Decimal, report["conditional_max_range_units"])) == [0, 2]


def test_overlapping_ticket_exposure_counts_each_ticket_once():
    _, opened = tickets((leg("a"), leg("b")), (leg("a"), leg("c")), (leg("b"), leg("c")))
    report = summarize(opened)
    assert report["distinct_id_pairs"] == report["pairs_cooccurring_in_a_ticket"] == 3
    assert report["max_tickets_one_exact_id"] == 2
    assert report["max_tickets_either_id"] == 3
    assert Decimal(report["max_stake_either_id"]) == 3


def test_both_sides_of_one_exact_id_and_reversed_labels_are_supported():
    _, one = tickets((leg("a"), leg("b")), (leg("a", "away"),))
    _, reversed_one = tickets((leg("a", "away"), leg("b")), (leg("a"),))
    assert summarize(one) == summarize(reversed_one)
    assert summarize(one)["baseline_grid_states"] == 6


def test_no_pair_and_work_cap_do_not_report_conditional_extrema():
    assert summarize([])["scenario_status"] == "no_distinct_id_pair"
    _, one = tickets((leg("a"),))
    assert summarize(one)["scenario_status"] == "no_distinct_id_pair"
    _, two = tickets((leg("a"), leg("b")))
    report = summarize(two, max_work=7)
    assert report["scenario_status"] == "not_enumerated_work_cap"
    assert report["conditional_max_range_units"] is None
    assert report["mappings_with_lower_max"] is None
    for invalid in (True, 0, 65537):
        with pytest.raises(ValueError, match="work cap"):
            summarize(two, max_work=invalid)


def test_reference_disagreement_fails_closed(monkeypatch):
    _, opened = tickets((leg("a"), leg("b")))
    monkeypatch.setattr(module, "fraction_reference", lambda *args: {
        "states": 999, "min": 0, "max": 0, "unwinnable": 0})
    with pytest.raises(ValueError, match="rational reference"):
        summarize(opened)


def test_validation_is_reused():
    _, opened = tickets((leg("a"),))
    with pytest.raises(ValueError, match="duplicate ticket"):
        summarize([opened[0], deepcopy(opened[0])])
    _, duplicate_event = tickets((leg("a"), leg("a", "away")))
    with pytest.raises(ValueError, match="multiple legs"):
        summarize(duplicate_event)


def test_replay_preserves_old_reports_and_ignores_changed_future_outcome():
    data = {"matches": [fixture("private-a", [frame("private-a", 0), frame("private-a", 180)], "home"),
                        fixture("private-b", [frame("private-b", 0), frame("private-b", 180)], "away")]}
    copy = deepcopy(data)
    before = replay(data, include_scenario_stress=True, include_single_void_stress=True)
    after = replay(data, include_scenario_stress=True, include_single_void_stress=True,
                   include_alias_sensitivity=True)
    sensitivity = after.pop("alias_sensitivity")
    assert before == after and data == copy
    copy["matches"][0]["result"]["periods"] = [{"score": "2-11"}] * 3
    copy["matches"][0]["result"]["final_score"] = "2-11,2-11,2-11"
    assert replay(copy, include_alias_sensitivity=True)["alias_sensitivity"] == sensitivity
    assert "private-a" not in json.dumps(sensitivity)
    with pytest.raises(ValueError, match="boolean"):
        replay(data, include_alias_sensitivity=1)


def test_pinned_audit_fails_on_wrong_hash_and_omits_fixture_ids():
    data = {"matches": [fixture("private-a", [frame("private-a", 0)], None),
                        fixture("private-b", [frame("private-b", 0)], None)]}
    raw = json.dumps(data).encode()
    result = audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest())
    assert result["baseline_and_previous_stresses_unchanged"]
    assert "private-a" not in json.dumps(result)
    assert result["source_identity_verified"] is False
    with pytest.raises(ValueError, match="externally pinned"):
        audit(raw, expected_sha256="f" * 64)
