import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location("table_tennis_record", Path(__file__).parents[1] / "experiments/table_tennis_replay/record.py")
record = importlib.util.module_from_spec(spec)
spec.loader.exec_module(record)


def item(stamp, score="0-0"):
    return {"id": "1", "home": "A", "away": "B", "league": "L", "in_play": True,
            "score": score, "last_live_seen": stamp, "group_status": {"240": {"polled_at": stamp}},
            "quotes": [{"group_id": "240", "side": "A", "label": "A", "odds_dec": 1.5},
                       {"group_id": "240", "side": "B", "label": "B", "odds_dec": 2.5},
                       {"group_id": "240", "side": "A", "line": "-1.5", "odds_dec": 8}]}


def state():
    return {"matches": [{"id": "1", "home": "A", "away": "B", "league": "L", "result": None,
                         "first_live_at": None, "live_id": None, "frames": [], "status": "Очікується"}],
            "errors": []}


def test_live_frames_every_three_minutes_and_final_score():
    t = datetime(2026, 10, 28, tzinfo=timezone.utc)
    s = state()
    record.cycle(s, {"matches": [item(t.isoformat())]}, {"results": []}, now=t, board_sha="a"*64)
    later = t + timedelta(seconds=182)
    record.cycle(s, {"matches": [item(later.isoformat(), "11-9,4-3")]}, {"results": []}, now=later, board_sha="b"*64)
    frames = s["matches"][0]["frames"]
    assert [x["offset_seconds"] for x in frames] == [0, 180]
    assert frames[1]["score"] == "11-9,4-3"
    assert frames[1]["odds"]["home"]["odds"] == 1.5
    finish = later + timedelta(seconds=80)
    result = {"fixture_id": "1", "home": "A", "away": "B", "league": "L", "status": "finished",
              "final_score": "3-0", "periods": [{"name": "Game 1", "score": "11-9"}]}
    record.cycle(s, {"matches": []}, {"results": [result]}, now=finish, board_sha="c"*64)
    assert s["recording_complete"] and s["coverage_complete"]
    assert s["matches"][0]["frames"][-1]["kind"] == "final"
    assert s["matches"][0]["frames"][-1]["score"] == "3-0"


def test_gap_is_explicit_and_no_guess_is_made_for_unlinked_live_id(monkeypatch):
    t = datetime(2026, 10, 28, tzinfo=timezone.utc)
    s = state()
    wrong = item(t.isoformat())
    wrong["id"] = "another-id"
    monkeypatch.setattr(record, "related_ids", lambda _: set())
    record.cycle(s, {"matches": [wrong]}, {"results": []}, now=t, board_sha="a"*64)
    assert s["matches"][0]["frames"] == []
    record.cycle(s, {"matches": [item(t.isoformat())]}, {"results": []}, now=t, board_sha="a"*64)
    late = t + timedelta(seconds=450)
    record.cycle(s, {"matches": [item(late.isoformat())]}, {"results": []}, now=late, board_sha="b"*64)
    assert [f["kind"] for f in s["matches"][0]["frames"]] == ["live", "missing", "missing"]


def test_finished_without_capture_does_not_claim_complete_recording():
    t = datetime(2026, 10, 28, tzinfo=timezone.utc)
    s = state()
    result = {"fixture_id": "1", "home": "A", "away": "B", "league": "L", "status": "finished", "final_score": "3-1"}
    record.cycle(s, {"matches": []}, {"results": [result]}, now=t, board_sha="a"*64)
    assert s["recording_complete"] is True
    assert s["coverage_complete"] is False
    assert not s["matches"][0]["frames"]


def test_already_finished_match_does_not_gain_a_fabricated_live_start():
    t = datetime(2026, 10, 28, tzinfo=timezone.utc)
    s = state()
    result = {"fixture_id": "1", "home": "A", "away": "B", "league": "L", "status": "finished", "final_score": "3-1"}
    record.cycle(s, {"matches": [item(t.isoformat(), "11-9,11-7,9-11,11-6")]}, {"results": [result]}, now=t, board_sha="a"*64)
    assert s["matches"][0]["first_live_at"] is None
    assert not s["matches"][0]["frames"]
    assert s["coverage_complete"] is False


def test_provider_linked_live_id_with_result_is_not_a_new_live_start(monkeypatch):
    t = datetime(2026, 10, 28, tzinfo=timezone.utc)
    s = state()
    live = item(t.isoformat())
    live["id"] = "live-2"
    result = {"fixture_id": "live-2", "home": "A", "away": "B", "league": "L", "status": "finished", "final_score": "3-1"}
    monkeypatch.setattr(record, "related_ids", lambda _: {"1", "live-2"})
    record.cycle(s, {"matches": [live]}, {"results": [result]}, now=t, board_sha="a"*64)
    assert s["matches"][0]["first_live_at"] is None
    assert s["matches"][0]["result"]["final_score"] == "3-1"
