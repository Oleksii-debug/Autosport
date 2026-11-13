"""Synthetic void scenarios only; no real source or bookmaker evidence."""
from copy import deepcopy
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import json
from types import SimpleNamespace

import pytest

from autosport.domain import TicketStatus
import portfolio_scenario_fraction_reference as reference_module
from portfolio_replay import replay
import portfolio_single_void_stress as module
from portfolio_single_void_replay import audit
from portfolio_single_void_stress import summarize_single_void
from test_portfolio_replay import fixture, frame
from test_portfolio_scenario_stress import leg, tickets


def test_shared_void_effect_matches_actual_paperbook_settlement():
    book, items = tickets((leg("private-a"), leg("private-b")),
                          (leg("private-a"), leg("private-c")))
    before = (book.balance, book.committed_stake, [t.status for t in items])
    stress = summarize_single_void(items)
    assert before == (book.balance, book.committed_stake, [t.status for t in items])
    assert stress["binary_baseline_states"] == 8
    assert stress["planned_paired_comparisons"] == stress["completed_paired_comparisons"] == 24
    assert stress["comparison_status"] == "exact"
    assert stress["affected_ticket_count_range"] == [1, 2]
    assert stress["affected_stake_units_range"] == ["1", "2"]
    assert Decimal(stress["void_minus_nonvoid_min_units"]) == -4
    assert Decimal(stress["void_minus_nonvoid_max_units"]) == 4
    for ticket in items:
        wins = {x.quote_key for x in ticket.legs if x.event_id != "private-a"}
        voids = {x.quote_key for x in ticket.legs if x.event_id == "private-a"}
        book.settle(ticket.ticket_id, wins, voids, settled_at="2026-01-01T00:01:00+00:00")
    assert all(t.status is TicketStatus.WON and t.payout == 2 for t in items)
    # Original all-win P&L is +6; voiding shared A leaves +2.
    assert book.balance - Decimal("100") == 2
    assert stress["fraction_check"] == "all_pairs_matched_within_1e-24_scaled"
    assert stress["independent_grid_fraction_check"] == "matched_within_1e-24_scaled"
    assert "private-" not in json.dumps(stress)


def test_single_leg_void_refunds_stake_not_a_loss():
    book, items = tickets((leg("a"),))
    stress = summarize_single_void(items)
    assert stress["planned_paired_comparisons"] == 2
    assert Decimal(stress["void_scenario_pnl_min_units"]) == 0
    assert Decimal(stress["void_scenario_pnl_max_units"]) == 0
    assert Decimal(stress["void_minus_nonvoid_min_units"]) == -1
    assert Decimal(stress["void_minus_nonvoid_max_units"]) == 1
    book.settle(items[0].ticket_id, set(), {items[0].legs[0].quote_key},
                settled_at="2026-01-01T00:01:00+00:00")
    assert items[0].status is TicketStatus.VOID and items[0].payout == 1
    assert book.balance == 100


def test_void_with_another_losing_leg_still_loses_ticket():
    book, items = tickets((leg("a"), leg("b")))
    stress = summarize_single_void(items)
    assert Decimal(stress["void_scenario_pnl_min_units"]) == -1
    assert Decimal(stress["void_scenario_pnl_max_units"]) == 1
    assert Decimal(stress["void_minus_nonvoid_min_units"]) == -2
    assert Decimal(stress["void_minus_nonvoid_max_units"]) == 2
    book.settle(items[0].ticket_id, set(), {items[0].legs[0].quote_key},
                settled_at="2026-01-01T00:01:00+00:00")
    assert items[0].status is TicketStatus.LOST and items[0].payout == 0


def test_opposed_sides_void_together_instead_of_both_winning():
    book, items = tickets((leg("a"), leg("b")), (leg("a", "away"), leg("c")))
    stress = summarize_single_void(items)
    assert stress["binary_baseline_states"] == 12
    assert stress["planned_paired_comparisons"] == 36
    for ticket in items:
        wins = {x.quote_key for x in ticket.legs if x.event_id != "a"}
        voids = {x.quote_key for x in ticket.legs if x.event_id == "a"}
        book.settle(ticket.ticket_id, wins, voids, settled_at="2026-01-01T00:01:00+00:00")
    assert sum(t.payout - t.stake for t in items) == 2
    assert Decimal(stress["void_scenario_pnl_max_units"]) >= 2
    assert stress["bookmaker_void_rules_verified"] is False


def test_decimal_context_cannot_change_scenario_result():
    _, items = tickets((leg("a", odds="1.3333333333333333"), leg("b", odds="1.7777777777777777")),)
    expected = summarize_single_void(items)
    with localcontext() as context:
        context.prec = 3
        observed = summarize_single_void(items)
    assert observed == expected


def test_comparison_cap_fails_closed_without_partial_extrema():
    _, items = tickets(*[(leg("e-" + str(i)),) for i in range(9)])
    result = summarize_single_void(items, max_comparisons=100)
    assert result["planned_paired_comparisons"] == 9 * 512
    assert result["comparison_status"] == "not_enumerated_comparison_cap"
    assert result["completed_paired_comparisons"] == 0
    assert result["void_minus_nonvoid_min_units"] is result["void_scenario_pnl_max_units"] is None
    assert result["fraction_check"] == "not_run"


def test_empty_bad_cap_and_unsupported_inputs_are_explicit():
    result = summarize_single_void([])
    assert result["comparison_status"] == "no_open_events"
    assert result["planned_paired_comparisons"] == 0
    with pytest.raises(ValueError, match="comparison cap"):
        summarize_single_void([], max_comparisons=True)
    _, bad = tickets((leg("a", market="other"),))
    with pytest.raises(ValueError, match="unsupported"):
        summarize_single_void(bad)
    _, same = tickets((leg("a"), leg("a", "away")))
    with pytest.raises(ValueError, match="multiple legs"):
        summarize_single_void(same)


def test_wrong_canonical_settlement_fails_independent_fraction_check(monkeypatch):
    _, items = tickets((leg("a"),))
    monkeypatch.setattr(module, "PaperBook", SimpleNamespace(
        _settlement_result=lambda *args: (TicketStatus.WON, Decimal("100"), Decimal("100"))))
    with pytest.raises(ValueError, match="rational reference"):
        summarize_single_void(items)


def test_independent_grid_disagreement_fails_closed(monkeypatch):
    _, items = tickets((leg("a"),))
    monkeypatch.setattr(module, "independent_reference", lambda _: {
        "status": "exact_fraction", "comparisons": 2, "delta_min": Fraction(-99),
        "delta_max": Fraction(1), "void_min": Fraction(0), "void_max": Fraction(0),
        "sign_counts": {"negative": 1, "zero": 0, "positive": 1}})
    with pytest.raises(ValueError, match="independent fraction grid differs"):
        summarize_single_void(items)


def test_reference_cap_is_reported_without_claiming_a_crosscheck(monkeypatch):
    _, items = tickets((leg("a"),))
    monkeypatch.setattr(reference_module, "REFERENCE_CAP", 1)
    result = summarize_single_void(items)
    assert result["comparison_status"] == "exact"
    assert result["fraction_check"] == "all_pairs_matched_within_1e-24_scaled"
    assert result["independent_grid_fraction_check"] == "skipped_reference_cap"


def test_canonical_and_rational_formulas_match_rounding_boundary():
    _, items = tickets((leg("a", odds="1.123456789012345678901234567"),
                        leg("b", odds="1.234567890123456789012345678")))
    result = summarize_single_void(items)
    assert result["comparison_status"] == "exact"
    assert result["fraction_check"] == "all_pairs_matched_within_1e-24_scaled"


def test_opt_in_does_not_change_baseline_or_earlier_binary_stress():
    data = {"matches": [fixture("private-a", [frame("private-a", 0), frame("private-a", 180)], "home"),
                        fixture("private-b", [frame("private-b", 0), frame("private-b", 180)], "away")]}
    frozen = deepcopy(data)
    baseline = replay(data)
    binary = replay(data, include_scenario_stress=True)
    combined = replay(data, include_scenario_stress=True, include_single_void_stress=True)
    stress = combined.pop("single_void_stress")
    assert combined == binary
    assert replay(data) == baseline and data == frozen
    assert "private-" not in json.dumps(stress)
    assert stress["source_independence_verified"] is stress["profitability_claim"] is False


def test_future_result_change_does_not_rewrite_earlier_snapshot():
    data = {"matches": [fixture("a", [frame("a", 0), frame("a", 180)], "home"),
                        fixture("b", [frame("b", 0), frame("b", 180)], "away")]}
    later = deepcopy(data)
    later["matches"][0]["result"]["periods"] = [{"score": "2-11"}] * 3
    later["matches"][0]["result"]["final_score"] = "2-11,2-11,2-11"
    assert replay(data, include_single_void_stress=True)["single_void_stress"] == \
           replay(later, include_single_void_stress=True)["single_void_stress"]


def test_pinned_replay_and_flag_validation():
    data = {"matches": [fixture("private-a", [frame("private-a", 0)], None),
                        fixture("private-b", [frame("private-b", 0)], None)]}
    raw = json.dumps(data).encode()
    report = audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest())
    assert report["baseline_replay_unchanged"] is report["prior_binary_stress_unchanged"] is True
    assert "private-" not in json.dumps(report)
    with pytest.raises(ValueError, match="externally pinned"):
        audit(raw, expected_sha256="f" * 64)
    with pytest.raises(ValueError, match="boolean"):
        replay({"matches": []}, include_single_void_stress=1)
