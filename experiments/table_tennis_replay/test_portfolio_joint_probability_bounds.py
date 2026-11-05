from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace
import hashlib
import json

import pytest

pytest.importorskip("scipy", reason="optional SciPy required for joint-bound research")

import portfolio_joint_probability_bounds as bounds_module
from portfolio_joint_probability_bounds import _certify_candidate, summarize_joint_bounds
from portfolio_joint_probability_bounds_replay import audit
from portfolio_replay import replay
from test_portfolio_replay import fixture, frame
from test_portfolio_scenario_stress import leg, tickets


def test_two_leg_ticket_has_exact_half_to_one_bounds():
    book, opened = tickets((leg("private-a"), leg("private-b")))
    before = (book.balance, book.committed_stake, [x.status for x in opened])
    result = summarize_joint_bounds(opened)
    assert (result["lower_all_lose_probability"], result["upper_all_lose_probability"]) == ("1/2", "1")
    assert result["independent_all_lose_probability"] == "3/4"
    assert result["all_home_or_away_all_lose_probability"] == "1/2"
    assert result["exact_primal_and_dual_certificates_checked"] is True
    assert result["probability_authority_proven"] is result["physical_event_identity_verified"] is False
    assert "private-a" not in json.dumps(result) and "private-b" not in json.dumps(result)
    assert before == (book.balance, book.committed_stake, [x.status for x in opened])


def test_common_leg_and_opposite_side_are_joint_states():
    _, shared = tickets((leg("a"), leg("b")), (leg("a"), leg("c")))
    a = summarize_joint_bounds(shared)
    assert a["exact_event_ids"] == 3 and a["states"] == 8
    assert (a["lower_all_lose_probability"], a["upper_all_lose_probability"]) == ("1/2", "1")
    _, opposed = tickets((leg("a"), leg("b")), (leg("a", "away"), leg("c")))
    b = summarize_joint_bounds(opposed)
    assert b["opposed_side_event_count"] == 1
    assert (b["lower_all_lose_probability"], b["upper_all_lose_probability"]) == ("0", "1")


def test_opposing_single_legs_cannot_both_lose_under_binary_assumption():
    _, opened = tickets((leg("a", odds="1.1"),), (leg("a", "away", odds="1.1"),))
    report = summarize_joint_bounds(opened)
    assert report["lower_all_lose_probability"] == report["upper_all_lose_probability"] == "0"
    assert report["bound_is_for_all_tickets_losing_not_negative_total_pnl"] is True
    # In either binary state one ticket wins 1.1 and the other loses 1: net -0.9.
    assert Decimal("1.1") - 2 < 0


def test_empty_and_large_state_space_return_no_bound(monkeypatch):
    _, opened = tickets(*[(leg("event-" + str(i)),) for i in range(9)])
    monkeypatch.setattr(bounds_module, "_run_linprog", lambda *args: pytest.fail("solver must not run"))
    large = summarize_joint_bounds(opened)
    assert large["status"] == "not_computed_event_cap"
    assert large["lower_all_lose_probability"] is large["upper_all_lose_probability"] is None
    empty = summarize_joint_bounds([])
    assert empty["status"] == "no_open_tickets" and empty["lower_all_lose_probability"] is None


def test_bad_ticket_inputs_are_rejected_by_existing_validator():
    _, unsupported = tickets((leg("a", market="other"),))
    with pytest.raises(ValueError, match="unsupported"):
        summarize_joint_bounds(unsupported)
    _, repeated = tickets((leg("a"), leg("a", "away")))
    with pytest.raises(ValueError, match="multiple legs"):
        summarize_joint_bounds(repeated)
    _, bad = tickets((leg("a"),))
    bad[0].stake = Decimal("-1")
    with pytest.raises(ValueError, match="positive finite"):
        summarize_joint_bounds(bad)
    _, same = tickets((leg("a"),))
    with pytest.raises(ValueError, match="duplicate ticket"):
        summarize_joint_bounds([same[0], deepcopy(same[0])])


def candidate(x, dual, success=True):
    return SimpleNamespace(success=success, x=x, eqlin=SimpleNamespace(marginals=dual))


def test_exact_primal_dual_certificates_reject_false_solver_claims():
    matrix, objective = [[1, 1], [1, 0]], [1, 0]
    assert _certify_candidate(matrix, objective, candidate([0.5, 0.5], [0, 1])) == (Fraction(1, 2), 2)
    with pytest.raises(ValueError, match="solver did not succeed"):
        _certify_candidate(matrix, objective, candidate([0.5, 0.5], [0, 1], success=False))
    with pytest.raises(ValueError, match="violates exact marginals"):
        _certify_candidate(matrix, objective, candidate([0.25, 0.75], [0, 1]))
    with pytest.raises(ValueError, match="state inequality"):
        _certify_candidate(matrix, objective, candidate([0.5, 0.5], [1, 0]))
    with pytest.raises(ValueError, match="objectives differ"):
        _certify_candidate(matrix, objective, candidate([0.5, 0.5], [0, 0]))
    with pytest.raises(ValueError, match="non-finite"):
        _certify_candidate(matrix, objective, candidate([float("nan"), 0.5], [0, 1]))


def test_canonical_arithmetic_mismatch_and_solver_failure_are_not_certified(monkeypatch):
    _, opened = tickets((leg("a"), leg("b")))
    with monkeypatch.context() as patch:
        patch.setattr(bounds_module, "_reference_profit", lambda *args: Fraction(999))
        with pytest.raises(ValueError, match="rational reference"):
            summarize_joint_bounds(opened)
    monkeypatch.setattr(bounds_module, "_run_linprog", lambda *args: candidate([], [], success=False))
    with pytest.raises(ValueError, match="solver did not succeed"):
        summarize_joint_bounds(opened)


def test_opt_in_preserves_baseline_previous_stresses_and_input():
    data = {"matches": [fixture("private-a", [frame("private-a", 0), frame("private-a", 180)], "home"),
                        fixture("private-b", [frame("private-b", 0), frame("private-b", 180)], "away")]}
    original = deepcopy(data)
    flags = dict(include_scenario_stress=True, include_single_void_stress=True,
                 include_alias_sensitivity=True, include_probability_sensitivity=True)
    before = replay(data, **flags)
    after = replay(data, **flags, include_joint_probability_bounds=True)
    report = after.pop("joint_probability_bounds")
    assert after == before and data == original
    assert report["first_peak_open_tickets"]["open_tickets"] == 2
    assert "private-a" not in json.dumps(report) and "private-b" not in json.dumps(report)


def test_future_outcome_cannot_alter_earlier_peak():
    data = {"matches": [fixture("a", [frame("a", 0), frame("a", 180)], "home"),
                        fixture("b", [frame("b", 0), frame("b", 180)], "away")]}
    changed = deepcopy(data)
    changed["matches"][0]["result"]["periods"] = [{"score": "2-11"}] * 3
    changed["matches"][0]["result"]["final_score"] = "2-11,2-11,2-11"
    assert replay(data, include_joint_probability_bounds=True)["joint_probability_bounds"] == \
           replay(changed, include_joint_probability_bounds=True)["joint_probability_bounds"]


def test_pinned_input_and_exact_boolean_flag():
    with pytest.raises(ValueError, match="boolean"):
        replay({"matches": []}, include_joint_probability_bounds=1)
    data = {"matches": [fixture("private-a", [frame("private-a", 0)], None),
                        fixture("private-b", [frame("private-b", 0)], None)]}
    raw = json.dumps(data).encode()
    result = audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest())
    assert result["baseline_and_previous_stresses_unchanged"] is True
    assert result["retrospective_postselected"] is True
    assert "private-a" not in json.dumps(result) and "private-b" not in json.dumps(result)
    with pytest.raises(ValueError, match="externally pinned"):
        audit(raw, expected_sha256="f" * 64)
