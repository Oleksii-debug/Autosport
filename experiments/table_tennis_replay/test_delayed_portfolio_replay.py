from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import hashlib
import json

import pytest

import delayed_portfolio_replay
from delayed_portfolio_replay import replay
from prospective_capture import collect
from test_quote_delay_audit import BASE, build_session, match, stamp


def later_result(identity, winner="home", finished=60, updated=80):
    score = "11-2" if winner == "home" else "2-11"
    return {"fixture_id": identity, "sport": "table_tennis", "home": identity + "A",
            "away": identity + "B", "league": "L", "status": "finished",
            "periods": [{"score": score}] * 3, "final_score": ",".join([score] * 3),
            "finished_at": stamp(finished), "updated_at": stamp(updated)}


def followup(rows, requested=90, received=100):
    raw = json.dumps({"sport": "table_tennis", "date": "2026-10-29", "results": rows}).encode()
    receipt = {"schema": "autosport.tt_dated_followup.v1", "bytes": len(raw),
               "sha256": hashlib.sha256(raw).hexdigest(),
               "url": "https://moviodds.com/results?sport=table_tennis&date=2026-10-29",
               "requested_at": stamp(requested), "received_at": stamp(received),
               "rights_verified": False, "identity_verified": False, "executable_price_verified": False}
    return raw, receipt


def set_session_results(path, seq, rows):
    body = path / f"{seq:04d}.results.body.json"
    receipt_path = path / f"{seq:04d}.results.receipt.json"
    raw = json.dumps({"sport": "table_tennis", "results": rows}).encode()
    body.write_bytes(raw)
    receipt = json.loads(receipt_path.read_text())
    receipt["sha256"], receipt["bytes"] = hashlib.sha256(raw).hexdigest(), len(raw)
    receipt_path.write_text(json.dumps(receipt))


def test_tolerance_is_later_only_assumed_fill_and_never_real(tmp_path):
    build_session(tmp_path, [(0, [match("a", 0), match("b", 0)]),
                             (40, [match("a", 40, 1.48), match("b", 40)])])
    raw, receipt = followup([later_result("a"), later_result("b")])
    report = replay(tmp_path, raw, receipt)
    assert report["common_counts"]["from_98_to_below_100_percent"] == 1
    strict, tolerant = report["policies"]["no_worse"], report["policies"]["tolerance_2pct"]
    assert strict["assumed_fills"] == 0 and strict["threshold_rejected"] == 1
    assert (tolerant["assumed_fills"], tolerant["won"], tolerant["pending"]) == (1, 1, 0)
    assert tolerant["open_events"][0]["legs"][0]["assumed_price"] == "1.48"
    assert tolerant["cost_stressed_net_pending_at_face_value"] == "1.210"
    assert report["fills_are_assumed"] is True
    assert report["bookmaker_acceptance_verified"] is report["real_money_execution"] is False
    assert report["no_bet"]["net"] == "0"


def test_unknown_outcome_stays_pending_and_cannot_become_loss(tmp_path):
    build_session(tmp_path, [(0, [match("a", 0), match("b", 0)]),
                             (40, [match("a", 40), match("b", 40)])])
    raw, receipt = followup([later_result("a", "away")])
    report = replay(tmp_path, raw, receipt)["policies"]["no_worse"]
    assert (report["assumed_fills"], report["won"], report["lost"], report["pending"]) == (1, 0, 0, 1)
    assert report["open_stake"] == "1"
    assert report["gross_net_pending_at_face_value"] == "0"


def test_future_results_cannot_change_prior_actions(tmp_path):
    build_session(tmp_path, [(0, [match("a", 0), match("b", 0)]),
                             (40, [match("a", 40), match("b", 40)])])
    good = followup([later_result("a"), later_result("b")])
    bad = followup([later_result("a", "away"), later_result("b")])
    a, b = replay(tmp_path, *good), replay(tmp_path, *bad)
    assert a["decisions"] == b["decisions"]
    assert a["policies"]["no_worse"]["open_events"] == b["policies"]["no_worse"]["open_events"]
    assert a["policies"]["no_worse"]["won"] == 1
    assert b["policies"]["no_worse"]["lost"] == 1


def test_outcome_received_before_hypothetical_fill_blocks_pair(tmp_path):
    build_session(tmp_path, [(0, [match("a", 0), match("b", 0)]),
                             (40, [match("a", 40), match("b", 40)])])
    set_session_results(tmp_path, 1, [{"fixture_id": "a", "status": "unknown"}])
    raw, receipt = followup([later_result("a"), later_result("b")])
    report = replay(tmp_path, raw, receipt)
    assert report["common_counts"]["result_seen_before_fill"] == 1
    assert report["policies"]["no_worse"]["assumed_fills"] == 0


def test_later_unavailable_does_not_substitute_another_fixture(tmp_path):
    original = [match(x, 0) for x in "abc"]
    later = [match(x, 40) for x in "abc"]
    later[0]["quotes"][0]["suspended"] = True
    build_session(tmp_path, [(0, original), (40, later)])
    report = replay(tmp_path, *followup([later_result(x) for x in "abc"]))
    assert report["decisions"][0]["fixture_ids"] == ["a", "b"]
    assert report["common_counts"]["missing_or_invalid"] == 1
    assert report["policies"]["tolerance_2pct"]["assumed_fills"] == 0


@pytest.mark.parametrize("corruption", ["name", "early_last_live", "future_update", "score"])
def test_unusable_followup_never_settles_a_leg(tmp_path, corruption):
    build_session(tmp_path, [(0, [match("a", 0), match("b", 0)]),
                             (40, [match("a", 40), match("b", 40)])])
    a, b = later_result("a"), later_result("b")
    if corruption == "name": a["home"] = "different"
    elif corruption == "early_last_live": a["finished_at"] = stamp(30)
    elif corruption == "future_update": a["updated_at"] = stamp(101)
    else: a["final_score"] = "11-3,11-2,11-2"
    report = replay(tmp_path, *followup([a, b]))["policies"]["no_worse"]
    assert (report["won"], report["lost"], report["pending"]) == (0, 0, 1)


def test_correction_after_fill_quarantines_settlement(tmp_path):
    build_session(tmp_path, [(0, [match("a", 0), match("b", 0)]),
                             (40, [match("a", 40), match("b", 40)])])
    set_session_results(tmp_path, 2, [later_result("a", "away", finished=42, updated=42)])
    report = replay(tmp_path, *followup([later_result("a"), later_result("b")]))
    assert report["policies"]["no_worse"]["pending"] == 1
    assert report["policies"]["no_worse"]["lost"] == 0


def test_duplicate_or_future_prior_result_after_fill_quarantines(tmp_path):
    build_session(tmp_path, [(0, [match("a", 0), match("b", 0)]),
                             (40, [match("a", 40), match("b", 40)])])
    prior = later_result("a", finished=42, updated=42)
    set_session_results(tmp_path, 2, [prior, deepcopy(prior)])
    raw, receipt = followup([later_result("a"), later_result("b")])
    assert replay(tmp_path, raw, receipt)["policies"]["no_worse"]["pending"] == 1
    prior["updated_at"] = stamp(44)  # result receipt is at 43
    set_session_results(tmp_path, 2, [prior])
    assert replay(tmp_path, raw, receipt)["policies"]["no_worse"]["pending"] == 1


def test_multiple_open_tickets_expose_shared_fixture(tmp_path):
    times = iter(BASE + timedelta(seconds=s) for s in (0, 1, 2, 3, 40, 41, 42, 43, 80, 81, 82, 83))
    boards = iter((0, 40, 80))
    def fetch(url):
        if "/odds?" in url:
            seconds = next(boards)
            return json.dumps({"server_time": stamp(seconds),
                               "matches": [match("a", seconds), match("b", seconds)]}).encode()
        return b'{"sport":"table_tennis","results":[]}'
    collect(tmp_path, cycles=3, fetch=fetch, clock=lambda: next(times), sleep=lambda _: None)
    results = [later_result("a", finished=100, updated=110),
               later_result("b", finished=100, updated=110)]
    report = replay(tmp_path, *followup(results, requested=120, received=130))["policies"]["no_worse"]
    assert report["assumed_fills"] == report["won"] == 2
    assert report["peak_tickets_sharing_fixture"] == 2


def test_rejects_tampered_or_backdated_followup(tmp_path):
    build_session(tmp_path, [(0, [match("a", 0), match("b", 0)]),
                             (40, [match("a", 40), match("b", 40)])])
    raw, receipt = followup([later_result("a"), later_result("b")])
    with pytest.raises(ValueError, match="receipt mismatch"):
        replay(tmp_path, raw + b" ", receipt)
    backdated = deepcopy(receipt)
    backdated["requested_at"] = stamp(42)
    with pytest.raises(ValueError, match="after prior capture"):
        replay(tmp_path, raw, backdated)


def test_out_of_window_quotes_are_not_interpolated(tmp_path):
    build_session(tmp_path, [(0, [match("a", 0), match("b", 0)]),
                             (61, [match("a", 61), match("b", 61)])], interval=61)
    report = replay(tmp_path, *followup([later_result("a", finished=75),
                                        later_result("b", finished=75)]))
    assert report["common_counts"]["outside_30_60_seconds"] == 1
    assert report["policies"]["no_worse"]["assumed_fills"] == 0


def test_paper_risk_rejects_stake_exceeding_bankroll_cap(tmp_path, monkeypatch):
    build_session(tmp_path, [(0, [match("a", 0), match("b", 0)]),
                             (40, [match("a", 40), match("b", 40)])])
    monkeypatch.setattr(delayed_portfolio_replay, "INITIAL", Decimal("1"))
    report = replay(tmp_path, *followup([later_result("a"), later_result("b")]))
    assert report["policies"]["no_worse"]["risk_rejected"] == 1
    assert report["policies"]["no_worse"]["assumed_fills"] == 0
