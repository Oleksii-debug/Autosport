from copy import deepcopy
from datetime import timedelta
import json

import pytest

from forward_portfolio_journal import freeze_action, freeze_proposal, known_prior_ids, proposal, settle_actions, verify_actions
from causal_h2h_revision_history import append_followup
from prospective_capture import capture_one
from test_causal_h2h_score import observation
from test_delayed_portfolio_replay import followup, later_result, set_session_results
from test_quote_delay_audit import BASE, match, stamp


HEAD = "a" * 64


def clock(*seconds):
    values = iter(BASE + timedelta(seconds=s) for s in seconds)
    return lambda: next(values)


def capture(session, seq, kind, seconds, rows):
    if kind == "odds":
        payload = {"server_time": stamp(seconds), "matches": rows}
    else:
        payload = {"sport": "table_tennis", "results": rows}
    return capture_one(session, seq, kind, fetch=lambda _: json.dumps(payload).encode(),
                       clock=clock(seconds, seconds + 1))


def setup(root):
    session, journal = root / "session", root / "journal"
    session.mkdir(); journal.mkdir()
    return session, journal


def step1(session, journal, *, first_results=(), second_prices=None, excluded=frozenset()):
    first = [match(name, 0) for name in "abc"]
    capture(session, 1, "odds", 0, first)
    capture(session, 1, "results", 2, list(first_results))
    freeze_proposal(session, journal, 1, excluded, HEAD, clock=clock(4, 5))
    second = second_prices or [match(name, 40) for name in "abc"]
    capture(session, 2, "odds", 40, second)
    freeze_action(session, journal, 1, excluded, HEAD, clock=clock(42, 43))


def step2(session, journal, *, second_results=(), excluded=frozenset()):
    capture(session, 2, "results", 44, list(second_results))
    freeze_proposal(session, journal, 2, excluded, HEAD, clock=clock(46, 47))
    capture(session, 3, "odds", 80, [match(name, 80) for name in "abc"])
    freeze_action(session, journal, 2, excluded, HEAD, clock=clock(82, 83))
    capture(session, 3, "results", 84, [])
    return verify_actions(session, journal, excluded, HEAD)


def test_two_anchored_transitions_use_one_paper_book_per_policy(tmp_path):
    session, journal = setup(tmp_path)
    step1(session, journal)
    result = step2(session, journal)
    assert [e["category"] for e in result["decisions"]] == ["at_least_100_percent"] * 2
    for p in result["policies"].values():
        assert p["assumed_open"] == 2 and p["open_stake"] == "2"
        assert p["peak_tickets_sharing_fixture"] == 2
        assert p["assumed_cost_if_opened"] == "0.02"
    assert result["no_bet"]["assumed_open"] == 0
    assert result["outcomes_evaluated"] is result["real_money_execution"] is False


def test_known_prior_and_already_received_results_are_excluded(tmp_path):
    session, journal = setup(tmp_path)
    step1(session, journal, first_results=[{"fixture_id": "a"}], excluded={"b"})
    result = step2(session, journal, excluded={"b"})
    assert result["decisions"][0]["fixture_ids"] == ["c"]
    assert result["decisions"][0]["category"] == "no_pair"
    assert result["policies"]["no_worse"]["assumed_open"] == 0


def test_later_results_do_not_rewrite_earlier_action(tmp_path):
    session, journal = setup(tmp_path)
    step1(session, journal)
    capture(session, 2, "results", 44, [{"fixture_id": "a", "status": "finished"}])
    earlier = verify_actions(session, journal, frozenset(), HEAD, through=1)
    freeze_proposal(session, journal, 2, frozenset(), HEAD, clock=clock(46, 47))
    capture(session, 3, "odds", 80, [match(name, 80) for name in "abc"])
    freeze_action(session, journal, 2, frozenset(), HEAD, clock=clock(82, 83))
    capture(session, 3, "results", 84, [{"fixture_id": "b", "status": "finished"}])
    final = verify_actions(session, journal, frozenset(), HEAD)
    assert final["decisions"][0] == earlier["decisions"][0]
    assert final["decisions"][1]["fixture_ids"] == ["b", "c"]
    assert final["policies"]["no_worse"]["peak_tickets_sharing_fixture"] == 2
    set_session_results(session, 3, [{"fixture_id": "a", "status": "different future"}])
    assert verify_actions(session, journal, frozenset(), HEAD) == final


def test_two_thresholds_treat_price_deterioration_differently(tmp_path):
    session, journal = setup(tmp_path)
    prices = [match("a", 40, 1.48, 2.5), match("b", 40), match("c", 40)]
    step1(session, journal, second_prices=prices)
    capture(session, 2, "results", 44, [])
    result = verify_actions(session, journal, frozenset(), HEAD, through=1)
    assert result["decisions"][0]["category"] == "from_98_to_below_100_percent"
    assert result["policies"]["no_worse"]["assumed_open"] == 0
    assert result["policies"]["tolerance_2pct"]["assumed_open"] == 1


@pytest.mark.parametrize("bad", ["suspended", "changed_id", "missing"])
def test_unavailable_later_quote_does_not_create_ticket(tmp_path, bad):
    session, journal = setup(tmp_path)
    prices = [match(name, 40) for name in "abc"]
    if bad == "suspended": prices[0]["quotes"][0]["suspended"] = True
    elif bad == "changed_id": prices[0]["quotes"][0]["sel_id"] = "different"
    else: prices.pop(0)
    step1(session, journal, second_prices=prices)
    capture(session, 2, "results", 44, [])
    result = verify_actions(session, journal, frozenset(), HEAD, through=1)
    assert result["decisions"][0]["category"] == "missing_or_invalid"
    assert all(v["assumed_open"] == 0 for v in result["policies"].values())


def test_proposal_and_action_cannot_be_created_after_next_data(tmp_path):
    session, journal = setup(tmp_path)
    capture(session, 1, "odds", 0, [match(x, 0) for x in "ab"])
    capture(session, 1, "results", 2, [])
    capture(session, 2, "odds", 40, [match(x, 40) for x in "ab"])
    with pytest.raises(ValueError, match="before next odds"):
        freeze_proposal(session, journal, 1, frozenset(), HEAD, clock=clock(42, 43))
    capture(session, 2, "results", 44, [])
    with pytest.raises(ValueError, match="before next results"):
        freeze_action(session, journal, 1, frozenset(), HEAD, clock=clock(46, 47))


def test_journal_tamper_and_prior_set_change_fail_closed(tmp_path):
    session, journal = setup(tmp_path)
    step1(session, journal)
    capture(session, 2, "results", 44, [])
    with pytest.raises(ValueError, match="causal source selection"):
        verify_actions(session, journal, {"a"}, HEAD, through=1)
    path = journal / "action-0001.body.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="receipt mismatch"):
        verify_actions(session, journal, frozenset(), HEAD, through=1)


def test_proposal_receipt_must_precede_next_odds_request(tmp_path):
    session, journal = setup(tmp_path)
    step1(session, journal)
    capture(session, 2, "results", 44, [])
    path = journal / "proposal-0001.receipt.json"
    receipt = json.loads(path.read_text())
    receipt["frozen_at"] = stamp(40)
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="before next odds"):
        verify_actions(session, journal, frozenset(), HEAD, through=1)


def test_action_receipt_must_precede_next_results_request(tmp_path):
    session, journal = setup(tmp_path)
    step1(session, journal)
    capture(session, 2, "results", 44, [])
    path = journal / "action-0001.receipt.json"
    receipt = json.loads(path.read_text())
    receipt["frozen_at"] = stamp(44)
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="before next results"):
        verify_actions(session, journal, frozenset(), HEAD, through=1)


def test_only_past_result_snapshots_determine_proposal(tmp_path):
    session, journal = setup(tmp_path)
    step1(session, journal)
    capture(session, 2, "results", 44, [{"fixture_id": "a"}])
    before = proposal(session, 1, frozenset(), HEAD)
    set_session_results(session, 2, [{"fixture_id": "b"}, {"fixture_id": "c"}])
    assert proposal(session, 1, frozenset(), HEAD) == before
    assert [x["fixture_id"] for x in proposal(session, 2, frozenset(), HEAD)["selected"]] == ["a"]


def test_snapshot_delay_outside_window_creates_no_fill(tmp_path):
    session, journal = setup(tmp_path)
    capture(session, 1, "odds", 0, [match(x, 0) for x in "ab"])
    capture(session, 1, "results", 2, [])
    freeze_proposal(session, journal, 1, frozenset(), HEAD, clock=clock(4, 5))
    capture(session, 2, "odds", 75, [match(x, 75) for x in "ab"])
    freeze_action(session, journal, 1, frozenset(), HEAD, clock=clock(77, 78))
    capture(session, 2, "results", 79, [])
    result = verify_actions(session, journal, frozenset(), HEAD, through=1)
    assert result["decisions"][0]["category"] == "outside_30_60_seconds"
    assert all(p["assumed_open"] == 0 for p in result["policies"].values())


def test_freeze_must_not_run_long_after_quote(tmp_path):
    session, journal = setup(tmp_path)
    capture(session, 1, "odds", 0, [match(x, 0) for x in "ab"])
    capture(session, 1, "results", 2, [])
    with pytest.raises(ValueError, match="more than 30 seconds"):
        freeze_proposal(session, journal, 1, frozenset(), HEAD, clock=clock(32, 33))


def test_action_must_not_run_long_after_quote(tmp_path):
    session, journal = setup(tmp_path)
    capture(session, 1, "odds", 0, [match(x, 0) for x in "ab"])
    capture(session, 1, "results", 2, [])
    freeze_proposal(session, journal, 1, frozenset(), HEAD, clock=clock(4, 5))
    capture(session, 2, "odds", 40, [match(x, 40) for x in "ab"])
    with pytest.raises(ValueError, match="more than 30 seconds"):
        freeze_action(session, journal, 1, frozenset(), HEAD, clock=clock(72, 73))


def test_known_prior_ids_use_pinned_history_and_observed_odds(tmp_path):
    prior = tmp_path / "prior"; prior.mkdir()
    observation(prior, ids=("old",))
    history = tmp_path / "history"; history.mkdir()
    raw, receipt = followup([later_result("known")])
    head = append_followup(prior, history, raw, json.dumps(receipt).encode(), expected_head=None)
    ids, available = known_prior_ids(prior, history, head)
    assert ids == {"old", "known"} and available == receipt["received_at"]
    with pytest.raises(ValueError, match="externally pinned"):
        known_prior_ids(prior, history, "b" * 64)


@pytest.mark.parametrize("winner, expected", [("home", "won"), ("away", "lost")])
def test_outcomes_settle_after_receipt_without_rewriting_actions(tmp_path, winner, expected):
    session, journal = setup(tmp_path)
    step1(session, journal)
    actions = step2(session, journal)
    raw, receipt = followup([later_result("a", winner, finished=90, updated=110),
                             later_result("b", finished=90, updated=110)], requested=150, received=160)
    report = settle_actions(session, journal, frozenset(), HEAD, raw, receipt)
    assert report["actions"] == actions
    for policy in report["policies"].values():
        assert policy[expected] == 2
        assert policy["pending"] == 0
    assert report["real_money_execution"] is report["profitability_claim"] is False


@pytest.mark.parametrize("condition", ["missing", "before_bound", "future_update", "missing_final"])
def test_unverified_or_missing_leg_remains_pending(tmp_path, condition):
    session, journal = setup(tmp_path)
    step1(session, journal)
    step2(session, journal)
    a = later_result("a", finished=90, updated=110)
    b = later_result("b", finished=90, updated=110)
    if condition == "missing": rows = [a]
    else:
        if condition == "before_bound": b["finished_at"] = stamp(30)
        elif condition == "future_update": b["updated_at"] = stamp(161)
        else: b["final_score"] = None
        rows = [a, b]
    report = settle_actions(session, journal, frozenset(), HEAD, *followup(rows, requested=150, received=160))
    for policy in report["policies"].values():
        assert policy["pending"] == 2
        assert policy["won"] == policy["lost"] == 0
        assert policy["open_stake"] == "2"


def test_conflicting_earlier_source_outcome_never_settles(tmp_path):
    session, journal = setup(tmp_path)
    step1(session, journal)
    step2(session, journal)
    conflicting = later_result("a", "away", finished=80, updated=84.5)
    set_session_results(session, 3, [conflicting])
    rows = [later_result("a", "home", finished=90, updated=110),
            later_result("b", finished=90, updated=110)]
    report = settle_actions(session, journal, frozenset(), HEAD, *followup(rows, requested=150, received=160))
    assert report["policies"]["no_worse"]["pending"] == 2
    assert report["policies"]["no_worse"]["lost"] == 0
