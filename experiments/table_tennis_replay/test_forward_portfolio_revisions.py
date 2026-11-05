from copy import deepcopy
import json

import pytest

from forward_portfolio_journal import settle_actions
from forward_portfolio_revisions import append_followup, evaluate_history
from test_delayed_portfolio_replay import followup, later_result
from test_forward_portfolio_journal import HEAD, setup, step1, step2
from test_quote_delay_audit import stamp


def fixture(root, *, second_results=()):
    session, journal = setup(root)
    step1(session, journal)
    step2(session, journal, second_results=second_results)
    history = root / "dated-history"; history.mkdir()
    return session, journal, history


def add(session, journal, history, head, rows, step):
    raw, receipt = followup(rows, requested=150 + 60 * step, received=160 + 60 * step)
    body = (json.dumps(receipt, sort_keys=True) + "\n").encode()
    new = append_followup(session, journal, frozenset(), HEAD, history, raw, body, expected_head=head)
    return new, raw, receipt


def results(a="home", b="home", updated=110):
    return [later_result("a", a, finished=90, updated=updated),
            later_result("b", b, finished=90, updated=updated)]


def test_initial_pending_then_settled_matches_existing_authority(tmp_path):
    session, journal, history = fixture(tmp_path)
    head, _, _ = add(session, journal, history, None, [], 0)
    prefix = evaluate_history(session, journal, frozenset(), HEAD, history, head)
    head, raw, receipt = add(session, journal, history, head, results(updated=180), 1)
    report = evaluate_history(session, journal, frozenset(), HEAD, history, head)
    baseline = settle_actions(session, journal, frozenset(), HEAD, raw, receipt)
    assert report["snapshots"][:1] == prefix["snapshots"]
    assert report["snapshots"][0]["policies"]["no_worse"]["pending"] == 2
    assert report["snapshots"][1]["fixture_counts"] == {"eligible": 2, "quarantined": 0, "unresolved": 0}
    assert report["snapshots"][1]["policies"] == baseline["policies"]
    assert report["snapshots"][1]["policies"]["no_worse"]["won"] == 2
    assert report["real_money_execution"] is report["profitability_claim"] is False


@pytest.mark.parametrize("case", ["winner", "same_winner_score", "participant", "league", "last_live", "status", "future_update"])
def test_revision_then_reversion_stays_quarantined(tmp_path, case):
    session, journal, history = fixture(tmp_path)
    old = results()
    changed = deepcopy(old)
    if case == "winner": changed[0] = later_result("a", "away", finished=90, updated=180)
    elif case == "same_winner_score":
        changed[0]["periods"][0] = {"score": "11-3"}
        changed[0]["final_score"] = "11-3,11-2,11-2"
        changed[0]["updated_at"] = stamp(180)
    elif case == "participant": changed[0]["home"] = "different"
    elif case == "league": changed[0]["league"] = "different"
    elif case == "last_live": changed[0]["finished_at"] = stamp(91)
    elif case == "status": changed[0]["status"] = "unknown"
    else: changed[0]["updated_at"] = stamp(221)
    head, _, _ = add(session, journal, history, None, old, 0)
    previous = evaluate_history(session, journal, frozenset(), HEAD, history, head)
    head, _, _ = add(session, journal, history, head, changed, 1)
    head, _, _ = add(session, journal, history, head, old, 2)
    report = evaluate_history(session, journal, frozenset(), HEAD, history, head)
    assert report["snapshots"][:1] == previous["snapshots"]
    assert report["snapshots"][0]["policies"]["no_worse"]["won"] == 2
    for snapshot in report["snapshots"][1:]:
        assert snapshot["fixture_counts"] == {"eligible": 1, "quarantined": 1, "unresolved": 0}
        assert snapshot["policies"]["no_worse"]["pending"] == 2
        assert snapshot["policies"]["no_worse"]["won"] == snapshot["policies"]["no_worse"]["lost"] == 0


def test_disappearance_then_return_never_silently_resettles(tmp_path):
    session, journal, history = fixture(tmp_path)
    old = results()
    head, _, _ = add(session, journal, history, None, old, 0)
    head, _, _ = add(session, journal, history, head, [old[1]], 1)
    head, _, _ = add(session, journal, history, head, old, 2)
    report = evaluate_history(session, journal, frozenset(), HEAD, history, head)
    for snapshot in report["snapshots"][1:]:
        assert snapshot["fixture_counts"]["quarantined"] == 1
        assert snapshot["policies"]["no_worse"]["pending"] == 2
    assert report["snapshots"][-1]["fixture_records"][0]["reason"] == "previously_eligible_missing"


def test_invalid_finished_then_absent_then_repaired_is_quarantined(tmp_path):
    session, journal, history = fixture(tmp_path)
    bad = results(); bad[0]["final_score"] = None
    head, _, _ = add(session, journal, history, None, bad, 0)
    head, _, _ = add(session, journal, history, head, [bad[1]], 1)
    head, _, _ = add(session, journal, history, head, results(updated=230), 2)
    report = evaluate_history(session, journal, frozenset(), HEAD, history, head)
    assert report["snapshots"][0]["fixture_counts"] == {"eligible": 1, "quarantined": 0, "unresolved": 1}
    assert report["snapshots"][-1]["fixture_counts"] == {"eligible": 1, "quarantined": 1, "unresolved": 0}
    assert report["snapshots"][-1]["policies"]["no_worse"]["pending"] == 2


def test_inconsistent_earlier_unfinished_identity_stays_quarantined(tmp_path):
    session, journal, history = fixture(tmp_path)
    bad = results(); bad[0]["status"] = "unknown"; bad[0]["home"] = "other"
    head, _, _ = add(session, journal, history, None, bad, 0)
    head, _, _ = add(session, journal, history, head, results(updated=180), 1)
    report = evaluate_history(session, journal, frozenset(), HEAD, history, head)
    assert report["snapshots"][-1]["fixture_counts"]["quarantined"] == 1


def test_unfinished_matching_identity_can_become_eligible_later(tmp_path):
    session, journal, history = fixture(tmp_path)
    live = results(); live[0]["status"] = "unknown"
    head, _, _ = add(session, journal, history, None, live, 0)
    head, _, receipt = add(session, journal, history, head, results(updated=180), 1)
    report = evaluate_history(session, journal, frozenset(), HEAD, history, head)
    assert report["snapshots"][-1]["fixture_counts"]["eligible"] == 2
    assert report["snapshots"][-1]["fixture_records"][0]["first_eligible_at"] == receipt["received_at"]
    assert report["snapshots"][-1]["policies"]["no_worse"]["won"] == 2


def test_updated_at_change_alone_is_not_a_sporting_correction(tmp_path):
    session, journal, history = fixture(tmp_path)
    head, _, _ = add(session, journal, history, None, results(), 0)
    head, _, _ = add(session, journal, history, head, results(updated=180), 1)
    report = evaluate_history(session, journal, frozenset(), HEAD, history, head)
    assert report["snapshots"][-1]["fixture_counts"]["eligible"] == 2
    assert report["snapshots"][-1]["policies"]["no_worse"]["won"] == 2


def test_shared_fixture_quarantine_blocks_both_tickets(tmp_path):
    prior = later_result("a", finished=44, updated=44.5)
    prior["status"] = "unknown"
    session, journal, history = fixture(tmp_path, second_results=[prior])
    initial = results() + [later_result("c", "home", finished=90, updated=110)]
    changed = deepcopy(initial); changed[1] = later_result("b", "away", finished=90, updated=180)
    head, _, _ = add(session, journal, history, None, initial, 0)
    head, _, _ = add(session, journal, history, head, changed, 1)
    report = evaluate_history(session, journal, frozenset(), HEAD, history, head)
    assert report["snapshots"][0]["policies"]["no_worse"]["won"] == 2
    assert report["snapshots"][-1]["fixture_counts"]["quarantined"] == 1
    assert report["snapshots"][-1]["policies"]["no_worse"]["pending"] == 2


@pytest.mark.parametrize("piece", ["body", "receipt", "link"])
def test_altered_history_bytes_are_rejected(tmp_path, piece):
    session, journal, history = fixture(tmp_path)
    head, _, _ = add(session, journal, history, None, results(), 0)
    head, _, _ = add(session, journal, history, head, results(updated=180), 1)
    path = history / f"0001.{piece}.json";path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        evaluate_history(session, journal, frozenset(), HEAD, history, head)


def test_latest_pin_detects_rollback_and_stale_append(tmp_path):
    session, journal, history = fixture(tmp_path)
    first, _, _ = add(session, journal, history, None, results(), 0)
    head, raw, receipt = add(session, journal, history, first, results(updated=180), 1)
    with pytest.raises(ValueError, match="externally pinned"):
        append_followup(session, journal, frozenset(), HEAD, history, raw, json.dumps(receipt).encode(), expected_head=first)
    for kind in ("body", "receipt", "link"):(history / f"0002.{kind}.json").unlink()
    with pytest.raises(ValueError, match="externally pinned"):
        evaluate_history(session, journal, frozenset(), HEAD, history, head)


def test_partial_write_and_out_of_order_receipts_are_rejected(tmp_path):
    session, journal, history = fixture(tmp_path)
    head, raw, receipt = add(session, journal, history, None, results(), 0)
    with pytest.raises(ValueError, match="follow previous receipt"):
        append_followup(session, journal, frozenset(), HEAD, history, raw, json.dumps(receipt).encode(), expected_head=head)
    (history / "0002.body.json").write_bytes(b"partial")
    with pytest.raises(ValueError, match="incomplete"):
        evaluate_history(session, journal, frozenset(), HEAD, history, head)
