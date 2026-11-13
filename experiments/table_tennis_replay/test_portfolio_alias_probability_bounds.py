"""Analytical fixtures use invented IDs, quotes and results only."""
from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace
import hashlib
import json

import pytest

pytest.importorskip("scipy", reason="optional SciPy required for certified research bounds")

import portfolio_alias_probability_bounds as module
from portfolio_alias_probability_bounds import summarize_alias_probability_bounds as summarize
from portfolio_alias_probability_bounds_replay import audit
from portfolio_alias_probability_reference import count_extreme_mappings
from portfolio_replay import replay
from test_portfolio_alias_sensitivity import leg, tickets
from test_portfolio_replay import fixture, frame


def dist(report, name):
    return {(entry["lower"], entry["upper"]): entry["mappings"]
            for entry in report["alias_bounds"][name]}


def test_same_or_reversed_ids_in_single_parlay_are_analytically_exact():
    book, opened = tickets((leg("private-a"), leg("private-b")))
    before = (book.balance, book.committed_stake, deepcopy(opened))
    report = summarize(opened)
    assert report["baseline"]["all_tickets_lose"] == {"lower": "1/2", "upper": "1"}
    assert report["baseline"]["negative_open_ticket_pnl"] == {"lower": "1/2", "upper": "1"}
    expected = {("1/2", "1/2"): 1, ("1", "1"): 1}
    assert dist(report, "all_tickets_lose_distribution") == expected
    assert dist(report, "negative_open_ticket_pnl_distribution") == expected
    assert report["alias_bounds"]["mappings_narrowing_all_tickets_lose"] == 2
    assert report["alias_bounds"]["binary_states_per_mapping"] == 2
    assert report["exact_primal_and_dual_certificates_checked"]
    assert "private-a" not in json.dumps(report) and "private-b" not in json.dumps(report)
    assert before == (book.balance, book.committed_stake, opened)


def test_all_lose_is_not_the_same_as_negative_net_pnl():
    # Opposite sides of hypothetical alias IDs, low payouts: one winner still loses net.
    _, opened = tickets((leg("a", odds="1.1"),), (leg("b", "away", odds="1.1"),))
    report = summarize(opened)
    assert report["baseline"]["all_tickets_lose"] == {"lower": "0", "upper": "1/2"}
    assert report["baseline"]["negative_open_ticket_pnl"] == {"lower": "1/2", "upper": "1"}
    assert dist(report, "all_tickets_lose_distribution") == {("0", "0"): 1, ("1/2", "1/2"): 1}
    assert dist(report, "negative_open_ticket_pnl_distribution") == {("1/2", "1/2"): 1, ("1", "1"): 1}


@pytest.mark.parametrize("odds, expected", [
    ("2", "0"),
    ("1.9999999999999999999999999999", "1"),
    ("2.0000000000000000000000000001", "0"),
])
def test_negative_pnl_uses_strict_exact_rational_sign(odds, expected):
    _, opened = tickets((leg("a", odds=odds),), (leg("a", "away", odds=odds),))
    report = summarize(opened)
    assert report["status"] == "no_distinct_id_pair"
    assert report["baseline"]["all_tickets_lose"] == {"lower": "0", "upper": "0"}
    assert report["baseline"]["negative_open_ticket_pnl"] == {"lower": expected, "upper": expected}


def test_relabeling_sides_and_ticket_order_do_not_change_aggregate():
    _, first = tickets((leg("a"), leg("b")), (leg("a", "away"), leg("c")))
    _, swapped = tickets((leg("a", "away"), leg("b")), (leg("a"), leg("c")))
    assert summarize(first) == summarize(swapped)
    assert summarize(first) == summarize(list(reversed(first)))


def test_no_open_or_capacity_fail_closed_without_calling_solver(monkeypatch):
    monkeypatch.setattr(module, "_run_linprog", lambda *args: pytest.fail("solver must not run"))
    assert summarize([])["status"] == "no_open_tickets"
    _, large = tickets(*[(leg("e" + str(i)),) for i in range(9)])
    assert summarize(large)["status"] == "not_computed_event_cap"
    _, two = tickets((leg("a"), leg("b")))
    capped = summarize(two, max_mappings=1)
    assert capped["status"] == "not_computed_mapping_cap"
    assert capped["baseline"] is capped["alias_bounds"] is None
    for value in (True, 0, 57, 1.0):
        with pytest.raises(ValueError, match="mapping cap"):
            summarize(two, max_mappings=value)


def test_existing_ticket_validation_is_reused():
    _, opened = tickets((leg("a"),))
    with pytest.raises(ValueError, match="duplicate ticket"):
        summarize([opened[0], deepcopy(opened[0])])
    _, repeated = tickets((leg("a"), leg("a", "away")))
    with pytest.raises(ValueError, match="multiple legs"):
        summarize(repeated)
    _, invalid = tickets((leg("a"),))
    invalid[0].stake = Decimal("-1")
    with pytest.raises(ValueError, match="positive finite"):
        summarize(invalid)


def test_invalid_solver_and_reference_economics_do_not_publish_bounds(monkeypatch):
    _, opened = tickets((leg("a"), leg("b")))
    with monkeypatch.context() as patch:
        patch.setattr(module, "_reference_profit", lambda *args: Fraction(999))
        with pytest.raises(ValueError, match="rational reference"):
            summarize(opened)
    with monkeypatch.context() as patch:
        patch.setattr(module, "_run_linprog", lambda *args: SimpleNamespace(success=False))
        with pytest.raises(ValueError, match="solver did not succeed"):
            summarize(opened)
    with monkeypatch.context() as patch:
        patch.setattr(module, "summarize_joint_bounds", lambda *args: {
            "status": "exact_fraction_primal_dual_certified",
            "lower_all_lose_probability": "0", "upper_all_lose_probability": "0"})
        with pytest.raises(ValueError, match="previous certified report"):
            summarize(opened)
    assert count_extreme_mappings(opened) == {
        "all_lose_always": 1, "all_lose_never": 0,
        "negative_always": 1, "negative_never": 0}
    with monkeypatch.context() as patch:
        patch.setattr(module, "count_extreme_mappings", lambda *args: {})
        with pytest.raises(ValueError, match="direct alias state extremes"):
            summarize(opened)


def test_opting_in_preserves_prior_reports_input_and_causal_peak():
    data = {"matches": [fixture("private-a", [frame("private-a", 0), frame("private-a", 180)], "home"),
                        fixture("private-b", [frame("private-b", 0), frame("private-b", 180)], "away")]}
    original = deepcopy(data)
    flags = dict(include_scenario_stress=True, include_single_void_stress=True,
                 include_alias_sensitivity=True, include_probability_sensitivity=True,
                 include_joint_probability_bounds=True)
    prior = replay(data, **flags)
    updated = replay(data, **flags, include_alias_probability_bounds=True)
    report = updated.pop("alias_probability_bounds")
    assert prior == updated and data == original
    assert "private-a" not in json.dumps(report) and "private-b" not in json.dumps(report)
    changed = deepcopy(data)
    changed["matches"][0]["result"]["periods"] = [{"score": "2-11"}] * 3
    changed["matches"][0]["result"]["final_score"] = "2-11,2-11,2-11"
    assert replay(changed, include_alias_probability_bounds=True)["alias_probability_bounds"] == report
    with pytest.raises(ValueError, match="boolean"):
        replay(data, include_alias_probability_bounds=1)


def test_input_pin_audit_and_no_raw_ids_in_aggregate():
    data = {"matches": [fixture("private-a", [frame("private-a", 0)], None),
                        fixture("private-b", [frame("private-b", 0)], None)]}
    raw = json.dumps(data).encode()
    report = audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest())
    assert report["baseline_and_previous_stresses_unchanged"]
    assert report["retrospective_postselected"] and not report["source_identity_verified"]
    assert "private-a" not in json.dumps(report) and "private-b" not in json.dumps(report)
    with pytest.raises(ValueError, match="externally pinned"):
        audit(raw, expected_sha256="f" * 64)
    with pytest.raises(ValueError, match="empty or oversized"):
        audit(b"", expected_sha256=hashlib.sha256(b"").hexdigest())
