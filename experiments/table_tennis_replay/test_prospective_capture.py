from datetime import datetime, timedelta, timezone
import json
from urllib.error import HTTPError

import pytest

from prospective_capture import NoRedirect, capture_one, collect, iter_visible_before, verify_session


def clock():
    now = datetime(2026, 10, 29, tzinfo=timezone.utc)
    while True:
        yield now
        now += timedelta(seconds=1)


def fake_fetch(url):
    if "/odds?" in url:
        return b'{"server_time":"2026-10-29T00:00:00Z","matches":[]}'
    return b'{"sport":"table_tennis","results":[]}'


def test_capture_resume_avoids_refetch_and_preserves_receipts(tmp_path):
    ticks = clock()
    calls = []
    def fetch(url):
        calls.append(url)
        return fake_fetch(url)
    collect(tmp_path, cycles=1, fetch=fetch, clock=lambda: next(ticks), sleep=lambda _: None)
    before = (tmp_path / "0001.odds.receipt.json").read_bytes()
    collect(tmp_path, cycles=2, fetch=fetch, clock=lambda: next(ticks), sleep=lambda _: None)
    assert len(calls) == 4
    assert len(verify_session(tmp_path)) == 4
    assert (tmp_path / "0001.odds.receipt.json").read_bytes() == before


def test_failed_fetch_can_resume_after_verified_partial_cycle(tmp_path):
    ticks = clock()
    def fails_on_results(url):
        if "/results?" in url:
            raise OSError("network unavailable")
        return fake_fetch(url)
    with pytest.raises(OSError):
        collect(tmp_path, cycles=1, fetch=fails_on_results, clock=lambda: next(ticks), sleep=lambda _: None)
    assert set(verify_session(tmp_path)) == {(1, "odds")}
    collect(tmp_path, cycles=1, fetch=fake_fetch, clock=lambda: next(ticks), sleep=lambda _: None)
    assert len(verify_session(tmp_path)) == 2


def test_modified_body_or_receipt_is_rejected(tmp_path):
    ticks = clock()
    capture_one(tmp_path, 1, "odds", fetch=fake_fetch, clock=lambda: next(ticks))
    (tmp_path / "0001.odds.body.json").write_bytes(b'{"matches":[]}')
    with pytest.raises(ValueError, match="receipt does not bind"):
        verify_session(tmp_path)


def test_incomplete_pair_fails_closed_without_deleting(tmp_path):
    path = tmp_path / "0001.odds.body.json"
    path.write_bytes(b'{"matches":[]}')
    with pytest.raises(ValueError, match="incomplete"):
        verify_session(tmp_path)
    assert path.exists()


def test_bad_schema_and_duplicate_json_do_not_write(tmp_path):
    ticks = clock()
    for bad in (b'{"matches":[],"matches":[]}', b'{"sport":"soccer","results":[]}'):
        kind = "odds" if b'matches' in bad else "results"
        with pytest.raises(ValueError):
            capture_one(tmp_path, 1, kind, fetch=lambda _: bad, clock=lambda: next(ticks))
    assert not list(tmp_path.iterdir())


def test_sequence_gap_and_timestamp_reordering_rejected(tmp_path):
    ticks = clock()
    capture_one(tmp_path, 2, "odds", fetch=fake_fetch, clock=lambda: next(ticks))
    with pytest.raises(ValueError, match="sequence gap"):
        verify_session(tmp_path)
    (tmp_path / "0002.odds.body.json").unlink()
    (tmp_path / "0002.odds.receipt.json").unlink()
    capture_one(tmp_path, 1, "odds", fetch=fake_fetch, clock=lambda: next(ticks))
    rec = tmp_path / "0001.odds.receipt.json"
    value = json.loads(rec.read_text())
    value["requested_at"] = "2026-10-30T00:00:00+00:00"
    rec.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="clock order"):
        verify_session(tmp_path)


def test_source_time_mismatch_and_redirect_rejected(tmp_path):
    ticks = clock()
    capture_one(tmp_path, 1, "odds", fetch=fake_fetch, clock=lambda: next(ticks))
    rec = tmp_path / "0001.odds.receipt.json"
    value = json.loads(rec.read_text()); value["source_server_time"] = "different"
    rec.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="source time mismatch"):
        verify_session(tmp_path)
    class Req:
        full_url = "https://moviodds.com/odds"
    with pytest.raises(HTTPError):
        NoRedirect().redirect_request(Req(), None, 302, "", {}, "https://other.example")


def test_symlink_evidence_rejected(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"{}")
    link = tmp_path / "0001.odds.body.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    target.unlink()
    with pytest.raises(ValueError, match="unexpected"):
        verify_session(tmp_path)


def test_causal_reader_excludes_equal_or_later_receipts(tmp_path):
    ticks = clock()
    collect(tmp_path, cycles=1, fetch=fake_fetch, clock=lambda: next(ticks), sleep=lambda _: None)
    odds_received = datetime(2026, 10, 29, 0, 0, 1, tzinfo=timezone.utc)
    result_received = datetime(2026, 10, 29, 0, 0, 3, tzinfo=timezone.utc)
    assert list(iter_visible_before(tmp_path, odds_received)) == []
    visible = list(iter_visible_before(tmp_path, result_received))
    assert [r["kind"] for r, _ in visible] == ["odds"]
    visible = list(iter_visible_before(tmp_path, result_received + timedelta(microseconds=1)))
    assert [r["kind"] for r, _ in visible] == ["odds", "results"]
    with pytest.raises(ValueError, match="timezone-aware"):
        list(iter_visible_before(tmp_path, datetime(2026, 10, 29)))
