from copy import deepcopy
import json

import pytest

from causal_h2h_longitudinal import compare
from causal_h2h_revision_history import append_followup, evaluate_history
from test_causal_h2h_score import observation
from test_delayed_portfolio_replay import followup, later_result
from test_quote_delay_audit import stamp


def setup_history(root, ids=("a",)):
    observation(root, ids=ids)
    history = root / "history"
    history.mkdir()
    return history


def add(root, history, head, rows, sequence):
    raw, receipt = followup(rows, requested=90 + 60 * sequence, received=100 + 60 * sequence)
    receipt_bytes = (json.dumps(receipt, sort_keys=True) + "\n").encode()
    new_head = append_followup(root, history, raw, receipt_bytes, expected_head=head)
    return new_head, raw, receipt


def test_history_matches_prior_two_receipt_adapter_and_preserves_prefix(tmp_path):
    history = setup_history(tmp_path, ids=("a", "b", "c"))
    old = later_result("a")
    head, raw1, receipt1 = add(tmp_path, history, None, [old], 0)
    head, raw2, receipt2 = add(tmp_path, history, head, [old, later_result("b", "away")], 1)
    prefix = evaluate_history(tmp_path, history, head)
    pair = compare(tmp_path, raw1, receipt1, raw2, receipt2)
    assert prefix["snapshots"][-1]["counts"] == {"eligible": 2, "quarantined": 0, "unresolved": 1}
    assert prefix["snapshots"][-1]["market"] == pair["current_market"]
    assert prefix["snapshots"][-1]["neutral"] == pair["current_neutral"]
    head, _, _ = add(tmp_path, history, head, [old, later_result("b", "away"), later_result("c")], 2)
    full = evaluate_history(tmp_path, history, head)
    assert full["snapshots"][:2] == prefix["snapshots"]
    assert full["snapshots"][-1]["counts"]["eligible"] == 3
    assert full["real_money_execution"] is full["model_training_authorized"] is False


@pytest.mark.parametrize("revision", ["winner", "same_winner_score", "name", "last_live", "future_update"])
def test_revision_then_reversion_stays_quarantined(tmp_path, revision):
    history = setup_history(tmp_path)
    old = later_result("a")
    changed = deepcopy(old)
    if revision == "winner":
        changed = later_result("a", "away")
    elif revision == "same_winner_score":
        changed["periods"][0] = {"score": "11-3"}
        changed["final_score"] = "11-3,11-2,11-2"
    elif revision == "name":
        changed["home"] = "other"
    elif revision == "last_live":
        changed["finished_at"] = stamp(61)
    else:
        changed["updated_at"] = stamp(999)
    head, _, _ = add(tmp_path, history, None, [old], 0)
    head, _, _ = add(tmp_path, history, head, [changed], 1)
    head, _, _ = add(tmp_path, history, head, [old], 2)
    report = evaluate_history(tmp_path, history, head)
    assert [s["counts"]["eligible"] for s in report["snapshots"]] == [1, 0, 0]
    assert [s["counts"]["quarantined"] for s in report["snapshots"]] == [0, 1, 1]
    assert report["snapshots"][-1]["market"] is None
    assert report["snapshots"][-1]["records"][0]["quarantined_at"] == stamp(160)
    assert report["snapshots"][0]["market"]["brier"] == pytest.approx(.0625)


def test_disappeared_result_does_not_requalify_on_reappearance(tmp_path):
    history = setup_history(tmp_path)
    a = later_result("a")
    head, _, _ = add(tmp_path, history, None, [a], 0)
    head, _, _ = add(tmp_path, history, head, [], 1)
    head, _, _ = add(tmp_path, history, head, [a], 2)
    report = evaluate_history(tmp_path, history, head)
    assert report["snapshots"][1]["records"][0]["reason"] == "previously_eligible_missing"
    assert report["snapshots"][2]["counts"] == {"eligible": 0, "quarantined": 1, "unresolved": 0}
    assert report["snapshots"][2]["market"] is None


def test_finished_invalid_then_missing_then_repaired_stays_quarantined(tmp_path):
    history = setup_history(tmp_path)
    invalid = later_result("a")
    invalid["final_score"] = None
    head, _, _ = add(tmp_path, history, None, [invalid], 0)
    head, _, _ = add(tmp_path, history, head, [], 1)
    head, _, _ = add(tmp_path, history, head, [later_result("a")], 2)
    report = evaluate_history(tmp_path, history, head)
    assert [s["counts"]["unresolved"] for s in report["snapshots"]] == [1, 1, 0]
    assert report["snapshots"][-1]["records"][0]["reason"] == "earlier_finished_ineligible"
    assert report["snapshots"][-1]["market"] is None


def test_unfinished_can_transition_to_eligible_without_backdating(tmp_path):
    history = setup_history(tmp_path)
    old = later_result("a")
    old["status"] = "live"
    head, _, _ = add(tmp_path, history, None, [old], 0)
    head, _, receipt = add(tmp_path, history, head, [later_result("a")], 1)
    report = evaluate_history(tmp_path, history, head)
    assert report["snapshots"][0]["market"] is None
    assert report["snapshots"][1]["records"][0]["first_eligible_at"] == receipt["received_at"]
    assert report["snapshots"][1]["counts"]["eligible"] == 1


def test_only_update_timestamp_can_change(tmp_path):
    history = setup_history(tmp_path)
    head, _, _ = add(tmp_path, history, None, [later_result("a")], 0)
    head, _, _ = add(tmp_path, history, head, [later_result("a", updated=120)], 1)
    report = evaluate_history(tmp_path, history, head)
    assert report["snapshots"][-1]["counts"]["eligible"] == 1


@pytest.mark.parametrize("piece", ["body", "receipt", "link"])
def test_tampering_any_recorded_piece_fails_closed(tmp_path, piece):
    history = setup_history(tmp_path)
    head, _, _ = add(tmp_path, history, None, [later_result("a")], 0)
    head, _, _ = add(tmp_path, history, head, [later_result("a")], 1)
    path = history / f"0001.{piece}.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        evaluate_history(tmp_path, history, head)


def test_interrupted_triplet_and_unexpected_files_fail_closed(tmp_path):
    history = setup_history(tmp_path)
    head, _, _ = add(tmp_path, history, None, [later_result("a")], 0)
    (history / "0002.body.json").write_bytes(b"partial")
    with pytest.raises(ValueError, match="incomplete"):
        evaluate_history(tmp_path, history, head)
    (history / "0002.body.json").unlink()
    (history / "unexpected").write_text("x")
    with pytest.raises(ValueError, match="unexpected"):
        evaluate_history(tmp_path, history, head)


def test_missing_middle_and_latest_rollback_rejected_against_latest_pin(tmp_path):
    history = setup_history(tmp_path)
    head, _, _ = add(tmp_path, history, None, [], 0)
    head, _, _ = add(tmp_path, history, head, [], 1)
    head, _, _ = add(tmp_path, history, head, [], 2)
    for kind in ("body", "receipt", "link"):
        (history / f"0002.{kind}.json").unlink()
    with pytest.raises(ValueError, match="sequence gap"):
        evaluate_history(tmp_path, history, head)
    for kind in ("body", "receipt", "link"):
        (history / f"0003.{kind}.json").unlink()
    with pytest.raises(ValueError, match="externally pinned"):
        evaluate_history(tmp_path, history, head)


def test_stale_pin_and_overlapping_receipt_never_append(tmp_path):
    history = setup_history(tmp_path)
    head, raw, receipt = add(tmp_path, history, None, [later_result("a")], 0)
    before = sorted(p.name for p in history.iterdir())
    with pytest.raises(ValueError, match="externally pinned"):
        append_followup(tmp_path, history, raw, json.dumps(receipt).encode(), expected_head="a" * 64)
    with pytest.raises(ValueError, match="strictly after"):
        append_followup(tmp_path, history, raw, json.dumps(receipt).encode(), expected_head=head)
    assert sorted(p.name for p in history.iterdir()) == before


def test_duplicate_fixture_id_is_rejected_before_append(tmp_path):
    history = setup_history(tmp_path)
    a = later_result("a")
    raw, receipt = followup([a, deepcopy(a)])
    with pytest.raises(ValueError, match="duplicate fixture ID"):
        append_followup(tmp_path, history, raw, json.dumps(receipt).encode(), expected_head=None)
    assert not list(history.iterdir())
