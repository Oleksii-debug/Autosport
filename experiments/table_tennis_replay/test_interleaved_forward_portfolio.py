from copy import deepcopy
from decimal import Decimal
import json

import pytest

import interleaved_forward_portfolio as interleaved
import joint_forward_portfolio as bounded
import test_forward_portfolio_journal as journal_test
from joint_forward_portfolio import SessionEvidence
from test_delayed_portfolio_replay import later_result
from test_forward_portfolio_journal import HEAD
from test_forward_portfolio_revisions import add, results
from test_joint_forward_portfolio import build, evidence, shift_fixture_clock


def custom(root, *, first_results=(), dated=()):
    root.mkdir()
    session, journal = journal_test.setup(root)
    journal_test.step1(session, journal, first_results=first_results)
    journal_test.step2(session, journal)
    history = root / "dated-history"; history.mkdir()
    head, _, _ = add(session, journal, history, None, list(dated), 0)
    return evidence(session, journal, history, head)


def events(report, kind, *, at=None):
    return [e for e in report["events"] if e["kind"] == kind and (at is None or e["at"] == at)]


def test_overlapping_sessions_share_one_book_and_keep_pending_exposure(tmp_path, monkeypatch):
    first = build(tmp_path / "first")
    shift_fixture_clock(monkeypatch, prefix="new-", seconds=60)
    second = build(tmp_path / "second", [later_result("new-a", "away", finished=90, updated=110),
                                         later_result("new-b", finished=90, updated=110)])
    with pytest.raises(ValueError, match="windows must not overlap"):
        bounded.replay([first, second])
    report = interleaved.replay([first, second])
    for state in report["policies"].values():
        assert (state["opened"], state["won"], state["lost"], state["pending"]) == (4, 2, 2, 0)
        assert state["peak_open_tickets"] == 4
        assert state["peak_tickets_sharing_exact_id"] == 2
        assert Decimal(state["cash_excluding_cost"]) == Decimal("100.50")
        assert Decimal(state["net_cost_stressed_pending_at_face_value"]) == Decimal("0.46")
    actions = events(report, "actions")
    assert actions[-1]["policies"]["no_worse"]["pending"] == 4
    assert actions[-1]["policies"]["no_worse"]["cash_excluding_cost"] == "96"
    assert report["different_source_ids_prove_independence"] is False
    assert report["real_money_execution"] is report["profitability_claim"] is False
    assert '"fixture_id"' not in json.dumps(report) and '"new-aA"' not in json.dumps(report)


def test_shared_risk_counts_other_sessions_open_stakes(tmp_path, monkeypatch):
    first = build(tmp_path / "first")
    shift_fixture_clock(monkeypatch, prefix="new-", seconds=60)
    second = build(tmp_path / "second", [later_result("new-a", finished=90, updated=110),
                                          later_result("new-b", finished=90, updated=110)])
    risk_class = bounded.PaperRiskPolicy
    monkeypatch.setattr(bounded, "PaperRiskPolicy", lambda: risk_class(max_committed_fraction=Decimal("0.02")))
    report = interleaved.replay([first, second])
    transitions = [e["policies"]["no_worse"]["transitions"] for e in events(report, "actions")]
    assert transitions == [{"counterfactual_assumed_open": 1}, {"counterfactual_assumed_open": 1},
                           {"shared_book_risk_rejected": 1}, {"shared_book_risk_rejected": 1}]
    assert report["policies"]["no_worse"]["peak_open_tickets"] == 2


def test_future_outcome_change_does_not_change_earlier_actions(tmp_path, monkeypatch):
    first = build(tmp_path / "first")
    shift_fixture_clock(monkeypatch, prefix="new-", seconds=60)
    win = build(tmp_path / "win", [later_result("new-a", finished=90, updated=110),
                                   later_result("new-b", finished=90, updated=110)])
    lose = build(tmp_path / "lose", [later_result("new-a", "away", finished=90, updated=110),
                                     later_result("new-b", finished=90, updated=110)])
    a = interleaved.replay([first, win]); b = interleaved.replay([first, lose])
    assert events(a, "actions") == events(b, "actions")
    assert a["policies"]["no_worse"]["won"] == 4
    assert b["policies"]["no_worse"]["lost"] == 2


def test_simultaneous_actions_are_order_invariant_and_both_blocked(tmp_path, monkeypatch):
    first = build(tmp_path / "first")
    shift_fixture_clock(monkeypatch, prefix="new-", seconds=0)
    second = build(tmp_path / "second", [later_result("new-a", finished=90, updated=110),
                                          later_result("new-b", finished=90, updated=110)])
    a = interleaved.replay([first, second]); b = interleaved.replay([second, first])
    assert a == b
    assert a["policies"]["no_worse"]["opened"] == 0
    assert all(e["policies"]["no_worse"]["transitions"] == {"simultaneous_action_time_ambiguous": 2}
               for e in events(a, "actions"))


def test_action_precedes_result_receipt_at_exact_same_time(tmp_path, monkeypatch):
    first = build(tmp_path / "first")
    shift_fixture_clock(monkeypatch, prefix="new-", seconds=117)
    second = build(tmp_path / "second", [later_result("new-a", finished=90, updated=110),
                                          later_result("new-b", finished=90, updated=110)])
    report = interleaved.replay([first, second])
    at = next(e["at"] for e in events(report, "receipts") if e["dated_receipts"] == 1)
    same = [e for e in report["events"] if e["at"] == at]
    assert [e["kind"] for e in same] == ["actions", "receipts"]
    before, after = (e["policies"]["no_worse"] for e in same)
    assert (before["opened"], before["won"], before["pending"]) == (3, 0, 3)
    assert (after["won"], after["pending"]) == (2, 1)


def test_same_time_generic_result_does_not_backdate_before_action(tmp_path, monkeypatch):
    first = build(tmp_path / "first")
    shift_fixture_clock(monkeypatch, prefix="new-", seconds=40)
    prior = later_result("a", "away", finished=0, updated=1)
    second = custom(tmp_path / "second", first_results=[prior], dated=[])
    report = interleaved.replay([first, second])
    action = events(report, "actions")[0]
    same_time = [e["kind"] for e in report["events"] if e["at"] == action["at"]]
    assert same_time == ["actions", "receipts"]
    assert action["policies"]["no_worse"]["transitions"] == {"counterfactual_assumed_open": 1}
    assert events(report, "actions")[1]["policies"]["no_worse"]["transitions"] == {"simultaneous_action_time_ambiguous": 2}
    assert report["policies"]["no_worse"]["pending"] == 2
    assert report["policies"]["no_worse"]["won"] == 0


def test_selected_exact_id_across_sessions_is_blocked_not_merged(tmp_path, monkeypatch):
    first = build(tmp_path / "first")
    shift_fixture_clock(monkeypatch, seconds=60)
    second = build(tmp_path / "second", [])
    report = interleaved.replay([first, second])
    counts = [e["policies"]["no_worse"]["transitions"] for e in events(report, "actions")]
    assert counts == [{"counterfactual_assumed_open": 1}, {"counterfactual_assumed_open": 1},
                      {"cross_session_exact_id_ambiguous": 1}, {"cross_session_exact_id_ambiguous": 1}]
    assert report["policies"]["no_worse"]["opened"] == 2


def test_cross_session_prior_result_blocks_later_candidate(tmp_path, monkeypatch):
    first = build(tmp_path / "first", results() + [later_result("new-a", finished=90, updated=110)])
    shift_fixture_clock(monkeypatch, prefix="new-", seconds=200)
    second = build(tmp_path / "second", [later_result("new-a", finished=90, updated=110),
                                          later_result("new-b", finished=90, updated=110)])
    report = interleaved.replay([first, second])
    later = events(report, "actions")[-2:]
    assert all(e["policies"]["no_worse"]["transitions"] == {"prior_result_seen": 1} for e in later)
    assert report["policies"]["no_worse"]["opened"] == 2


def test_cross_session_result_seen_between_actions_blocks_second_and_quarantines(tmp_path, monkeypatch):
    first = build(tmp_path / "first")
    shift_fixture_clock(monkeypatch, prefix="new-", seconds=60)
    contradiction = later_result("a", "away", finished=0, updated=1)
    second = custom(tmp_path / "second", first_results=[contradiction],
                    dated=[later_result("new-a", finished=90, updated=110),
                           later_result("new-b", finished=90, updated=110)])
    report = interleaved.replay([first, second])
    actions = events(report, "actions")
    assert actions[1]["policies"]["no_worse"]["transitions"] == {"prior_result_seen": 1}
    assert any(e["quarantined_total"] >= 1 for e in events(report, "receipts"))
    # First ticket remains unresolved; it is neither a win nor a loss.
    state = report["policies"]["no_worse"]
    assert state["pending"] == 1 and state["won"] == 2 and state["lost"] == 0


def test_cross_session_conflict_between_freeze_and_next_request_cannot_be_ignored(tmp_path, monkeypatch):
    first = build(tmp_path / "first")
    # First action freezes at t=43, its next result request begins at t=44;
    # a different session receives the conflicting result at t=43.5.
    shift_fixture_clock(monkeypatch, prefix="new-", seconds=40.5)
    conflicting = later_result("a", "away", finished=2, updated=2)
    second = custom(tmp_path / "second", first_results=[conflicting], dated=[])
    report = interleaved.replay([first, second])
    assert events(report, "actions")[0]["policies"]["no_worse"]["transitions"] == {
        "counterfactual_assumed_open": 1}
    assert events(report, "actions")[1]["policies"]["no_worse"]["transitions"] == {
        "prior_result_seen": 1}
    assert any(e["quarantined_total"] >= 1 for e in events(report, "receipts"))
    state = report["policies"]["no_worse"]
    assert state["won"] == state["lost"] == 0 and state["pending"] == 3


def test_earlier_consistent_unfinished_row_is_not_a_sporting_correction(tmp_path, monkeypatch):
    first = build(tmp_path / "first")
    shift_fixture_clock(monkeypatch, prefix="new-", seconds=40.5)
    live = later_result("a", finished=2, updated=2); live["status"] = "unknown"
    second = custom(tmp_path / "second", first_results=[live], dated=[])
    report = interleaved.replay([first, second])
    state = report["policies"]["no_worse"]
    assert state["held_at"] is None and state["won"] == 1 and state["pending"] == 2
    assert all(e["quarantined_total"] == 0 for e in events(report, "receipts"))


def test_later_generic_conflict_holds_previously_settled_book(tmp_path, monkeypatch):
    first = build(tmp_path / "first")
    shift_fixture_clock(monkeypatch, prefix="new-", seconds=200)
    contradiction = later_result("a", "away", finished=-110, updated=0)
    second = custom(tmp_path / "second", first_results=[contradiction],
                    dated=[later_result("new-a", finished=90, updated=110),
                           later_result("new-b", finished=90, updated=110)])
    report = interleaved.replay([first, second])
    state = report["policies"]["no_worse"]
    assert state["held_at"] is not None and state["cash_excluding_cost"] is None
    assert state["won"] is state["lost"] is state["pending"] is None
    assert state["settled_before_hold"] == {"won": 2, "lost": 0}
    assert all(e["policies"]["no_worse"]["transitions"] == {"book_held_for_review": 1}
               for e in events(report, "actions")[-2:])


def test_later_invalid_generic_record_also_holds_the_book(tmp_path, monkeypatch):
    first = build(tmp_path / "first")
    shift_fixture_clock(monkeypatch, prefix="new-", seconds=200)
    # Same sporting fields as before, but updated_at predates finished_at.
    invalid = later_result("a", finished=-110, updated=-111)
    second = custom(tmp_path / "second", first_results=[invalid],
                    dated=[later_result("new-a", finished=90, updated=110),
                           later_result("new-b", finished=90, updated=110)])
    state = interleaved.replay([first, second])["policies"]["no_worse"]
    assert state["held_at"] is not None and state["cash_excluding_cost"] is None


def test_duplicate_generic_records_after_settlement_hold_the_book(tmp_path, monkeypatch):
    first = build(tmp_path / "first")
    shift_fixture_clock(monkeypatch, prefix="new-", seconds=200)
    same = later_result("a", finished=-110, updated=0)
    second = custom(tmp_path / "second", first_results=[same, deepcopy(same)],
                    dated=[later_result("new-a", finished=90, updated=110),
                           later_result("new-b", finished=90, updated=110)])
    state = interleaved.replay([first, second])["policies"]["no_worse"]
    assert state["held_at"] is not None and state["cash_excluding_cost"] is None


@pytest.mark.parametrize("correction", ["changed", "missing"])
def test_same_time_correction_holds_before_other_session_settlement(tmp_path, monkeypatch, correction):
    first = build(tmp_path / "first")
    changed = deepcopy(results()); changed[0] = later_result("a", "away", finished=90, updated=180)
    head, _, _ = add(first.session, first.journal, first.history, first.history_head,
                     changed if correction == "changed" else [], 1)
    first = SessionEvidence(first.session, first.journal, first.excluded, first.prior_head, first.history, head)
    shift_fixture_clock(monkeypatch, prefix="new-", seconds=60)
    second = build(tmp_path / "second", [later_result("new-a", finished=90, updated=110),
                                          later_result("new-b", finished=90, updated=110)])
    a = interleaved.replay([first, second]); b = interleaved.replay([second, first])
    assert a == b
    batch = next(e for e in events(a, "receipts") if e["dated_receipts"] == 2)
    assert batch["newly_quarantined"] == (1 if correction == "changed" else 2)
    assert batch["policies"]["no_worse"]["held_at"] == batch["at"]
    assert batch["policies"]["no_worse"]["settled_before_hold"] == {"won": 2, "lost": 0}


def test_same_time_finished_and_unfinished_source_rows_are_quarantined(tmp_path, monkeypatch):
    first = build(tmp_path / "first")
    shift_fixture_clock(monkeypatch, prefix="new-", seconds=157)
    live = later_result("a", finished=-67, updated=-47); live["status"] = "unknown"
    second = custom(tmp_path / "second", first_results=[live],
                    dated=[later_result("new-a", finished=90, updated=110),
                           later_result("new-b", finished=90, updated=110)])
    report = interleaved.replay([first, second])
    batch = next(e for e in events(report, "receipts") if e["count"] == 2 and e["dated_receipts"] == 1)
    assert batch["newly_quarantined"] == 1
    assert batch["policies"]["no_worse"]["won"] == 0
    assert report["policies"]["no_worse"]["pending"] == 2


def test_unrelated_dated_absence_does_not_invent_correction(tmp_path, monkeypatch):
    first = build(tmp_path / "first")
    shift_fixture_clock(monkeypatch, prefix="new-", seconds=200)
    second = build(tmp_path / "second", [later_result("new-a", finished=90, updated=110),
                                          later_result("new-b", finished=90, updated=110)])
    result = interleaved.replay([first, second])["policies"]["no_worse"]
    assert result["held_at"] is None and result["won"] == 4


def test_duplicate_pinned_history_and_tampering_fail_closed(tmp_path):
    item = build(tmp_path / "one")
    with pytest.raises(ValueError, match="duplicate pinned session history"):
        interleaved.replay([item, item])
    path = item.history / "0001.body.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        interleaved.replay([item])
