import json

import pytest

from forward_scoped_recheck import capture_next
from test_delayed_portfolio_replay import followup
from test_forward_portfolio_journal import HEAD, clock
from test_forward_portfolio_revisions import add, fixture, results
from test_quote_delay_audit import stamp


def prior(tmp_path):
    session, journal, history = fixture(tmp_path)
    head, _, _ = add(session, journal, history, None, [], 0)
    return session, journal, history, head


def check(data, output, *, raw=b"{}", at=260, earliest=200, head=None):
    session, journal, history, actual = data
    return capture_next(session, journal, frozenset(), HEAD, history, head or actual, output,
                        date="2026-10-29", not_before=stamp(earliest),
                        fetch=lambda _: raw, clock=clock(at, at + 1))


def test_recheck_keeps_previous_pending_state_and_appends(tmp_path):
    data = prior(tmp_path)
    raw, _ = followup(results(updated=180))
    output = tmp_path / "recheck"
    report = check(data, output, raw=raw)
    assert report["earlier_reports_preserved"]
    assert report["fixture_counts"] == {"eligible": 2, "quarantined": 0, "unresolved": 0}
    assert report["policies"]["no_worse"]["won"] == 2
    stored = json.loads((output / "revision-report.json").read_text())
    assert stored["snapshots"][0]["policies"]["no_worse"]["pending"] == 2
    assert (output / "dated-results.body.json").read_bytes() == raw


def test_early_request_and_wrong_pin_never_fetch(tmp_path):
    data = prior(tmp_path)
    with pytest.raises(ValueError, match="too early"):
        check(data, tmp_path / "too-early", at=199)
    with pytest.raises(ValueError, match="externally pinned"):
        check(data, tmp_path / "wrong-head", head="b" * 64)
    assert not (tmp_path / "too-early").exists()
    assert not (tmp_path / "wrong-head").exists()


def test_malformed_response_is_retained_and_not_appended(tmp_path):
    data = prior(tmp_path)
    output = tmp_path / "malformed"
    with pytest.raises(ValueError):
        check(data, output, raw=b"malformed response")
    assert (output / "dated-results.body.json").read_bytes() == b"malformed response"
    assert (output / "dated-results.receipt.json").is_file()
    assert not (data[2] / "0002.link.json").exists()


def test_existing_destination_prevents_another_fetch(tmp_path):
    data = prior(tmp_path)
    output = tmp_path / "existing"; output.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        check(data, output)
