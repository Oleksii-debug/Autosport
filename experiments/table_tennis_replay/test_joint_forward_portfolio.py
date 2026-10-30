from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json

import pytest

import joint_forward_portfolio as joint
import test_forward_portfolio_journal as journal_test
import test_quote_delay_audit as quote_test
from joint_forward_portfolio import SessionEvidence, replay
from test_delayed_portfolio_replay import later_result
from test_forward_portfolio_journal import HEAD
from test_forward_portfolio_revisions import add, fixture, results


def evidence(session, journal, history, head):
    return SessionEvidence(session, journal, frozenset(), HEAD, history, head)


def build(root, rows=None):
    root.mkdir()
    session, journal, history = fixture(root)
    head, _, _ = add(session, journal, history, None, rows if rows is not None else results(), 0)
    return evidence(session, journal, history, head)


def shift_fixture_clock(monkeypatch, *, prefix="", seconds=1000):
    shifted = quote_test.BASE + timedelta(seconds=seconds)
    original_match = quote_test.match
    monkeypatch.setattr(quote_test, "BASE", shifted)
    monkeypatch.setattr(journal_test, "BASE", shifted)
    if prefix:
        monkeypatch.setattr(journal_test, "match",
                            lambda identity, seconds, *args: original_match(prefix + identity, seconds, *args))


def test_single_session_reuses_book_and_measures_shared_open_legs(tmp_path):
    item = build(tmp_path / "one")
    report = replay([item])
    for result in report["policies"].values():
        assert (result["opened"], result["won"], result["lost"], result["pending"]) == (2, 2, 0, 0)
        assert result["peak_open_tickets"] == result["peak_tickets_sharing_exact_id"] == 2
        assert Decimal(result["cash_excluding_cost"]) == Decimal("102.50")
        assert Decimal(result["net_cost_stressed_pending_at_face_value"]) == Decimal("2.48")
    assert report["frozen_actions_were_originally_separate_books"] is True
    assert report["real_money_execution"] is report["profitability_claim"] is False
    assert "fixture_id" not in json.dumps(report) and '"aA"' not in json.dumps(report)


def test_two_sessions_use_one_balance_and_account_each_ticket_once(tmp_path, monkeypatch):
    first = build(tmp_path / "first")
    shift_fixture_clock(monkeypatch, prefix="new-")
    second = build(tmp_path / "second", [later_result("new-a", "away", finished=90, updated=110),
                                         later_result("new-b", "home", finished=90, updated=110)])
    report = replay([first, second])
    for result in report["policies"].values():
        assert (result["opened"], result["won"], result["lost"], result["pending"]) == (4, 2, 2, 0)
        assert Decimal(result["cash_excluding_cost"]) == Decimal("100.50")
        assert Decimal(result["net_cost_stressed_pending_at_face_value"]) == Decimal("0.46")
        assert Decimal(result["assumed_cost"]) == Decimal("0.04")
    second_first = next(e for e in report["events"] if e["session"] == 2 and e["kind"] == "action")
    assert Decimal(second_first["policies"]["no_worse"]["cash_excluding_cost"]) == Decimal("101.50")


def test_shared_risk_uses_earlier_losses_with_tightened_test_reserve(tmp_path, monkeypatch):
    first = build(tmp_path / "first", results(a="away"))
    shift_fixture_clock(monkeypatch, prefix="new-")
    second = build(tmp_path / "second", [later_result("new-a", finished=90, updated=110),
                                          later_result("new-b", finished=90, updated=110)])
    monkeypatch.setattr(joint, "INITIAL", Decimal("50"))
    risk_class = joint.PaperRiskPolicy
    monkeypatch.setattr(joint, "PaperRiskPolicy", lambda: risk_class(minimum_cash_reserve_fraction=Decimal("0.95")))
    report = replay([first, second])
    second_actions = [e for e in report["events"] if e["session"] == 2 and e["kind"] == "action"]
    assert [e["policies"]["no_worse"]["transition"] for e in second_actions] == [
        "shared_book_risk_rejected", "shared_book_risk_rejected"]
    assert report["policies"]["no_worse"]["opened"] == 2
    assert report["policies"]["no_worse"]["lost"] == 2


def test_later_result_correction_holds_book_without_rewriting_earlier_events(tmp_path):
    item = build(tmp_path / "one")
    prefix = replay([item])
    revised = deepcopy(results()); revised[0] = later_result("a", "away", finished=90, updated=180)
    head, _, _ = add(item.session, item.journal, item.history, item.history_head, revised, 1)
    changed = SessionEvidence(item.session, item.journal, item.excluded, item.prior_head, item.history, head)
    result = replay([changed])
    assert result["events"][:len(prefix["events"])] == prefix["events"]
    for state in result["policies"].values():
        assert state["held_at"] is not None
        assert state["cash_excluding_cost"] is state["pending_stake"] is None
        assert state["won"] is state["lost"] is state["pending"] is None
        assert state["settled_before_hold"] == {"won": 2, "lost": 0}
        assert state["net_cost_stressed_pending_at_face_value"] is None
    assert result["events"][-1]["policies"]["no_worse"]["transition"] == \
           "book_held_after_settled_result_correction"


def test_held_book_refuses_later_session_actions(tmp_path, monkeypatch):
    item = build(tmp_path / "first")
    changed = deepcopy(results()); changed[0] = later_result("a", "away", finished=90, updated=180)
    head, _, _ = add(item.session, item.journal, item.history, item.history_head, changed, 1)
    first = SessionEvidence(item.session, item.journal, item.excluded, item.prior_head, item.history, head)
    shift_fixture_clock(monkeypatch, prefix="new-")
    second = build(tmp_path / "second", [later_result("new-a", finished=90, updated=110),
                                          later_result("new-b", finished=90, updated=110)])
    report = replay([first, second])
    actions = [e for e in report["events"] if e["session"] == 2 and e["kind"] == "action"]
    assert [e["policies"]["no_worse"]["transition"] for e in actions] == [
        "book_held_for_review", "book_held_for_review"]
    assert report["policies"]["no_worse"]["cash_excluding_cost"] is None


def test_future_result_does_not_change_frozen_actions(tmp_path):
    win = build(tmp_path / "win", results())
    lose = build(tmp_path / "lose", results(a="away"))
    first = replay([win]); second = replay([lose])
    assert [e for e in first["events"] if e["kind"] == "action"] == \
           [e for e in second["events"] if e["kind"] == "action"]
    assert first["policies"]["no_worse"]["won"] == 2
    assert second["policies"]["no_worse"]["lost"] == 2


def test_pending_quarantined_leg_never_becomes_a_loss(tmp_path):
    root = tmp_path / "one"; root.mkdir()
    session, journal, history = fixture(root)
    first = [later_result("a", finished=90, updated=110)]
    head, _, _ = add(session, journal, history, None, first, 0)
    head, _, _ = add(session, journal, history, head, [], 1)
    result = replay([evidence(session, journal, history, head)])["policies"]["no_worse"]
    assert (result["opened"], result["won"], result["lost"], result["pending"]) == (2, 0, 0, 2)
    assert result["held_at"] is None and Decimal(result["pending_stake"]) == Decimal(2)


def test_prior_session_result_blocks_later_exact_id_without_replacement(tmp_path, monkeypatch):
    old = results() + [later_result("new-a", finished=90, updated=110)]
    first = build(tmp_path / "first", old)
    shift_fixture_clock(monkeypatch, prefix="new-")
    second = build(tmp_path / "second", [later_result("new-a", finished=90, updated=110),
                                          later_result("new-b", finished=90, updated=110)])
    result = replay([first, second])
    transitions = [e["policies"]["no_worse"]["transition"] for e in result["events"]
                   if e["session"] == 2 and e["kind"] == "action"]
    assert transitions == ["prior_session_result_seen", "prior_session_result_seen"]
    assert result["policies"]["no_worse"]["opened"] == 2


def test_session_overlap_and_cross_session_id_reuse_fail_closed(tmp_path, monkeypatch):
    first = build(tmp_path / "first")
    concurrent = build(tmp_path / "concurrent")
    with pytest.raises(ValueError, match="windows must not overlap"):
        replay([first, concurrent])
    shift_fixture_clock(monkeypatch)
    reused = build(tmp_path / "reused")
    with pytest.raises(ValueError, match="ID reused across sessions"):
        replay([first, reused])


def test_touching_capture_and_receipt_times_are_not_assumed_ordered(tmp_path, monkeypatch):
    first = build(tmp_path / "first")  # first dated receipt is at second 160
    shift_fixture_clock(monkeypatch, prefix="new-", seconds=160)
    second = build(tmp_path / "second", [later_result("new-a", finished=90, updated=110),
                                          later_result("new-b", finished=90, updated=110)])
    with pytest.raises(ValueError, match="windows must not overlap"):
        replay([first, second])


def test_tampered_history_and_bad_prior_identity_fail_closed(tmp_path):
    item = build(tmp_path / "one")
    path = item.history / "0001.body.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        replay([item])
    with pytest.raises(ValueError, match="unusable prior result identity"):
        joint._source_ids(b'{"results":[{"fixture_id":123}]}')


def test_unexpected_session_file_is_preserved_and_stops_replay(tmp_path):
    item = build(tmp_path / "one")
    partial = item.session / "partial-evidence"
    partial.write_bytes(b"do not silently discard")
    with pytest.raises(ValueError, match="unexpected evidence entry"):
        replay([item])
    assert partial.read_bytes() == b"do not silently discard"


def test_quote_age_is_checked_at_frozen_action_time(tmp_path):
    item = build(tmp_path / "one")
    prepared = joint._prepare(item)
    when, _, seq, _, decision = next(e for e in prepared["events"] if e[3] == "action")
    state = joint._state()
    status = joint._open(state, prepared, "no_worse", seq, decision, when + timedelta(seconds=31), set())
    assert status == "quote_stale_at_frozen_action" and state["opened"] == 0
