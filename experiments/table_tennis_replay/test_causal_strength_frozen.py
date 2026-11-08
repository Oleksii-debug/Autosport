"""Synthetic only: integration and adversarial checks, no real results or bets."""
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import math

import pytest

import causal_strength_frozen as module
from causal_strength_frozen import evaluate, freeze
from prospective_capture import capture_one
from test_causal_h2h_observer import observed
from test_causal_strength_h2h import history, setup
from test_delayed_portfolio_replay import followup, later_result, set_session_results
from test_quote_delay_audit import BASE, build_session, match


def seconds(value):
    return BASE + timedelta(seconds=value)


def quote(fixture, home, away):
    row = match(fixture, 110)
    row["home"], row["away"] = home, away
    row["quotes"][0]["side"], row["quotes"][1]["side"] = home, away
    return row


def dated(fixture, home, away, winner="home"):
    row = later_result(fixture, winner, finished=160, updated=180)
    row["home"], row["away"] = home, away
    return row


def setup_frozen(root):
    root.mkdir(exist_ok=True)
    prior_session = root / "prior"
    build_session(prior_session, [(0, [match("old", 0)]), (40, [match("old", 40)])])
    raw, receipt = followup([later_result("known")])
    (root / "prior.json").write_bytes(raw)
    (root / "prior.receipt.json").write_text(json.dumps(receipt))
    registry, store = setup(root)
    history(store, with_h2h=True)
    store.record_performance(observed(event="eg", subject="E", opponent="G", score="1"))
    board = {"server_time": seconds(110).isoformat(), "matches": [
        quote("full", "A", "B"), quote("form", "A", "E"), match("none", 110)]}
    times = iter([seconds(110), seconds(111)])
    capture_one(root / "snapshots", 1, "odds", fetch=lambda _: json.dumps(board).encode(),
                clock=lambda: next(times))
    return (prior_session, root / "prior.json", root / "prior.receipt.json",
            registry.path, store.path)


def complete(root, prior):
    times = iter([seconds(112), seconds(112.5)])
    envelope = freeze(root, *prior, clock=lambda: next(times))
    times = iter([seconds(113), seconds(114)])
    capture_one(root / "snapshots", 1, "results",
                fetch=lambda _: b'{"sport":"table_tennis","results":[]}',
                clock=lambda: next(times))
    return envelope


def pins(envelope):
    return {"expected_forecast_sha256": envelope["sha256"],
            "expected_receipt_sha256": envelope["receipt_sha256"]}


def results():
    return followup([dated("full", "A", "B"), dated("form", "A", "E", "away"),
                     dated("none", "noneA", "noneB")], requested=190, received=200)


def test_frozen_candidates_and_matched_outcome_cohorts(tmp_path):
    prior = setup_frozen(tmp_path)
    pinned = pins(complete(tmp_path, prior))
    report = evaluate(tmp_path, *results(), **pinned)
    assert report["frozen_candidates"] == report["outcome_counts"]["eligible"] == 3
    assert report["all_eligible_market"]["matches"] == 3
    form = report["matched_form"]
    combined = report["matched_h2h"]
    assert form["fixture_ids"] == ["form", "full"]
    assert combined["fixture_ids"] == ["full"]
    assert report["candidate_status_counts"]["form_ready"] == 2
    assert report["candidate_status_counts"]["form_blocked_by_identity_or_h2h_history"] == 1
    rows = {row["fixture_id"]: row for row in report["rows"]}
    assert form["scores"]["market"]["brier"] == pytest.approx((0.625**2 + 0.375**2) / 2)
    assert form["scores"]["form"]["log_loss"] == pytest.approx(
        -(math.log(1 - rows["form"]["form"]) + math.log(rows["full"]["form"])) / 2)
    assert combined["scores"]["form_h2h"]["brier"] == pytest.approx((1 - 59/84)**2)
    economic = report["hypothetical_single_ticket_economics"]
    combined_returns = economic["matched"]["form_h2h"]["results"]
    assert combined_returns["market"]["selected"] == combined_returns["wait"]["selected"] == 0
    assert combined_returns["form_h2h"]["settled"] == 1
    assert Decimal(combined_returns["form_h2h"]["settled_net_units"]) == Decimal("0.46")
    assert report["matched_common_opponents"]["matches"] == 0
    assert economic["accepted_quotes_verified"] is economic["real_money_execution"] is False
    assert report["independent_holdout"] is report["identity_verified"] is False
    assert report["profitability_claim"] is report["model_training_authorized"] is False


def test_pending_selected_match_is_not_profit_or_a_confirmed_loss(tmp_path):
    prior = setup_frozen(tmp_path)
    pinned = pins(complete(tmp_path, prior))
    raw, receipt = followup([dated("form", "A", "E")], requested=190, received=200)
    report = evaluate(tmp_path, raw, receipt, **pinned)
    tally = report["hypothetical_single_ticket_economics"]["matched"]["form_h2h"]["results"]["form_h2h"]
    assert tally["selected"] == tally["unresolved"] == 1
    assert tally["settled"] == 0
    assert Decimal(tally["settled_net_units"]) == 0
    assert Decimal(tally["if_all_unresolved_lose_units"]) == Decimal("-1.01")


def test_common_opponents_have_their_own_matched_market_comparison(tmp_path):
    prior = setup_frozen(tmp_path)
    from autosport.opponent_intelligence import OpponentIntelligenceStore
    from autosport.participant_identity import ParticipantIdentityRegistry
    registry = ParticipantIdentityRegistry(prior[-2])
    store = OpponentIntelligenceStore(prior[-1], registry)
    for name in ("C", "D"):
        store.record_performance(observed(event="b" + name, subject="B", opponent=name, score="0"))
    report = evaluate(tmp_path, *results(), **pins(complete(tmp_path, prior)))
    compared = report["matched_common_opponents"]
    assert compared["fixture_ids"] == ["full"]
    assert compared["scores"]["common_opponents"]["brier"] == pytest.approx(1 / 9)
    assert compared["scores"]["market"]["brier"] == pytest.approx((1 - 0.625) ** 2)
    tally = report["hypothetical_single_ticket_economics"]["matched"]["common_opponents"]["results"]
    # The heuristic is closer on this sample, but its modeled edge is too low to bet.
    assert tally["common_opponents"]["selected"] == 0


def test_frozen_economic_replay_cannot_substitute_a_better_quote(tmp_path):
    prior = setup_frozen(tmp_path)
    complete(tmp_path, prior)
    journal = json.loads((tmp_path / "forecast.body.json").read_text("utf-8"))
    from delay_policy_audit import _exact_snapshot
    from prospective_capture import verify_session
    session = tmp_path / "snapshots"
    snapshot = _exact_snapshot(session, 1, verify_session(session)[(1, "odds")])
    journal["forecasts"][0]["paper_economic_decision"]["displayed_odds"][0] = "12"
    with pytest.raises(ValueError, match="does not match source quotes"):
        module._economic_replay(journal["forecasts"], [], snapshot)


def test_unknown_outcome_and_conflicting_history_do_not_become_losses(tmp_path):
    prior = setup_frozen(tmp_path)
    pinned = pins(complete(tmp_path, prior))
    earlier = dated("full", "A", "B", "away")
    earlier["finished_at"] = earlier["updated_at"] = seconds(113.5).isoformat()
    set_session_results(tmp_path / "snapshots", 1, [earlier])
    raw, receipt = followup([dated("full", "A", "B"), dated("form", "A", "E")],
                            requested=190, received=200)
    report = evaluate(tmp_path, raw, receipt, **pinned)
    assert report["outcome_counts"]["identity_time_or_correction_conflict"] == 1
    assert report["outcome_counts"]["not_present_in_followup"] == 1
    assert report["matched_form"]["fixture_ids"] == ["form"]
    assert report["matched_h2h"]["matches"] == 0
    assert all(v is None for v in report["matched_h2h"]["scores"].values())


def test_pre_result_boundary_and_existing_artifact_are_fail_closed(tmp_path):
    prior = setup_frozen(tmp_path)
    pinned = pins(complete(tmp_path, prior))
    with pytest.raises(ValueError, match="no current results"):
        freeze(tmp_path, *prior, clock=lambda: seconds(115))
    receipt_path = tmp_path / "forecast.receipt.json"
    envelope = json.loads(receipt_path.read_text())
    envelope["frozen_at"] = seconds(113).isoformat()  # equals result request
    receipt_path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="chronology"):
        evaluate(tmp_path, *results(), **pinned)


def test_store_change_during_construction_is_rejected(tmp_path, monkeypatch):
    prior = setup_frozen(tmp_path)
    original = module.build_strength_journal
    def mutate(*args, **kwargs):
        journal = original(*args, **kwargs)
        prior[3].write_bytes(prior[3].read_bytes() + b" ")
        return journal
    monkeypatch.setattr(module, "build_strength_journal", mutate)
    with pytest.raises(ValueError, match="stores changed"):
        freeze(tmp_path, *prior, clock=lambda: seconds(112))
    assert not (tmp_path / "forecast.body.json").exists()


def test_external_pin_rejects_rewritten_forecast_even_with_rewritten_receipt(tmp_path):
    prior = setup_frozen(tmp_path)
    pinned = pins(complete(tmp_path, prior))
    path = tmp_path / "forecast.body.json"
    journal = json.loads(path.read_text())
    journal["forecasts"][0]["strength_home_probability"] = "0.8"
    payload = json.dumps(journal).encode()
    path.write_bytes(payload)
    receipt_path = tmp_path / "forecast.receipt.json"
    envelope = json.loads(receipt_path.read_text())
    envelope["sha256"], envelope["bytes"] = hashlib.sha256(payload).hexdigest(), len(payload)
    receipt_path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="pinned digest"):
        evaluate(tmp_path, *results(), **pinned)


def test_invalid_candidate_status_or_probability_is_rejected(tmp_path):
    prior = setup_frozen(tmp_path)
    complete(tmp_path, prior)
    path = tmp_path / "forecast.body.json"
    journal = json.loads(path.read_text())
    row = next(x for x in journal["forecasts"] if x["fixture_id"] == "full")
    row["strength_home_probability"] = "NaN"
    payload = json.dumps(journal).encode()
    path.write_bytes(payload)
    receipt_path = tmp_path / "forecast.receipt.json"
    envelope = json.loads(receipt_path.read_text())
    envelope["sha256"], envelope["bytes"] = hashlib.sha256(payload).hexdigest(), len(payload)
    receipt_path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="invalid candidate probability"):
        evaluate(tmp_path, *results(), expected_forecast_sha256=envelope["sha256"],
                 expected_receipt_sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest())


def test_correction_at_decision_time_cannot_change_frozen_form(tmp_path):
    from autosport.opponent_intelligence import OpponentIntelligenceStore
    from autosport.participant_identity import ParticipantIdentityRegistry

    prior = setup_frozen(tmp_path)
    registry = ParticipantIdentityRegistry(prior[3])
    store = OpponentIntelligenceStore(prior[4], registry)
    original = store.record_performance(observed(event="ac", subject="A", opponent="C", score="1"))
    store.record_performance(observed(event="ac", subject="A", opponent="C", score="0",
                                      available=112, recorded=112,
                                      supersedes=original.performance_id))
    pinned = pins(complete(tmp_path, prior))
    journal = json.loads((tmp_path / "forecast.body.json").read_text())
    full = next(row for row in journal["forecasts"] if row["fixture_id"] == "full")
    assert float(full["strength_home_probability"]) == pytest.approx(9/14)
    report = evaluate(tmp_path, *results(), **pinned)
    assert report["matched_h2h"]["matches"] == 1


def test_external_receipt_pin_rejects_changed_but_plausible_freeze_time(tmp_path):
    prior = setup_frozen(tmp_path)
    pinned = pins(complete(tmp_path, prior))
    path = tmp_path / "forecast.receipt.json"
    envelope = json.loads(path.read_text())
    envelope["frozen_at"] = seconds(112.75).isoformat()  # still before result request
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="receipt does not match independently pinned digest"):
        evaluate(tmp_path, *results(), **pinned)
