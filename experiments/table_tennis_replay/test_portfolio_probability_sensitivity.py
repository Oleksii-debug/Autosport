from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
import hashlib
import json

import pytest

import portfolio_probability_sensitivity as probability_module
from portfolio_probability_sensitivity import summarize_probability_sensitivity
from portfolio_probability_sensitivity_replay import audit
from portfolio_replay import replay
from test_portfolio_replay import fixture, frame
from test_portfolio_scenario_stress import leg, tickets


def test_same_marginals_can_produce_different_ticket_risk():
    book, opened = tickets((leg("private-a"), leg("private-b")))
    before = (book.balance, book.committed_stake, [ticket.status for ticket in opened])
    report = summarize_probability_sensitivity(opened)
    independent, common = report["independent_exact_ids"], report["all_home_or_all_away"]
    assert (independent["state_count"], common["state_count"]) == (4, 2)
    assert Decimal(independent["conditional_mean_open_ticket_pnl_units"]) == 0
    assert Decimal(common["conditional_mean_open_ticket_pnl_units"]) == 1
    assert Decimal(independent["conditional_probability_all_open_tickets_lose"]) == Decimal("0.75")
    assert Decimal(common["conditional_probability_all_open_tickets_lose"]) == Decimal("0.5")
    assert independent["scenario_search_cross_check"] == "matched_exact_independent_groups"
    assert independent["probability_authority_proven"] is common["probability_authority_proven"] is False
    assert "private-a" not in json.dumps(report) and "private-b" not in json.dumps(report)
    assert before == (book.balance, book.committed_stake, [ticket.status for ticket in opened])


def test_shared_leg_is_a_single_event_and_exact_all_lose_probability():
    _, opened = tickets((leg("a"), leg("b")), (leg("a"), leg("c")))
    report = summarize_probability_sensitivity(opened)
    assert report["exact_event_ids"] == 3
    assert Decimal(report["independent_exact_ids"]["conditional_probability_all_open_tickets_lose"]) == Decimal("0.625")
    assert Decimal(report["all_home_or_all_away"]["conditional_mean_open_ticket_pnl_units"]) == 2


def test_opposite_sides_share_exactly_one_binary_event():
    _, opened = tickets((leg("a"), leg("b")), (leg("a", "away"), leg("c")))
    report = summarize_probability_sensitivity(opened)
    assert report["hypothetical_opposed_side_event_count"] == 1
    for law in ("independent_exact_ids", "all_home_or_all_away"):
        assert Decimal(report[law]["conditional_probability_all_open_tickets_lose"]) == Decimal("0.5")
        assert Decimal(report[law]["conditional_mean_open_ticket_pnl_units"]) == 0


def test_all_tickets_lose_is_not_the_same_as_negative_total_pnl():
    # Under all-home, one low-odds ticket wins but total P&L is negative.
    _, opened = tickets((leg("a", odds="1.1"),), (leg("b", "away"),))
    report = summarize_probability_sensitivity(opened)["all_home_or_all_away"]
    assert Decimal(report["conditional_mean_open_ticket_pnl_units"]) < 0
    assert Decimal(report["conditional_probability_all_open_tickets_lose"]) == 0


def test_large_and_empty_snapshots_do_not_fabricate_probability_results(monkeypatch):
    _, opened = tickets(*[(leg("event-" + str(i)),) for i in range(9)])
    monkeypatch.setattr(probability_module, "analyse_joint_distribution",
                        lambda *args: pytest.fail("must not enumerate oversized or empty snapshot"))
    large = summarize_probability_sensitivity(opened)
    assert large["status"] == "not_computed_event_cap"
    assert large["independent_exact_ids"] is large["all_home_or_all_away"] is None
    empty = summarize_probability_sensitivity([])
    assert empty["status"] == "no_open_tickets" and empty["independent_exact_ids"] is None


def test_invalid_market_duplicate_event_and_stake_fail_closed():
    _, bad = tickets((leg("a", market="other"),))
    with pytest.raises(ValueError, match="unsupported"):
        summarize_probability_sensitivity(bad)
    _, repeated = tickets((leg("a"), leg("a", "away")))
    with pytest.raises(ValueError, match="multiple legs"):
        summarize_probability_sensitivity(repeated)
    _, bad_stake = tickets((leg("a"),))
    bad_stake[0].stake = Decimal("-1")
    with pytest.raises(ValueError, match="positive finite"):
        summarize_probability_sensitivity(bad_stake)
    _, same = tickets((leg("a"),))
    with pytest.raises(ValueError, match="duplicate ticket"):
        summarize_probability_sensitivity([same[0], deepcopy(same[0])])


def test_rational_disagreement_fails_closed(monkeypatch):
    _, opened = tickets((leg("a"), leg("b")))
    monkeypatch.setattr(probability_module, "_reference_profit", lambda *args: Fraction(999))
    with pytest.raises(ValueError, match="rational reference"):
        summarize_probability_sensitivity(opened)


def test_opt_in_preserves_all_previous_replay_results_and_input():
    data = {"matches": [fixture("private-a", [frame("private-a", 0), frame("private-a", 180)], "home"),
                        fixture("private-b", [frame("private-b", 0), frame("private-b", 180)], "away")]}
    original = deepcopy(data)
    flags = dict(include_scenario_stress=True, include_single_void_stress=True, include_alias_sensitivity=True)
    before = replay(data, **flags)
    after = replay(data, **flags, include_probability_sensitivity=True)
    probability = after.pop("probability_sensitivity")
    assert after == before and data == original
    assert probability["first_peak_open_tickets"]["open_tickets"] == 2
    assert "private-a" not in json.dumps(probability) and "private-b" not in json.dumps(probability)


def test_future_result_change_does_not_change_earlier_peak():
    data = {"matches": [fixture("a", [frame("a", 0), frame("a", 180)], "home"),
                        fixture("b", [frame("b", 0), frame("b", 180)], "away")]}
    changed = deepcopy(data)
    changed["matches"][0]["result"]["periods"] = [{"score": "2-11"}] * 3
    changed["matches"][0]["result"]["final_score"] = "2-11,2-11,2-11"
    assert replay(data, include_probability_sensitivity=True)["probability_sensitivity"] == \
           replay(changed, include_probability_sensitivity=True)["probability_sensitivity"]


def test_boolean_flag_and_pinned_input_fail_closed():
    with pytest.raises(ValueError, match="boolean"):
        replay({"matches": []}, include_probability_sensitivity=1)
    data = {"matches": [fixture("private-a", [frame("private-a", 0)], None),
                        fixture("private-b", [frame("private-b", 0)], None)]}
    raw = json.dumps(data).encode()
    report = audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest())
    assert report["baseline_and_previous_stresses_unchanged"] is report["retrospective_postselected"] is True
    assert report["probability_sensitivity"]["first_peak_open_tickets"]["open_tickets"] == 1
    assert "private-a" not in json.dumps(report) and "private-b" not in json.dumps(report)
    with pytest.raises(ValueError, match="externally pinned"):
        audit(raw, expected_sha256="f" * 64)
