"""Synthetic forecast and result-revision tests; not real source evidence."""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest

from causal_h2h_revision_history import evaluate_history as evaluate_legacy
from causal_strength_revision_history import append_followup, evaluate_history
from test_causal_strength_frozen import complete, dated, seconds, setup_frozen
from test_delayed_portfolio_replay import followup


def setup_history(root):
    frozen = complete(root, setup_frozen(root))
    pins = {"expected_forecast_sha256": frozen["sha256"],
            "expected_forecast_receipt_sha256": frozen["receipt_sha256"]}
    history = root / "history"
    history.mkdir()
    return history, pins


def rows():
    return [dated("full", "A", "B"), dated("form", "A", "E", "away"),
            dated("none", "noneA", "noneB")]


def add(root, directory, pins, head, results, sequence):
    raw, receipt = followup(results, requested=190 + 100 * sequence,
                            received=200 + 100 * sequence)
    receipt_bytes = (json.dumps(receipt, sort_keys=True) + "\n").encode()
    return append_followup(root, directory, raw, receipt_bytes, expected_head=head, **pins)


def test_asof_matched_cohorts_and_persistent_quarantine(tmp_path):
    history, pins = setup_history(tmp_path)
    original = rows()
    head = add(tmp_path, history, pins, None, original, 0)
    prefix = evaluate_history(tmp_path, history, head, **pins)
    changed = [dated("full", "A", "B", "away"), original[1]]  # none disappears
    head = add(tmp_path, history, pins, head, changed, 1)
    head = add(tmp_path, history, pins, head, original, 2)  # both revert/reappear
    report = evaluate_history(tmp_path, history, head, **pins)
    first, second, third = report["snapshots"]
    assert first == prefix["snapshots"][0]
    assert [s["counts"] for s in (first, second, third)] == [
        {"eligible": 3, "quarantined": 0, "unresolved": 0},
        {"eligible": 1, "quarantined": 2, "unresolved": 0},
        {"eligible": 1, "quarantined": 2, "unresolved": 0}]
    assert first["matched_form"]["fixture_ids"] == ["form", "full"]
    assert first["matched_h2h"]["fixture_ids"] == ["full"]
    assert second["matched_form"]["fixture_ids"] == third["matched_form"]["fixture_ids"] == ["form"]
    assert third["matched_h2h"]["matches"] == 0
    assert all(value is None for value in third["matched_h2h"]["scores"].values())
    assert first["matched_form"]["scores"]["market"]["brier"] == pytest.approx(0.265625)
    h2h = first["matched_h2h"]["scores"]
    assert h2h["market"]["brier"] == pytest.approx((3 / 8) ** 2)
    assert h2h["form"]["brier"] == pytest.approx((5 / 14) ** 2)
    assert h2h["form_h2h"]["brier"] == pytest.approx((25 / 84) ** 2)
    assert h2h["form_h2h"]["log_loss"] == pytest.approx(-math.log(59 / 84))
    assert third["matched_form"]["scores"]["market"]["brier"] == pytest.approx(0.625**2)
    records = {r["fixture_id"]: r for r in third["records"]}
    assert records["full"]["reason"] == "previously_eligible_changed"
    assert records["none"]["reason"] == "previously_eligible_missing"
    assert report["model_training_authorized"] is report["real_money_execution"] is False
    assert report["profitability_claim"] is report["independent_holdout"] is False
    link = json.loads((history / "0001.link.json").read_text())
    assert link["schema"] == "research.tt_result_history_link.v2"
    assert link["forecast_sha256"] == pins["expected_forecast_sha256"]
    assert link["forecast_receipt_sha256"] == pins["expected_forecast_receipt_sha256"]


@pytest.mark.parametrize("revision", ["same_winner_score", "side_name", "last_live", "future_update"])
def test_any_known_sporting_revision_and_reversion_stays_quarantined(tmp_path, revision):
    history, pins = setup_history(tmp_path)
    original = rows()[0]
    changed = deepcopy(original)
    if revision == "same_winner_score":
        changed["periods"][0] = {"score": "11-3"}
        changed["final_score"] = "11-3,11-2,11-2"
    elif revision == "side_name":
        changed["home"] = "other"
    elif revision == "last_live":
        changed["finished_at"] = seconds(161).isoformat()
    else:
        changed["updated_at"] = seconds(999).isoformat()
    head = add(tmp_path, history, pins, None, [original], 0)
    head = add(tmp_path, history, pins, head, [changed], 1)
    head = add(tmp_path, history, pins, head, [original], 2)
    report = evaluate_history(tmp_path, history, head, **pins)
    assert [s["counts"]["eligible"] for s in report["snapshots"]] == [1, 0, 0]
    assert [s["counts"]["quarantined"] for s in report["snapshots"]] == [0, 1, 1]
    assert report["snapshots"][-1]["matched_h2h"]["matches"] == 0


def test_invalid_finished_then_absence_then_repair_does_not_requalify(tmp_path):
    history, pins = setup_history(tmp_path)
    invalid = rows()[0]
    invalid["final_score"] = None
    head = add(tmp_path, history, pins, None, [invalid], 0)
    head = add(tmp_path, history, pins, head, [], 1)
    head = add(tmp_path, history, pins, head, [rows()[0]], 2)
    report = evaluate_history(tmp_path, history, head, **pins)
    last = report["snapshots"][-1]
    record = next(x for x in last["records"] if x["fixture_id"] == "full")
    assert record["reason"] == "earlier_finished_ineligible"
    assert last["counts"] == {"eligible": 0, "quarantined": 1, "unresolved": 2}
    assert last["matched_form"]["scores"]["market"] is None


def test_unfinished_then_finished_is_admitted_only_when_received(tmp_path):
    history, pins = setup_history(tmp_path)
    unfinished = rows()[0]
    unfinished["status"] = "unknown"
    head = add(tmp_path, history, pins, None, [unfinished], 0)
    head = add(tmp_path, history, pins, head, [rows()[0]], 1)
    report = evaluate_history(tmp_path, history, head, **pins)
    assert report["snapshots"][0]["matched_h2h"]["matches"] == 0
    assert report["snapshots"][1]["matched_h2h"]["matches"] == 1
    record = next(x for x in report["snapshots"][1]["records"] if x["fixture_id"] == "full")
    assert record["first_eligible_at"] == seconds(300).isoformat()


def test_updated_at_alone_does_not_change_sporting_result(tmp_path):
    history, pins = setup_history(tmp_path)
    original = rows()[0]
    later = deepcopy(original)
    later["updated_at"] = seconds(250).isoformat()
    head = add(tmp_path, history, pins, None, [original], 0)
    head = add(tmp_path, history, pins, head, [later], 1)
    assert evaluate_history(tmp_path, history, head, **pins)["snapshots"][-1]["matched_h2h"]["matches"] == 1


def test_new_receipt_pin_cannot_rebind_existing_history(tmp_path):
    history, pins = setup_history(tmp_path)
    head = add(tmp_path, history, pins, None, rows(), 0)
    path = tmp_path / "forecast.receipt.json"
    receipt = json.loads(path.read_text())
    receipt["frozen_at"] = seconds(112.75).isoformat()
    path.write_text(json.dumps(receipt))
    altered = {**pins, "expected_forecast_receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    with pytest.raises(ValueError, match="history link or evidence hash mismatch"):
        evaluate_history(tmp_path, history, head, **altered)
    with pytest.raises(ValueError, match="history link or evidence hash mismatch"):
        add(tmp_path, history, altered, head, rows(), 1)
    assert len(list(history.iterdir())) == 3


def test_wrong_pins_never_append_first_triplet(tmp_path):
    history, pins = setup_history(tmp_path)
    wrong = {**pins, "expected_forecast_sha256": "a" * 64}
    with pytest.raises(ValueError, match="pinned digest"):
        add(tmp_path, history, wrong, None, rows(), 0)
    wrong = {**pins, "expected_forecast_receipt_sha256": "a" * 64}
    with pytest.raises(ValueError, match="pinned digest"):
        add(tmp_path, history, wrong, None, rows(), 0)
    assert list(history.iterdir()) == []


@pytest.mark.parametrize("piece", ["body", "receipt", "link"])
def test_history_tampering_fails_with_latest_pin(tmp_path, piece):
    history, pins = setup_history(tmp_path)
    head = add(tmp_path, history, pins, None, rows(), 0)
    head = add(tmp_path, history, pins, head, rows(), 1)
    path = history / f"0001.{piece}.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        evaluate_history(tmp_path, history, head, **pins)


def test_missing_middle_latest_rollback_and_partial_write_fail_closed(tmp_path):
    history, pins = setup_history(tmp_path)
    first = add(tmp_path, history, pins, None, rows(), 0)
    second = add(tmp_path, history, pins, first, rows(), 1)
    head = add(tmp_path, history, pins, second, rows(), 2)
    for kind in ("body", "receipt", "link"):
        (history / f"0002.{kind}.json").unlink()
    with pytest.raises(ValueError, match="sequence gap"):
        evaluate_history(tmp_path, history, head, **pins)
    for kind in ("body", "receipt", "link"):
        (history / f"0003.{kind}.json").unlink()
    with pytest.raises(ValueError, match="externally pinned"):
        evaluate_history(tmp_path, history, head, **pins)
    (history / "0002.body.json").write_bytes(b"partial")
    with pytest.raises(ValueError, match="incomplete"):
        evaluate_history(tmp_path, history, first, **pins)


def test_stale_head_and_overlapping_request_cannot_append(tmp_path):
    history, pins = setup_history(tmp_path)
    head = add(tmp_path, history, pins, None, rows(), 0)
    before = sorted(x.name for x in history.iterdir())
    with pytest.raises(ValueError, match="externally pinned"):
        add(tmp_path, history, pins, "a" * 64, rows(), 1)
    raw, receipt = followup(rows(), requested=200, received=201)
    with pytest.raises(ValueError, match="strictly after"):
        append_followup(tmp_path, history, raw, json.dumps(receipt).encode(),
                        expected_head=head, **pins)
    assert sorted(x.name for x in history.iterdir()) == before


def test_v1_and_v2_links_are_not_interchangeable(tmp_path):
    history, pins = setup_history(tmp_path)
    head = add(tmp_path, history, pins, None, rows(), 0)
    with pytest.raises(ValueError, match="history link or evidence hash mismatch"):
        evaluate_legacy(tmp_path, history, head)
    path = history / "0001.link.json"
    link = json.loads(path.read_text())
    link.pop("forecast_receipt_sha256")
    link["schema"] = "research.tt_result_history_link.v1"
    raw = json.dumps(link).encode()
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="history link or evidence hash mismatch"):
        evaluate_history(tmp_path, history, hashlib.sha256(raw).hexdigest(), **pins)


def test_cli_appends_and_scores_with_all_three_pins(tmp_path):
    history, pins = setup_history(tmp_path)
    raw, receipt = followup(rows(), requested=190, received=200)
    body_path, receipt_path = tmp_path / "later.body", tmp_path / "later.receipt"
    body_path.write_bytes(raw)
    receipt_path.write_text(json.dumps(receipt))
    root = Path(__file__).resolve().parents[2]
    script = root / "experiments/table_tennis_replay/causal_strength_revision_history.py"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(root / "src"), str(root / "experiments/table_tennis_replay"),
         os.environ.get("PYTHONPATH", "")])}
    pinned = ["--expected-forecast-sha256", pins["expected_forecast_sha256"],
              "--expected-forecast-receipt-sha256", pins["expected_forecast_receipt_sha256"]]
    created = subprocess.run([sys.executable, str(script), "append", str(tmp_path),
                              str(history), str(body_path), str(receipt_path), *pinned],
                             env=env, capture_output=True, text=True, check=True)
    head = json.loads(created.stdout)["latest_link_sha256"]
    output = tmp_path / "report.json"
    subprocess.run([sys.executable, str(script), "score", str(tmp_path), str(history),
                    str(output), "--expected-head", head, *pinned],
                   env=env, capture_output=True, text=True, check=True)
    report = json.loads(output.read_text())
    assert report["expected_head_sha256"] == head
    assert report["snapshots"][0]["matched_h2h"]["matches"] == 1
