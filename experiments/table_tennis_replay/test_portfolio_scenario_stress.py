from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
import hashlib
import json

import pytest

import portfolio_scenario_stress as stress_module
from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from portfolio_replay import replay
from portfolio_scenario_stress import summarize_snapshot
from portfolio_scenario_stress_replay import audit
from test_portfolio_replay import fixture, frame


def leg(event, side="home", odds="2", market="match_winner"):
    return TicketLeg(event, market, side, Decimal(odds), sport="table_tennis")


def tickets(*legs):
    book = PaperBook(Decimal("100"))
    result = [book.open_ticket(tuple(group), Decimal("1"), reason="synthetic",
                               placed_at="2026-01-01T00:00:00+00:00") for group in legs]
    return book, result


def test_shared_leg_loss_changes_all_affected_tickets_without_independence():
    book, open_tickets = tickets((leg("private-a"), leg("private-b")),
                                 (leg("private-a"), leg("private-c")))
    before = (book.balance, book.committed_stake, [t.status for t in open_tickets])
    result = summarize_snapshot(open_tickets)
    assert (result["open_tickets"], result["exact_event_ids"], result["grid_states"]) == (2, 3, 8)
    assert result["grid_status"] == "exact"
    assert result["fraction_reference"]["status"] == "matched_within_1e-24_scaled"
    assert Decimal(result["grid_min_open_ticket_pnl_units"]) == -2
    assert Decimal(result["grid_max_open_ticket_pnl_units"]) == 6
    impact = result["one_event_loss_from_all_win"]
    assert Decimal(impact["max_scenario_pnl_drop_units"]) == 8
    assert impact["affected_ticket_count_range"] == [2, 2]
    assert impact["affected_stake_units_range"] == ["2", "2"]
    assert result["no_probability_or_expected_return"] is True
    assert "private-a" not in json.dumps(result)
    assert before == (book.balance, book.committed_stake, [t.status for t in open_tickets])


def test_opposite_sides_are_exclusive_and_all_win_counterfactual_is_unavailable():
    _, open_tickets = tickets((leg("a"), leg("b")), (leg("a", "away"), leg("c")))
    result = summarize_snapshot(open_tickets)
    assert result["grid_status"] == "conservative-enumeration"
    assert result["fraction_reference"]["status"] == "matched_within_1e-24_scaled"
    assert result["grid_states"] == 12
    assert Decimal(result["grid_min_open_ticket_pnl_units"]) == -2
    assert Decimal(result["grid_max_open_ticket_pnl_units"]) == 2
    assert result["one_event_loss_from_all_win"] is None


def test_tied_single_leg_impacts_report_aggregate_ranges():
    _, open_tickets = tickets((leg("a"), leg("b")))
    impact = summarize_snapshot(open_tickets)["one_event_loss_from_all_win"]
    assert Decimal(impact["max_scenario_pnl_drop_units"]) == 4
    assert impact["tied_event_count"] == 2
    assert impact["affected_ticket_count_range"] == [1, 1]


def test_large_scenario_space_is_not_claimed_as_enumerated():
    _, open_tickets = tickets(*[(leg("event-" + str(i)),) for i in range(17)])
    report = summarize_snapshot(open_tickets)
    assert report["grid_states"] == 131072
    assert report["grid_status"] == "not_enumerated_state_cap"
    assert report["fraction_reference"]["status"] == "not_cross_checked_due_to_state_cap"
    assert report["grid_min_open_ticket_pnl_units"] is report["grid_max_open_ticket_pnl_units"] is None


def test_fraction_reference_disagreement_fails_closed(monkeypatch):
    _, open_tickets = tickets((leg("a"),))
    monkeypatch.setattr(stress_module, "fraction_reference", lambda _: {
        "status": "exact_fraction", "states": 2, "min": Fraction(-99),
        "max": Fraction(1), "one_event_max_drop": Fraction(2)})
    with pytest.raises(ValueError, match="rational reference"):
        summarize_snapshot(open_tickets)


def test_empty_and_unsupported_scenarios_are_explicit():
    empty = summarize_snapshot([])
    assert empty["grid_states"] == 1 and empty["open_tickets"] == 0
    assert empty["one_event_loss_from_all_win"] is None
    _, bad = tickets((leg("a", market="other"),))
    with pytest.raises(ValueError, match="unsupported"):
        summarize_snapshot(bad)
    _, same_event = tickets((leg("a"), leg("a", "away")))
    with pytest.raises(ValueError, match="multiple legs"):
        summarize_snapshot(same_event)
    with pytest.raises(ValueError, match="state cap"):
        summarize_snapshot([], max_states=True)


def test_duplicate_ticket_and_invalid_stake_are_rejected():
    _, open_tickets = tickets((leg("a"),))
    with pytest.raises(ValueError, match="duplicate ticket"):
        summarize_snapshot([open_tickets[0], deepcopy(open_tickets[0])])
    open_tickets[0].stake = Decimal("-1")
    with pytest.raises(ValueError, match="positive finite"):
        summarize_snapshot(open_tickets)


def test_opt_in_stress_does_not_change_the_recorded_paper_replay():
    data = {"matches": [fixture("private-a", [frame("private-a", 0), frame("private-a", 180)], "home"),
                        fixture("private-b", [frame("private-b", 0), frame("private-b", 180)], "away")]}
    original = deepcopy(data)
    baseline = replay(data)
    stress_run = replay(data, include_scenario_stress=True)
    stress = stress_run.pop("scenario_stress")
    assert stress_run == baseline and data == original
    assert stress["first_peak_open_tickets"]["open_tickets"] == 2
    assert stress["first_peak_exact_id_sharing"]["max_tickets_sharing_exact_event_id"] == 2
    assert "private-a" not in json.dumps(stress) and "private-b" not in json.dumps(stress)
    assert stress["source_independence_verified"] is stress["profitability_claim"] is False


def test_future_result_changes_cannot_change_stress_at_earlier_peak():
    data = {"matches": [fixture("a", [frame("a", 0), frame("a", 180)], "home"),
                        fixture("b", [frame("b", 0), frame("b", 180)], "away")]}
    later = deepcopy(data)
    later["matches"][0]["result"]["periods"] = [{"score": "2-11"}] * 3
    later["matches"][0]["result"]["final_score"] = "2-11,2-11,2-11"
    assert replay(data, include_scenario_stress=True)["scenario_stress"] == \
           replay(later, include_scenario_stress=True)["scenario_stress"]


def test_stress_flag_rejects_non_boolean_values():
    with pytest.raises(ValueError, match="boolean"):
        replay({"matches": []}, include_scenario_stress=1)


def test_pinned_replay_omits_rows_and_rejects_wrong_input_hash():
    data = {"matches": [fixture("private-a", [frame("private-a", 0)], None),
                        fixture("private-b", [frame("private-b", 0)], None)]}
    raw = json.dumps(data).encode()
    result = audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest())
    assert result["baseline_replay_unchanged"] is result["retrospective_postselected"] is True
    assert result["scenario_stress"]["first_peak_open_tickets"]["open_tickets"] == 1
    assert "private-a" not in json.dumps(result) and "private-b" not in json.dumps(result)
    with pytest.raises(ValueError, match="externally pinned"):
        audit(raw, expected_sha256="f" * 64)
