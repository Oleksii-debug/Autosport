from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from portfolio_replay import replay

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def stamp(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat()


def frame(identity, seconds, home=2.0, away=1.5):
    quote = lambda p: {"odds": p, "suspended": False, "stale": False,
                       "source_price_changed_at": stamp(seconds), "market_checked_at": stamp(seconds)}
    return {"kind": "live", "observed_at": stamp(seconds), "source_id": identity,
            "source_stale": False, "source_polled_at": stamp(seconds),
            "source_last_live_seen": stamp(seconds), "odds": {"home": quote(home), "away": quote(away)}}


def fixture(identity, frames, winner="away", end=200):
    match = {"id": identity, "live_id": identity, "frames": frames}
    if winner is not None:
        score = "11-2" if winner == "home" else "2-11"
        match["result"] = {"observed_at": stamp(end), "source_fixture_id": identity,
                           "periods": [{"score": score}] * 3}
    return match


def test_shared_loss_and_sequential_paper_tickets():
    data = {"matches": [fixture("a", [frame("a", 0), frame("a", 180)], "home"),
                        fixture("b", [frame("b", 0), frame("b", 180)], "away")]}
    report = replay(data)
    assert (report["opened"], report["won"], report["lost"], report["pending"]) == (2, 0, 2, 0)
    assert report["max_tickets_sharing_fixture"] == 2
    assert Decimal_string(report["cost_stressed_net_assuming_open_stake_at_face_value"]) == "-2.02"


def Decimal_string(value):
    from decimal import Decimal
    return str(Decimal(value).quantize(Decimal("0.01")))


def test_unknown_result_remains_pending():
    report = replay({"matches": [fixture("a", [frame("a", 0)], None),
                                 fixture("b", [frame("b", 0)], "away")]})
    assert (report["opened"], report["pending"], report["lost"]) == (1, 1, 0)
    assert report["open_stake_units"] == "1"


def test_no_future_outcome_in_decisions():
    data = {"matches": [fixture("a", [frame("a", 0), frame("a", 180)], "home"),
                        fixture("b", [frame("b", 0), frame("b", 180)], "away")]}
    changed = deepcopy(data)
    changed["matches"][0]["result"]["periods"] = [{"score": "2-11"}] * 3
    opens = lambda r: [e for e in r["events"] if e["action"] == "open"]
    assert opens(replay(data)) == opens(replay(changed))
    assert replay(data)["lost"] == 2 and replay(changed)["won"] == 2


def test_stale_or_suspended_new_frame_invalidates_prior():
    stale = frame("a", 10)
    stale["odds"]["away"]["suspended"] = True
    report = replay({"matches": [fixture("a", [frame("a", 0), stale]),
                                 fixture("b", [frame("b", 10)])]})
    assert report["opened"] == 0


def test_old_and_future_market_times_refused():
    future = frame("a", 0)
    future["odds"]["home"]["market_checked_at"] = stamp(1)
    assert replay({"matches": [fixture("a", [future]), fixture("b", [frame("b", 0)])]})["opened"] == 0
    assert replay({"matches": [fixture("a", [frame("a", 0)]), fixture("b", [frame("b", 91)])]})["opened"] == 0


def test_same_time_result_after_open_not_lookahead():
    data = {"matches": [fixture("a", [frame("a", 0)], "home", 0),
                        fixture("b", [frame("b", 0)], "away", 0)]}
    report = replay(data)
    assert [event["action"] for event in report["events"]] == ["open", "settle"]
    assert report["lost"] == 1


def test_duplicate_ids_and_frame_times_fail_closed():
    a = fixture("a", [frame("a", 0)])
    with pytest.raises(ValueError, match="duplicate fixture"):
        replay({"matches": [a, deepcopy(a)]})
    a["frames"].append(frame("a", 0))
    with pytest.raises(ValueError, match="duplicate frame"):
        replay({"matches": [a]})


def test_invalid_or_wrong_identity_result_cannot_settle():
    a = fixture("a", [frame("a", 0)])
    a["result"]["source_fixture_id"] = "other"
    report = replay({"matches": [a, fixture("b", [frame("b", 0)])]})
    assert report["pending"] == 1


def test_unproven_cross_id_link_cannot_open_or_settle():
    a = fixture("a", [frame("alternate", 0)])
    a["live_id"] = "alternate"
    assert replay({"matches": [a, fixture("b", [frame("b", 0)])]})["opened"] == 0
